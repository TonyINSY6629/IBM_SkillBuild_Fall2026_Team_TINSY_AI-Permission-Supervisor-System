"""
tests/test_git_internals_guard.py — policy rule hr-007 and the audit log folder.

git runs commands named in .git/config (core.fsmonitor, diff.external) and in
.git/hooks. Without hr-007 an agent could write .git/config (auto-allowed by
al-002) and then run "git status" (auto-allowed by al-003): code would run
with no human approval. Writes inside .git/ are now denied; normal files such
as .gitignore and .github/ are not affected.

The audit log folder is protected even when AUDIT_LOG_PATH moves it into the
workspace.

Each case runs the real Normalizer and the real policy.yaml.

Run with:
    python -m pytest tests/test_git_internals_guard.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_permission_supervisor.schemas.verdict import Action
from ai_permission_supervisor.supervisor.normalizer import normalize
from ai_permission_supervisor.supervisor.rule_engine import RuleEngine, load_policy

PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())
ENGINE = RuleEngine(load_policy(Path(PROJECT_ROOT) / "ai_permission_supervisor" / "policy" / "policy.yaml"))


def _verdict(tool_name: str, path: str):
    req = normalize({
        "hook_event_name": "PreToolUse",
        "tool_use_id": "u-hr7",
        "session_id": "s-hr7",
        "tool_name": tool_name,
        "tool_input": {"path": path},
        "cwd": PROJECT_ROOT,
    }, [PROJECT_ROOT])
    return ENGINE.evaluate(req)


@pytest.mark.parametrize("path", [
    ".git/config",
    ".git\\hooks\\pre-commit",
    str(Path(PROJECT_ROOT) / ".git" / "config"),
])
def test_writes_inside_git_folder_are_denied(path):
    verdict = _verdict("write_file", path)
    assert verdict.action == Action.DENY
    assert verdict.matched_rule_id == "hr-007"


@pytest.mark.parametrize("path", [".gitignore", ".github/workflows/ci.yml", "notes.txt"])
def test_normal_workspace_writes_are_still_allowed(path):
    verdict = _verdict("write_file", path)
    assert verdict.action == Action.ALLOW


def test_moved_audit_log_cannot_be_read_or_written(monkeypatch, tmp_path):
    log_path = Path(PROJECT_ROOT) / "logs" / "audit_log.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log_path))
    assert _verdict("read_file", str(log_path)).action == Action.DENY
    assert _verdict("write_file", str(log_path)).action == Action.DENY