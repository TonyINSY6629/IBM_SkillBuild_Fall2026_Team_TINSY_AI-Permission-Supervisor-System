#!/usr/bin/env python3
"""
hook_client/main.py — AI Permission Supervisor Hook Client

Registered as the PreToolUse (and optionally UserPromptSubmit / PostToolUse / Stop)
hook in ~/.bob/settings/settings.json.

Contract:
  exit 0          → tool is allowed to run
  exit 2 + stderr → tool is blocked (reason on stderr is forwarded to the model)
  Any other exit  → Bob treats as non-blocking failure — tool RUNS (fails open).
                    The top-level try/except ensures we never reach this path.

Timeout ordering (must be maintained in settings.json):
  escalation_timeout (120 s) < DEADLINE (130 s) < Bob hook timeout (150 s)

This file must remain minimal and dependency-light.
Only stdlib modules are imported. No third-party packages.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration — can be overridden via environment variables
# ---------------------------------------------------------------------------

SUPERVISOR_URL: str = os.environ.get(
    "SUPERVISOR_URL", "http://127.0.0.1:9001"
)
DECIDE_ENDPOINT: str = f"{SUPERVISOR_URL}/decide"

# Hook Client deadline in seconds.  Must be:
#   escalation_timeout < DEADLINE < Bob hook timeout
# Default values: 120 < 130 < 150
DEADLINE_SECONDS: float = float(os.environ.get("HOOK_DEADLINE_SECONDS", "130"))
# Short timeout for non-PreToolUse events — they can never block a tool call.
FORWARD_TIMEOUT_SECONDS: float = float(os.environ.get("HOOK_FORWARD_TIMEOUT_SECONDS", "2"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deny(reason: str) -> None:
    """Write reason to stderr and exit 2 (block the tool call)."""
    print(reason, file=sys.stderr, flush=True)
    sys.exit(2)


def _forward_event(payload: dict) -> None:
    """
    Send a non-PreToolUse event (UserPromptSubmit, Stop, etc.) to the Supervisor
    so it can record prompts and clear sessions.  Best-effort: errors are ignored
    and we always exit 0 — these events can never block a tool call.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        DECIDE_ENDPOINT,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=FORWARD_TIMEOUT_SECONDS) as response:
        response.read()


def _call_supervisor(payload: dict) -> dict:
    """
    POST the hook payload to the Supervisor /decide endpoint.

    Returns the parsed JSON response body.
    Raises urllib.error.URLError / OSError on connection failure.
    Raises json.JSONDecodeError if the response is not valid JSON.

    The timeout passed to urlopen is the full Hook Client deadline.
    This covers both connection and read time — if the Supervisor takes
    longer than DEADLINE_SECONDS to respond (e.g. it is waiting on a human
    approval), the call raises socket.timeout, which is a subclass of OSError.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        DECIDE_ENDPOINT,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=DEADLINE_SECONDS) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # -----------------------------------------------------------------------
    # Top-level try/except: ANY unhandled exception → exit 2 (fail-closed).
    #
    # The only way to fail open from this point is if the Python interpreter
    # itself crashes (segfault, OOM-kill, etc.) before we reach an exit call.
    # That risk is mitigated by keeping this file minimal and import-light.
    # -----------------------------------------------------------------------
    try:
        # -------------------------------------------------------------------
        # 1. Read the hook payload from stdin.
        #    Bob always sends a JSON object on stdin for every hook event.
        # -------------------------------------------------------------------
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw)
        except (json.JSONDecodeError, Exception) as exc:
            _deny(f"SUPERVISOR_DENY: Hook Client could not parse payload: {exc}")

        # -------------------------------------------------------------------
        # 2. Only act on PreToolUse.  For all other hook events (SessionStart,
        #    UserPromptSubmit, PostToolUse, Stop) exit 0 immediately — these
        #    cannot block a tool call and we just want them to succeed silently.
        # -------------------------------------------------------------------
        hook_event = payload.get("hook_event_name", "")
        if hook_event != "PreToolUse":
            # Forward to the Supervisor so it can record prompts and close
            # sessions. Best-effort: any error is ignored and we always exit 0.
            try:
                _forward_event(payload)
            except Exception:
                pass
            sys.exit(0)

        # -------------------------------------------------------------------
        # 3. Call the Supervisor service.
        #    If the service is unreachable or the call times out → deny.
        #    This is the primary fail-closed guarantee: Supervisor down = block.
        # -------------------------------------------------------------------
        try:
            response = _call_supervisor(payload)
        except OSError as exc:
            # Covers: connection refused, timeout, network error.
            _deny(
                f"SUPERVISOR_DENY: Supervisor service unreachable "
                f"({DECIDE_ENDPOINT}): {exc}"
            )
        except json.JSONDecodeError as exc:
            _deny(f"SUPERVISOR_DENY: Supervisor returned invalid JSON: {exc}")

        # -------------------------------------------------------------------
        # 4. Interpret the Supervisor's verdict.
        #
        #    Expected response shape:
        #      { "action": "allow" | "deny" | "escalate_denied",
        #        "reason": "<string>" }
        #
        #    "escalate_denied" means the Escalation Manager timed out or the
        #    human rejected the call — the Supervisor resolves escalation
        #    internally and returns a final allow/deny to us.
        #
        #    Any action other than "allow" → exit 2.
        #    Missing or unrecognised action → exit 2 (fail-closed for unknowns).
        # -------------------------------------------------------------------
        action = response.get("action", "")
        reason = response.get("reason", "No reason provided by Supervisor.")

        if action == "allow":
            sys.exit(0)
        else:
            # deny, escalate_denied, or anything unrecognised
            _deny(f"SUPERVISOR_DENY: {reason}")

    except SystemExit:
        # Re-raise SystemExit so our own sys.exit() calls work correctly.
        raise

    except Exception as exc:
        # Catch-all: any unexpected error → exit 2 (fail-closed).
        _deny(f"SUPERVISOR_DENY: Hook Client unhandled error: {exc}")


if __name__ == "__main__":
    main()
