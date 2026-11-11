#!/usr/bin/env python3
"""
Step 1 — Logging-only probe hook.

Registered for ALL hook events (SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse, Stop).  It reads the JSON payload from stdin, appends a
timestamped record to the probe log, then exits 0 — never blocking anything.

Log location: ~/supervisor_probe/probe_log.jsonl
Each line is one JSON object:  { "logged_at": "<iso8601>", "payload": { ... } }

What this probe answers (see plan Step 1):
  1. Real tool_name values and tool_input shapes
  2. Whether PreToolUse fires before Bob's own approval prompt
  3. Whether PostToolUse fires even when a tool is blocked (it should not)
  4. Whether a long-timeout hook causes Bob to misbehave
  5. Whether PreToolUse fires for MCP tool calls

Usage (set in ~/.bob/settings/settings.json — see probe_settings.json):
  python3 ~/supervisor_probe/probe.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path.home() / "supervisor_probe"
LOG_FILE = LOG_DIR / "probe_log.jsonl"


def main() -> None:
    # Outer try/except guarantees exit 0 no matter what goes wrong.
    # This script must never crash or exit non-zero — any non-2 non-zero
    # exit from a hook is a non-blocking failure that Bob logs and ignores,
    # but we want zero surprises and a clean log.
    try:
        # Read payload from stdin — Bob always sends JSON on stdin.
        raw = sys.stdin.read()

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Record the parse error so we can investigate, but keep going.
            payload = {"parse_error": str(exc), "raw": raw}

        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }

        # Ensure log directory exists (outside workspace, inside home dir).
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        # Append one JSON line — one record per hook invocation.
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    except Exception:
        # Swallow everything — logging failure must never affect Bob.
        pass

    # Always exit 0.  This probe never blocks anything.
    sys.exit(0)


if __name__ == "__main__":
    main()
