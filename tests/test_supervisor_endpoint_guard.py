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

from ai_permission_supervisor.schemas.verdict import Action, DecidedBy
from ai_permission_supervisor.supervisor.normalizer import normalize
from ai_permission_supervisor.supervisor.rule_engine import RuleEngine, load_policy

PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())
WORKSPACE = [PROJECT_ROOT]
POLICY_PATH = Path(PROJECT_ROOT) / "ai_permission_supervisor" / "policy" / "policy.yaml"


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


# ---------------------------------------------------------------------------
# The Supervisor's own installation, active policy, and state directory are
# protected even when they sit inside the agent's workspace.
# ---------------------------------------------------------------------------

from ai_permission_supervisor import config


def _file(tool: str, path: str, cwd: str = PROJECT_ROOT) -> dict:
    tool_input = {"path": path}
    if tool == "write_file":
        tool_input.update(content="x", line_count=1)
    return {
        "hook_event_name": "PreToolUse", "tool_use_id": "u-self", "session_id": "s-self",
        "tool_name": tool, "tool_input": tool_input, "cwd": cwd,
    }


@pytest.fixture
def _clean_self_edit(monkeypatch, tmp_path):
    monkeypatch.delenv("APS_ALLOW_SELF_EDIT", raising=False)
    monkeypatch.delenv("POLICY_DIR", raising=False)
    monkeypatch.setenv("APS_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def test_editing_packaged_policy_is_hard_rail_denied(_clean_self_edit):
    target = str(config.PACKAGED_POLICY_DIR / "policy.yaml")
    req = normalize(_file("write_file", target), WORKSPACE)
    assert req.category == "supervisor_path_access"
    verdict = RuleEngine(load_policy(POLICY_PATH)).evaluate(req)
    assert verdict.action == Action.DENY and verdict.decided_by == DecidedBy.HARD_RAIL


def test_relative_path_into_package_is_caught(_clean_self_edit):
    req = normalize(_file("write_file", "ai_permission_supervisor/supervisor/normalizer.py"), WORKSPACE)
    assert req.category == "supervisor_path_access"


def test_reading_service_code_is_denied(_clean_self_edit):
    req = normalize(_file("read_file", str(config.PACKAGE_DIR / "supervisor" / "rule_engine.py")), WORKSPACE)
    assert req.category == "supervisor_path_access"


def test_state_directory_is_protected(_clean_self_edit):
    target = str(_clean_self_edit / "state" / "policy" / "policy.yaml")
    req = normalize(_file("write_file", target, cwd=str(_clean_self_edit)), [str(_clean_self_edit)])
    assert req.category == "supervisor_path_access"


def test_self_edit_opt_out_frees_package_but_not_policy_or_state(_clean_self_edit, monkeypatch):
    monkeypatch.setenv("APS_ALLOW_SELF_EDIT", "1")
    code = normalize(_file("write_file", "ai_permission_supervisor/supervisor/normalizer.py"), WORKSPACE)
    assert code.category == "file_write"
    # The active policy (here the packaged one) stays protected.
    policy = normalize(_file("write_file", str(config.PACKAGED_POLICY_DIR / "policy.yaml")), WORKSPACE)
    assert policy.category == "supervisor_path_access"


def test_ordinary_workspace_files_are_unaffected(_clean_self_edit):
    req = normalize(_file("write_file", "tests/new_test.py"), WORKSPACE)
    assert req.category == "file_write"
