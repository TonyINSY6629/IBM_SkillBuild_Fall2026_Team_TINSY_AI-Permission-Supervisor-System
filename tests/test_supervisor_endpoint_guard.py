"""
tests/test_supervisor_endpoint_guard.py

The agent must not be able to talk to the Supervisor's own HTTP API.
A shell command such as

    curl -X POST http://127.0.0.1:9001/decide -d '{"hook_event_name": "UserPromptSubmit", ...}'

could forge hook events (rewrite the recorded user task, or clear session
context with a fake Stop). The normalizer classifies such commands as
supervisor_path_access, so hard rail hr-003 denies them end to end.

Run with:
    python -m pytest tests/test_supervisor_endpoint_guard.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from schemas.verdict import Action, DecidedBy
from supervisor.normalizer import normalize
from supervisor.rule_engine import RuleEngine, load_policy

PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())
WORKSPACE = [PROJECT_ROOT]
POLICY_PATH = Path(PROJECT_ROOT) / "policy" / "policy.yaml"


def _exec(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_use_id": "u-guard",
        "session_id": "s-guard",
        "tool_name": "execute_command",
        "tool_input": {"command": command},
        "cwd": PROJECT_ROOT,
    }


@pytest.fixture(autouse=True)
def _default_port(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_URL", raising=False)
    monkeypatch.setenv("SUPERVISOR_PORT", "9001")


@pytest.mark.parametrize("command", [
    'curl -X POST http://127.0.0.1:9001/decide -d "{}"',
    "curl http://localhost:9001/escalation/abc/allow -X POST",
    "Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9001/decide",
    'python -c "import urllib.request; urllib.request.urlopen(\'http://LOCALHOST:9001/health\')"',
    "wget -qO- http://0.0.0.0:9001/health",
])
def test_commands_to_supervisor_endpoint_are_supervisor_access(command):
    req = normalize(_exec(command), WORKSPACE)
    assert req.category == "supervisor_path_access"


def test_supervisor_endpoint_command_is_hard_rail_denied():
    req = normalize(_exec('curl -X POST http://127.0.0.1:9001/decide -d "{}"'), WORKSPACE)
    verdict = RuleEngine(load_policy(POLICY_PATH)).evaluate(req)
    assert verdict.action == Action.DENY
    assert verdict.decided_by == DecidedBy.HARD_RAIL
    assert verdict.matched_rule_id == "hr-003"


def test_custom_port_from_supervisor_url_is_guarded(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_URL", "http://127.0.0.1:9123")
    req = normalize(_exec("curl http://127.0.0.1:9123/decide"), WORKSPACE)
    assert req.category == "supervisor_path_access"


def test_other_local_ports_are_not_affected():
    # A dev server on another port is ordinary network traffic, not the Supervisor.
    req = normalize(_exec("curl http://localhost:3000/api"), WORKSPACE)
    assert req.category == "network_request"


def test_cli_state_directory_is_supervisor_path():
    req = normalize(_exec("type C:\\Users\\me\\.ai_permission_supervisor\\supervisor.pid"), WORKSPACE)
    assert req.category == "supervisor_path_access"
