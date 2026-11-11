"""
tests/test_hook_client.py

Tests for hook_client/main.py.

Fail-closed cases are the critical ones — they must never regress:
  - Supervisor service down → exit 2
  - Supervisor returns non-allow → exit 2
  - Supervisor returns invalid JSON → exit 2
  - Payload parse failure → exit 2
  - Non-PreToolUse hook event → exit 0 (passthrough)

Run with:
    python -m pytest tests/test_hook_client.py -v
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOK_CLIENT = str(Path(__file__).parent.parent / "hook_client" / "main.py")
PYTHON = sys.executable


def _run_hook(payload: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the hook client with the given payload on stdin."""
    import os
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [PYTHON, HOOK_CLIENT],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=full_env,
    )


def _make_pre_tool_payload(tool_name: str = "read_file") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "test-session-001",
        "tool_use_id": "test-tool-use-001",
        "tool_name": tool_name,
        "tool_input": {"path": "/tmp/test.txt"},
        "cwd": "/tmp",
    }


# ---------------------------------------------------------------------------
# Fail-closed: Supervisor service down
# ---------------------------------------------------------------------------

class TestSupervisorDown:
    def test_exits_2_when_supervisor_unreachable(self):
        """If the Supervisor service is not running, the hook must exit 2."""
        result = _run_hook(
            _make_pre_tool_payload(),
            env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},  # nothing running here
        )
        assert result.returncode == 2, (
            f"Expected exit 2 (fail-closed), got {result.returncode}. "
            f"stderr: {result.stderr!r}"
        )

    def test_stderr_contains_reason_when_supervisor_down(self):
        """Stderr must contain a reason string so the model is not blocked silently."""
        result = _run_hook(
            _make_pre_tool_payload(),
            env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},
        )
        assert "SUPERVISOR_DENY" in result.stderr, (
            f"Expected 'SUPERVISOR_DENY' in stderr. Got: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Fail-closed: Supervisor returns deny
# ---------------------------------------------------------------------------

class _DenyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"action": "deny", "reason": "test deny reason"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence server logs during tests


def _start_mock_server(handler, port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class TestSupervisorDeny:
    def test_exits_2_on_deny_response(self):
        server = _start_mock_server(_DenyHandler, 19101)
        try:
            result = _run_hook(
                _make_pre_tool_payload(),
                env={"SUPERVISOR_URL": "http://127.0.0.1:19101",
                     "HOOK_DEADLINE_SECONDS": "5"},
            )
            assert result.returncode == 2, (
                f"Expected exit 2 on deny. Got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
            assert "test deny reason" in result.stderr, (
                f"Expected deny reason in stderr. Got: {result.stderr!r}"
            )
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Fail-closed: Supervisor returns invalid JSON
# ---------------------------------------------------------------------------

class _BadJsonHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b"not json at all"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestSupervisorBadJson:
    def test_exits_2_on_invalid_json_response(self):
        server = _start_mock_server(_BadJsonHandler, 19102)
        try:
            result = _run_hook(
                _make_pre_tool_payload(),
                env={"SUPERVISOR_URL": "http://127.0.0.1:19102",
                     "HOOK_DEADLINE_SECONDS": "5"},
            )
            assert result.returncode == 2, (
                f"Expected exit 2 on bad JSON. Got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Fail-closed: unrecognised action value
# ---------------------------------------------------------------------------

class _UnknownActionHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"action": "unknown_future_value", "reason": "unknown"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestUnknownAction:
    def test_exits_2_on_unrecognised_action(self):
        """Any action other than 'allow' must block — fail-closed for unknowns."""
        server = _start_mock_server(_UnknownActionHandler, 19103)
        try:
            result = _run_hook(
                _make_pre_tool_payload(),
                env={"SUPERVISOR_URL": "http://127.0.0.1:19103",
                     "HOOK_DEADLINE_SECONDS": "5"},
            )
            assert result.returncode == 2, (
                f"Expected exit 2 on unknown action. Got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Allow path: Supervisor returns allow
# ---------------------------------------------------------------------------

class _AllowHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"action": "allow", "reason": "auto-allowed"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestSupervisorAllow:
    def test_exits_0_on_allow_response(self):
        server = _start_mock_server(_AllowHandler, 19104)
        try:
            result = _run_hook(
                _make_pre_tool_payload(),
                env={"SUPERVISOR_URL": "http://127.0.0.1:19104",
                     "HOOK_DEADLINE_SECONDS": "5"},
            )
            assert result.returncode == 0, (
                f"Expected exit 0 on allow. Got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# Non-PreToolUse events pass through (exit 0, no supervisor call)
# ---------------------------------------------------------------------------

class TestNonPreToolUse:
    def test_session_start_exits_0(self):
        result = _run_hook(
            {"hook_event_name": "SessionStart", "session_id": "s1"},
            env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},  # nothing running
        )
        assert result.returncode == 0, (
            f"SessionStart should exit 0 (passthrough). Got {result.returncode}."
        )

    def test_post_tool_use_exits_0(self):
        result = _run_hook(
            {"hook_event_name": "PostToolUse", "session_id": "s1",
             "tool_name": "read_file", "tool_use_id": "u1"},
            env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},
        )
        assert result.returncode == 0, (
            f"PostToolUse should exit 0 (passthrough). Got {result.returncode}."
        )

    def test_stop_exits_0(self):
        result = _run_hook(
            {"hook_event_name": "Stop", "session_id": "s1"},
            env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},
        )
        assert result.returncode == 0, (
            f"Stop should exit 0 (passthrough). Got {result.returncode}."
        )

    def test_user_prompt_submit_exits_0(self):
        result = _run_hook(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
             "prompt": "do something"},
            env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},
        )
        assert result.returncode == 0, (
            f"UserPromptSubmit should exit 0 (passthrough). Got {result.returncode}."
        )


# ---------------------------------------------------------------------------
# Malformed stdin
# ---------------------------------------------------------------------------

class TestMalformedPayload:
    def test_exits_2_on_invalid_json_stdin(self):
        """If Bob sends garbage on stdin, the hook must exit 2 (fail-closed)."""
        import os
        result = subprocess.run(
            [PYTHON, HOOK_CLIENT],
            input="this is not json {{{",
            capture_output=True,
            text=True,
            env={**os.environ, "SUPERVISOR_URL": "http://127.0.0.1:19999"},
        )
        assert result.returncode == 2, (
            f"Expected exit 2 on malformed stdin. Got {result.returncode}."
        )


# ---------------------------------------------------------------------------
# Long-hold: Supervisor holds the connection open past the Hook Client deadline
# ---------------------------------------------------------------------------
# Plan note (ai-permission-supervisor-plan.md, Step 3.6 + Open Items):
#   "Long-hold (>10s) test still pending — do once before wiring the
#    Escalation Manager."
# The Escalation Manager is now wired. This test validates the timeout
# ordering invariant:
#   escalation_timeout (120s) < Hook Client DEADLINE (130s) < Bob hook timeout (150s)
# Using a short deadline (2s) so the test completes quickly.
# The server holds the connection open for longer than the deadline, simulating
# a Supervisor that is alive but unresponsive (e.g. blocked waiting for a human
# who never responds and whose escalation timeout is also broken).

class _HoldHandler(BaseHTTPRequestHandler):
    """Accept the connection but never send a response until the client drops."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        # Hold open — wait far longer than the Hook Client deadline.
        # The client's urlopen timeout will fire first and close the socket.
        import time as _time
        _time.sleep(30)

    def log_message(self, *args):
        pass


class TestLongHold:
    def test_exits_2_when_supervisor_holds_connection_open(self):
        """
        Done-condition for plan Step 3.6 long-hold test.

        The Supervisor accepts the connection but never replies (simulating a
        hung escalation wait). The Hook Client's own deadline must fire before
        Bob's hook timeout would, and the result must be exit 2.

        Uses HOOK_DEADLINE_SECONDS=2 so the test completes in ~2s.
        Validates that:
          - A live-but-unresponsive Supervisor does not leave Bob hanging.
          - The Hook Client's urlopen timeout (DEADLINE_SECONDS) is the
            actual enforcement boundary, not Bob's outer hook timeout.
        """
        server = _start_mock_server(_HoldHandler, 19105)
        try:
            import time as _time
            start = _time.monotonic()
            result = _run_hook(
                _make_pre_tool_payload(),
                env={
                    "SUPERVISOR_URL": "http://127.0.0.1:19105",
                    "HOOK_DEADLINE_SECONDS": "2",
                },
            )
            elapsed = _time.monotonic() - start

            assert result.returncode == 2, (
                f"Expected exit 2 on hung Supervisor. Got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
            assert "SUPERVISOR_DENY" in result.stderr, (
                f"Expected SUPERVISOR_DENY in stderr. Got: {result.stderr!r}"
            )
            # Must have returned within deadline + 1s buffer (not hung for 30s)
            assert elapsed < 4.0, (
                f"Hook Client took too long to exit ({elapsed:.2f}s). "
                f"Deadline enforcement may be broken."
            )
        finally:
            server.shutdown()
