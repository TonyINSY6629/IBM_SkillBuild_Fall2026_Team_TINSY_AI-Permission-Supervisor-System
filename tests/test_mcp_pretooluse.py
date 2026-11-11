"""
tests/test_mcp_pretooluse.py — MCP PreToolUse coverage probe

Open item from ai-permission-supervisor-plan.md:
  "Confirm PreToolUse fires for MCP tool calls."
  No MCP tools were used during Step 1 probe logging, so the 0 MCP calls
  in the probe log do not prove PreToolUse skips them.

What this file does
-------------------
Since we cannot fire a real MCP call without a running Bob session, this
test validates the Hook Client's behaviour for MCP-shaped payloads:

  1. MCP tool call payload (tool_name="mcp__server__tool") → Hook Client
     must treat it identically to a built-in tool — forwarded to Supervisor,
     fail-closed if Supervisor is down.

  2. Documents the open item and the manual test procedure to resolve it.

These tests use the same mock-server pattern as test_hook_client.py.

Manual verification step (do once with a live Bob session)
----------------------------------------------------------
1. Open Bob, enable an MCP server (any tool will do).
2. Ask Bob to call an MCP tool.
3. Check the probe log (hook_client/probe.py output) for an entry where
   tool_name starts with "mcp__".
4. If found: PreToolUse fires for MCP calls → Gateway is not needed for MVP.
   Update the plan's open item accordingly.
5. If no MCP entry appears even though the call ran: PreToolUse is skipped
   for MCP → Gateway remains required.

Run with:
    python -m pytest tests/test_mcp_pretooluse.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HOOK_CLIENT = str(Path(__file__).parent.parent / "hook_client" / "main.py")
PYTHON = sys.executable


def _run_hook(payload: dict, env: dict | None = None) -> subprocess.CompletedProcess:
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


def _mcp_pre_tool_payload(tool_name: str = "mcp__filesystem__read_file") -> dict:
    """
    Simulated PreToolUse payload for an MCP tool call.
    Bob uses the mcp__<server>__<tool> naming convention for MCP tools.
    The tool_input shape varies by server; filesystem tools use 'path'.
    """
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "test-mcp-session-001",
        "tool_use_id": "test-mcp-tool-use-001",
        "tool_name": tool_name,
        "tool_input": {"path": "/tmp/test.txt"},
        "cwd": "/tmp",
    }


# ---------------------------------------------------------------------------
# Helpers: mock servers
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


class _DenyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"action": "deny", "reason": "mcp deny test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start_mock_server(handler, port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Tests: MCP payloads are treated identically to built-in tool payloads
# ---------------------------------------------------------------------------

class TestMcpPayloadAllow:
    def test_mcp_tool_exits_0_on_allow(self):
        """
        An MCP-named tool call forwarded to the Supervisor and allowed
        must exit 0 — same as a built-in tool.
        """
        server = _start_mock_server(_AllowHandler, 19201)
        try:
            result = _run_hook(
                _mcp_pre_tool_payload("mcp__filesystem__read_file"),
                env={"SUPERVISOR_URL": "http://127.0.0.1:19201",
                     "HOOK_DEADLINE_SECONDS": "5"},
            )
            assert result.returncode == 0, (
                f"MCP tool allow should exit 0. Got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
        finally:
            server.shutdown()


class TestMcpPayloadDeny:
    def test_mcp_tool_exits_2_on_deny(self):
        """
        An MCP tool call denied by the Supervisor must exit 2 — fail-closed
        behaves identically for MCP tools and built-in tools.
        """
        server = _start_mock_server(_DenyHandler, 19202)
        try:
            result = _run_hook(
                _mcp_pre_tool_payload("mcp__github__create_issue"),
                env={"SUPERVISOR_URL": "http://127.0.0.1:19202",
                     "HOOK_DEADLINE_SECONDS": "5"},
            )
            assert result.returncode == 2, (
                f"MCP tool deny should exit 2. Got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
            assert "mcp deny test" in result.stderr, (
                f"Deny reason should be in stderr. Got: {result.stderr!r}"
            )
        finally:
            server.shutdown()


class TestMcpPayloadSupervisorDown:
    def test_mcp_tool_exits_2_when_supervisor_unreachable(self):
        """
        Supervisor down with an MCP tool call in flight → must exit 2.
        The hook must be fail-closed for MCP tools just as for built-in tools.
        """
        result = _run_hook(
            _mcp_pre_tool_payload("mcp__filesystem__write_file"),
            env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},
        )
        assert result.returncode == 2, (
            f"MCP tool with supervisor down should exit 2. Got {result.returncode}."
        )
        assert "SUPERVISOR_DENY" in result.stderr, (
            f"Expected SUPERVISOR_DENY in stderr. Got: {result.stderr!r}"
        )


class TestMcpVariousToolNames:
    """
    Verify a range of mcp__<server>__<tool> name patterns all route correctly.
    The Hook Client must not special-case or short-circuit MCP names.
    """

    MCP_TOOLS = [
        "mcp__filesystem__read_file",
        "mcp__filesystem__write_file",
        "mcp__github__create_issue",
        "mcp__github__push_files",
        "mcp__postgres__query",
        "mcp__brave_search__search",
    ]

    def test_all_mcp_tool_names_exit_2_when_supervisor_down(self):
        """All MCP tool name patterns fail-closed when Supervisor is unreachable."""
        for tool_name in self.MCP_TOOLS:
            result = _run_hook(
                _mcp_pre_tool_payload(tool_name),
                env={"SUPERVISOR_URL": "http://127.0.0.1:19999"},
            )
            assert result.returncode == 2, (
                f"Tool '{tool_name}' with supervisor down should exit 2. "
                f"Got {result.returncode}."
            )


# ---------------------------------------------------------------------------
# Documented manual step — not a runnable test
# ---------------------------------------------------------------------------

def test_mcp_open_item_documented():
    """
    This test always passes. It exists to document the open item and the
    manual verification procedure in the test suite output.

    OPEN ITEM: Confirm PreToolUse fires for real MCP tool calls in Bob.
    STATUS:    Unresolved — no MCP calls were logged during Step 1 probe.

    To resolve:
      1. Start probe hook (hook_client/probe.py) in Bob settings.
      2. Call any MCP tool from a Bob session.
      3. Check logs/ for an entry with tool_name starting "mcp__".
      4a. If found → PreToolUse fires for MCP; Gateway is not needed for MVP.
          Update plan open item, mark resolved.
      4b. If not found → PreToolUse is skipped for MCP; Gateway is required.
          Update plan open item, begin Gateway implementation.
    """
    assert True, "Open item documented — see docstring for manual steps."
