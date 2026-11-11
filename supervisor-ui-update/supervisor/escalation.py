"""
supervisor/escalation.py — Escalation Manager

Presents a tool-call for human approval and waits for a response.

Architecture:
  The Supervisor service hosts a lightweight approval UI at:
    GET  /escalation/{escalation_id}       → HTML approval page
    POST /escalation/{escalation_id}/allow → human approved
    POST /escalation/{escalation_id}/deny  → human denied

  When a tool call requires escalation:
    1. EscalationManager.request() creates a pending escalation record.
    2. It prints the approval URL to stderr (visible in the terminal where
       the Supervisor is running, and forwarded to the model via the Hook
       Client's stderr on deny).
    3. It blocks for up to ESCALATION_TIMEOUT_SECONDS waiting for a human
       response via the HTTP endpoints.
    4. If the human responds → return their decision (allow or deny).
    5. If timeout fires first → return deny.

  Timeout ordering invariant (must be maintained in settings):
    ESCALATION_TIMEOUT (120 s) < HOOK_CLIENT_DEADLINE (130 s) < Bob hook timeout (150 s)

UI mechanism:
  Currently: local HTTP page served by the Supervisor.
  The URL is printed to stderr and logged. Human opens it in a browser.
  Future: can be replaced with an IDE extension, OS notification, or
  any other mechanism — the EscalationManager interface does not change.

Public API:
  EscalationManager(base_url, timeout_seconds)
  EscalationManager.request(escalation_id, tool_name, category, reason) -> EscalationResult
  EscalationResult.action  — "allow" | "deny"
  EscalationResult.source  — "human_allow" | "human_deny" | "timeout"

  FastAPI router (to be included in supervisor/app.py):
    escalation_router  — mounts GET/POST endpoints under /escalation
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
import webbrowser
from urllib.parse import urlencode
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("supervisor.escalation")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ESCALATION_TIMEOUT_SECONDS: float = float(
    os.environ.get("ESCALATION_TIMEOUT_SECONDS", "120")
)

# Base URL of the Supervisor service — used to build the approval URL
# shown to the human.
SUPERVISOR_BASE_URL: str = os.environ.get(
    "SUPERVISOR_URL", "http://127.0.0.1:9001"
)

# URL of the React escalation pop-up (UI/escalation-popup-src). The supervisor
# opens  {POPUP_URL}/?id=<escalation_id>&api=<SUPERVISOR_URL>  when it escalates.
# Use the local dev server (bun run dev → http://localhost:8080) or the published
# Lovable URL. Set ESCALATION_POPUP_URL="" to disable and fall back to the HTML page.
POPUP_URL: str = os.environ.get("ESCALATION_POPUP_URL", "http://localhost:8080")
OPEN_BROWSER: bool = os.environ.get("ESCALATION_OPEN_BROWSER", "1") not in ("0", "false", "")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EscalationResult:
    escalation_id: str
    action: str          # "allow" | "deny"
    source: str          # "human_allow" | "human_deny" | "timeout"
    decided_at: float = field(default_factory=time.time)


@dataclass
class _PendingEscalation:
    escalation_id: str
    tool_name: str
    category: str
    reason: str
    details: dict = field(default_factory=dict)
    timeout: float = ESCALATION_TIMEOUT_SECONDS
    note: str = ""
    created_at: float = field(default_factory=time.time)
    # Event is set when a human responds (or timeout fires from outside).
    _event: threading.Event = field(default_factory=threading.Event)
    _result: Optional[EscalationResult] = None


# ---------------------------------------------------------------------------
# EscalationManager
# ---------------------------------------------------------------------------

class EscalationManager:
    """
    Manages pending escalations and provides the blocking .request() call.

    One instance is shared across the Supervisor service.
    Thread-safe: multiple tool calls can be escalated simultaneously.
    """

    def __init__(
        self,
        base_url: str = SUPERVISOR_BASE_URL,
        timeout_seconds: float = ESCALATION_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingEscalation] = {}

    def request(
        self,
        escalation_id: str,
        tool_name: str,
        category: str,
        reason: str,
        details: Optional[dict] = None,
    ) -> EscalationResult:
        """
        Present a tool call for human approval and block until resolved.

        Returns EscalationResult with action="allow" if human approved,
        or action="deny" if human denied or timeout fired.

        This call blocks for up to self._timeout seconds.
        Callers (policy_engine.py) must ensure this is called from a
        thread pool worker, not the async event loop directly.
        """
        pending = _PendingEscalation(
            escalation_id=escalation_id,
            tool_name=tool_name,
            category=category,
            reason=reason,
            details=details or {},
            timeout=self._timeout,
        )

        with self._lock:
            self._pending[escalation_id] = pending

        approval_url = self.popup_url(escalation_id)
        if OPEN_BROWSER:
            # Open in a background thread so a slow browser launch never delays the timer.
            threading.Thread(
                target=_open_quietly, args=(approval_url,), daemon=True
            ).start()

        # Print URL to stderr — this is forwarded to the model by the
        # Hook Client's stderr channel on deny, and also visible in the
        # terminal where the Supervisor is running.
        print(
            f"\n[SUPERVISOR ESCALATION] Human approval required:\n"
            f"  Tool:     {tool_name}\n"
            f"  Category: {category}\n"
            f"  Reason:   {reason}\n"
            f"  Approve / Deny at: {approval_url}\n"
            f"  Timeout in {int(self._timeout)}s\n",
            flush=True,
        )
        log.warning(
            "escalation: id=%s tool=%s category=%s url=%s timeout=%ss",
            escalation_id, tool_name, category, approval_url, int(self._timeout),
        )

        # Block until the human responds or the timeout fires.
        responded = pending._event.wait(timeout=self._timeout)

        with self._lock:
            self._pending.pop(escalation_id, None)

        if responded and pending._result is not None:
            return pending._result

        # Timeout — deny.
        log.warning(
            "escalation: id=%s TIMEOUT after %ss → deny", escalation_id, self._timeout
        )
        return EscalationResult(
            escalation_id=escalation_id,
            action="deny",
            source="timeout",
        )

    def popup_url(self, escalation_id: str) -> str:
        if not POPUP_URL:
            return f"{self._base_url}/escalation/{escalation_id}"
        q = urlencode({"id": escalation_id, "api": self._base_url})
        return f"{POPUP_URL.rstrip('/')}/?{q}"

    def resolve(self, escalation_id: str, approved: bool, note: str = "") -> bool:
        """
        Called by the HTTP endpoints when the human clicks Allow or Deny.

        Returns True if the escalation was found and resolved,
        False if it was not found (already timed out or already resolved).
        """
        with self._lock:
            pending = self._pending.get(escalation_id)

        if pending is None:
            log.warning(
                "escalation: resolve called for unknown id=%s (timed out?)",
                escalation_id,
            )
            return False

        source = "human_allow" if approved else "human_deny"
        pending.note = note[:500]
        pending._result = EscalationResult(
            escalation_id=escalation_id,
            action="allow" if approved else "deny",
            source=source,
        )
        pending._event.set()
        log.info(
            "escalation: id=%s resolved source=%s action=%s",
            escalation_id, source, pending._result.action,
        )
        return True

    def get_pending(self, escalation_id: str) -> Optional[_PendingEscalation]:
        """Return pending escalation details for rendering the approval UI."""
        with self._lock:
            return self._pending.get(escalation_id)


# ---------------------------------------------------------------------------
# FastAPI router — approval UI endpoints
# Imported and included in supervisor/app.py
# ---------------------------------------------------------------------------

def _open_quietly(url: str) -> None:
    try:
        webbrowser.open(url, new=1)
    except Exception as exc:  # never let the UI break the decision path
        log.warning("escalation: could not open browser: %s", exc)


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

escalation_router = APIRouter(prefix="/escalation", tags=["escalation"])

# Module-level singleton — replaced by app.py with the shared instance.
_manager: Optional[EscalationManager] = None


def set_manager(manager: EscalationManager) -> None:
    """Called by app.py at startup to inject the shared EscalationManager."""
    global _manager
    _manager = manager


@escalation_router.get("/{escalation_id}", response_class=HTMLResponse)
async def escalation_page(escalation_id: str) -> HTMLResponse:
    """Render the human-approval page."""
    if _manager is None:
        return HTMLResponse("<h1>Escalation Manager not initialised.</h1>", status_code=503)

    pending = _manager.get_pending(escalation_id)
    if pending is None:
        return HTMLResponse(
            _render_page(
                escalation_id,
                resolved=True,
                tool_name="",
                category="",
                reason="This escalation has already been resolved or timed out.",
            ),
            status_code=410,
        )

    elapsed = int(time.time() - pending.created_at)
    remaining = max(0, int(ESCALATION_TIMEOUT_SECONDS) - elapsed)

    return HTMLResponse(
        _render_page(
            escalation_id,
            resolved=False,
            tool_name=pending.tool_name,
            category=pending.category,
            reason=pending.reason,
            remaining=remaining,
        )
    )


@escalation_router.get("/{escalation_id}/data")
async def escalation_data(escalation_id: str) -> JSONResponse:
    """JSON payload for the React pop-up."""
    if _manager is None:
        return JSONResponse({"error": "Manager not initialised."}, status_code=503)
    pending = _manager.get_pending(escalation_id)
    if pending is None:
        return JSONResponse({"error": "resolved or timed out"}, status_code=410)
    elapsed = time.time() - pending.created_at
    return JSONResponse({
        "escalation_id": escalation_id,
        "tool_name": pending.tool_name,
        "category": pending.category,
        "reason": pending.reason,
        "timeout": int(pending.timeout),
        "remaining": max(0, int(pending.timeout - elapsed)),
        "details": pending.details,
    })


async def _note(request: Request) -> str:
    try:
        body = await request.json()
        return str(body.get("note", "")) if isinstance(body, dict) else ""
    except Exception:
        return ""


@escalation_router.post("/{escalation_id}/allow")
async def escalation_allow(escalation_id: str, request: Request) -> JSONResponse:
    """Human clicked Allow."""
    if _manager is None:
        return JSONResponse({"ok": False, "error": "Manager not initialised."}, status_code=503)
    found = _manager.resolve(escalation_id, approved=True, note=await _note(request))
    return JSONResponse({"ok": found, "action": "allow"})


@escalation_router.post("/{escalation_id}/deny")
async def escalation_deny(escalation_id: str, request: Request) -> JSONResponse:
    """Human clicked Deny."""
    if _manager is None:
        return JSONResponse({"ok": False, "error": "Manager not initialised."}, status_code=503)
    found = _manager.resolve(escalation_id, approved=False, note=await _note(request))
    return JSONResponse({"ok": found, "action": "deny"})


# ---------------------------------------------------------------------------
# HTML page renderer
# ---------------------------------------------------------------------------

def _render_page(
    escalation_id: str,
    resolved: bool,
    tool_name: str,
    category: str,
    reason: str,
    remaining: int = 0,
) -> str:
    if resolved:
        body = f"""
        <h1 style="color:#888">Escalation Resolved</h1>
        <p>{reason}</p>
        """
    else:
        body = f"""
        <h1>⚠ Supervisor Approval Required</h1>
        <table>
          <tr><th>Tool</th><td><code>{tool_name}</code></td></tr>
          <tr><th>Category</th><td><code>{category}</code></td></tr>
          <tr><th>Reason</th><td>{reason}</td></tr>
          <tr><th>Expires in</th><td id="countdown">{remaining}s</td></tr>
        </table>
        <br>
        <form method="POST" action="/escalation/{escalation_id}/allow" style="display:inline">
          <button type="submit"
            style="background:#2a9d2a;color:#fff;padding:10px 28px;font-size:16px;
                   border:none;border-radius:4px;cursor:pointer;margin-right:12px">
            ✅ Allow
          </button>
        </form>
        <form method="POST" action="/escalation/{escalation_id}/deny" style="display:inline">
          <button type="submit"
            style="background:#c0392b;color:#fff;padding:10px 28px;font-size:16px;
                   border:none;border-radius:4px;cursor:pointer">
            ❌ Deny
          </button>
        </form>
        <script>
          var t = {remaining};
          var el = document.getElementById('countdown');
          var iv = setInterval(function() {{
            t--;
            if (t <= 0) {{ clearInterval(iv); el.textContent = 'Expired'; }}
            else {{ el.textContent = t + 's'; }}
          }}, 1000);
        </script>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Supervisor Escalation</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px;
            margin: 60px auto; padding: 0 20px; color: #1f2328; }}
    h1   {{ font-size: 1.4em; }}
    table{{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
    th   {{ text-align: left; padding: 6px 12px; background: #f7f8fa;
            width: 120px; border: 1px solid #e5e7eb; }}
    td   {{ padding: 6px 12px; border: 1px solid #e5e7eb; }}
    code {{ background: #f0f0f0; padding: 2px 5px; border-radius: 3px; }}
  </style>
</head>
<body>
  {body}
  <hr style="margin-top:40px; border-color:#e5e7eb">
  <p style="font-size:11px;color:#888;text-align:center">
    AI Permission Supervisor &mdash; escalation id: {escalation_id}
  </p>
</body>
</html>"""
