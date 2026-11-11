"""
tests/test_rule_engine.py

Tests for supervisor/rule_engine.py and supervisor/policy_engine.py.

Done-condition (from plan Step 3.5):
  - supervisor-path write → hard-rail deny
  - workspace file read   → allow
  - unknown / shell_exec  → escalate

Also tests:
  - Every hard rail in policy.yaml fires correctly
  - allow_rules fire for workspace reads and writes
  - Default escalate fires when nothing matches
  - PolicyEngine writes an audit entry before returning
  - PolicyEngine raises PolicyEngineError when audit write fails
  - Condition matching edge cases (target_pattern, omitted fields)

Run with:
    python -m pytest tests/test_rule_engine.py -v
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from schemas.permission_request import ActionType, PermissionRequest
from schemas.verdict import Action, DecidedBy, Label
from supervisor.audit import AuditLog
from supervisor.policy_engine import PolicyEngine, PolicyEngineError
from supervisor.rule_engine import RuleEngine, load_policy

PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())
POLICY_PATH = Path(PROJECT_ROOT) / "policy" / "policy.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(
    tool_name: str = "read_file",
    action_type: ActionType = ActionType.READ,
    category: str = "file_read",
    targets: list[str] | None = None,
    inside_workspace: bool = True,
    touches_secrets: bool = False,
    tool_use_id: str = "u-001",
    session_id: str = "s-001",
    cwd: str = PROJECT_ROOT,
) -> PermissionRequest:
    return PermissionRequest(
        tool_use_id=tool_use_id,
        session_id=session_id,
        tool_name=tool_name,
        tool_input={},
        cwd=cwd,
        action_type=action_type,
        category=category,
        targets=targets if targets is not None else [PROJECT_ROOT + "/foo.py"],
        inside_workspace=inside_workspace,
        touches_secrets=touches_secrets,
    )


def _engine() -> RuleEngine:
    return RuleEngine(load_policy(POLICY_PATH))


def _audit(tmp_path: Path) -> AuditLog:
    return AuditLog(log_path=tmp_path / "audit_log.jsonl")


# ---------------------------------------------------------------------------
# RuleEngine — hard rails
# ---------------------------------------------------------------------------

class TestHardRails:
    def test_supervisor_path_write_is_hard_rail_deny(self):
        """Done-condition: supervisor-path write → hard-rail deny."""
        req = _req(
            tool_name="write_file",
            action_type=ActionType.WRITE,
            category="supervisor_path_access",
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.DENY
        assert verdict.decided_by == DecidedBy.HARD_RAIL
        assert verdict.matched_rule_id == "hr-001"

    def test_supervisor_path_read_is_hard_rail_deny(self):
        req = _req(
            tool_name="read_file",
            action_type=ActionType.READ,
            category="supervisor_path_access",
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.DENY
        assert verdict.decided_by == DecidedBy.HARD_RAIL
        assert verdict.matched_rule_id == "hr-002"

    def test_supervisor_path_exec_is_hard_rail_deny(self):
        req = _req(
            tool_name="execute_command",
            action_type=ActionType.EXEC,
            category="supervisor_path_access",
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.DENY
        assert verdict.decided_by == DecidedBy.HARD_RAIL
        assert verdict.matched_rule_id == "hr-003"

    def test_outside_workspace_write_is_hard_rail_deny(self):
        req = _req(
            tool_name="write_file",
            action_type=ActionType.WRITE,
            category="workspace_outside_write",
            inside_workspace=False,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.DENY
        assert verdict.decided_by == DecidedBy.HARD_RAIL
        assert verdict.matched_rule_id == "hr-005"

    def test_secret_write_is_hard_rail_deny(self):
        req = _req(
            tool_name="write_file",
            action_type=ActionType.WRITE,
            category="file_write",
            touches_secrets=True,
            inside_workspace=True,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.DENY
        assert verdict.decided_by == DecidedBy.HARD_RAIL
        assert verdict.matched_rule_id == "hr-006"

    def test_bob_settings_write_is_hard_rail_deny(self):
        """hr-004: target_pattern **/.bob/** must match .bob paths."""
        bob_settings = str(Path.home() / ".bob" / "settings" / "settings.json")
        req = _req(
            tool_name="write_file",
            action_type=ActionType.WRITE,
            category="file_write",        # NOT supervisor_path_access
            targets=[bob_settings],
            inside_workspace=False,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.DENY
        assert verdict.decided_by == DecidedBy.HARD_RAIL
        assert verdict.matched_rule_id == "hr-004"

    def test_hard_rail_deny_has_reason(self):
        req = _req(
            action_type=ActionType.WRITE,
            category="supervisor_path_access",
        )
        verdict = _engine().evaluate(req)
        assert verdict.reason  # must not be empty
        assert "Blocked" in verdict.reason

    def test_hard_rail_deny_has_policy_version(self):
        req = _req(action_type=ActionType.WRITE, category="supervisor_path_access")
        verdict = _engine().evaluate(req)
        assert verdict.policy_version == "2026-11-04-v1"


# ---------------------------------------------------------------------------
# RuleEngine — allow rules
# ---------------------------------------------------------------------------

class TestAllowRules:
    def test_workspace_file_read_is_allowed(self):
        """Done-condition: workspace file read → allow."""
        req = _req(
            tool_name="read_file",
            action_type=ActionType.READ,
            category="file_read",
            inside_workspace=True,
            touches_secrets=False,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.ALLOW
        assert verdict.decided_by == DecidedBy.RULE
        assert verdict.matched_rule_id == "al-001"
        assert verdict.label == Label.SAFE

    def test_workspace_file_write_is_allowed(self):
        req = _req(
            tool_name="write_file",
            action_type=ActionType.WRITE,
            category="file_write",
            inside_workspace=True,
            touches_secrets=False,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.ALLOW
        assert verdict.decided_by == DecidedBy.RULE
        assert verdict.matched_rule_id == "al-002"
        assert verdict.label == Label.NEUTRAL

    def test_secret_read_does_not_allow(self):
        """al-001 requires touches_secrets=False — secret reads fall to default."""
        req = _req(
            action_type=ActionType.READ,
            category="file_read",
            inside_workspace=True,
            touches_secrets=True,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.ESCALATE

    def test_outside_workspace_read_does_not_allow(self):
        """al-001 requires inside_workspace=True."""
        req = _req(
            action_type=ActionType.READ,
            category="file_read",
            inside_workspace=False,
            touches_secrets=False,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.ESCALATE


# ---------------------------------------------------------------------------
# RuleEngine — default escalate
# ---------------------------------------------------------------------------

class TestDefaultEscalate:
    def test_shell_exec_escalates(self):
        """Done-condition: unknown/shell_exec → escalate."""
        req = _req(
            tool_name="execute_command",
            action_type=ActionType.EXEC,
            category="shell_exec",
            inside_workspace=False,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.ESCALATE
        assert verdict.decided_by == DecidedBy.RULE
        assert verdict.matched_rule_id is None

    def test_network_request_escalates(self):
        req = _req(
            tool_name="execute_command",
            action_type=ActionType.EXEC,
            category="network_request",
            inside_workspace=False,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.ESCALATE

    def test_unclassified_escalates(self):
        req = _req(
            tool_name="update_todo_list",
            action_type=ActionType.OTHER,
            category="unclassified",
            targets=[],
            inside_workspace=True,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.ESCALATE

    def test_default_escalate_has_reason(self):
        req = _req(category="unclassified", action_type=ActionType.OTHER, targets=[])
        verdict = _engine().evaluate(req)
        assert verdict.reason


# ---------------------------------------------------------------------------
# RuleEngine — priority: hard_rail always beats allow
# ---------------------------------------------------------------------------

class TestRulePriority:
    def test_hard_rail_beats_allow_for_secret_write_in_workspace(self):
        """
        A write that is inside the workspace AND touches secrets must still
        be denied by hr-006, not allowed by al-002.
        """
        req = _req(
            action_type=ActionType.WRITE,
            category="file_write",
            inside_workspace=True,
            touches_secrets=True,
        )
        verdict = _engine().evaluate(req)
        assert verdict.action == Action.DENY
        assert verdict.decided_by == DecidedBy.HARD_RAIL


# ---------------------------------------------------------------------------
# PolicyEngine — audit entry written before response
# ---------------------------------------------------------------------------

class TestPolicyEngineAudit:
    def test_allow_writes_audit_entry(self, tmp_path):
        audit = _audit(tmp_path)
        engine = PolicyEngine(_engine(), audit)
        req = _req(
            action_type=ActionType.READ,
            category="file_read",
            inside_workspace=True,
        )
        result = engine.decide(req)
        assert result["action"] == "allow"

        # Audit file must exist and contain exactly one entry
        result_v = audit.verify_chain()
        assert result_v.ok
        assert result_v.total_entries == 1

    def test_deny_writes_audit_entry(self, tmp_path):
        audit = _audit(tmp_path)
        engine = PolicyEngine(_engine(), audit)
        req = _req(
            action_type=ActionType.WRITE,
            category="supervisor_path_access",
        )
        result = engine.decide(req)
        assert result["action"] == "deny"

        result_v = audit.verify_chain()
        assert result_v.ok
        assert result_v.total_entries == 1

    def test_escalate_writes_audit_entry_and_resolves_to_escalate_denied(self, tmp_path):
        """Escalate stub → escalate_denied, but audit entry still written."""
        audit = _audit(tmp_path)
        engine = PolicyEngine(_engine(), audit)
        req = _req(
            tool_name="execute_command",
            action_type=ActionType.EXEC,
            category="shell_exec",
            inside_workspace=False,
        )
        result = engine.decide(req)
        assert result["action"] == "escalate_denied"

        result_v = audit.verify_chain()
        assert result_v.ok
        assert result_v.total_entries == 1

    def test_audit_write_failure_raises_policy_engine_error(self, tmp_path):
        """If the audit log write fails, PolicyEngine must raise PolicyEngineError."""
        failing_audit = MagicMock(spec=AuditLog)
        from supervisor.audit import AuditWriteError
        failing_audit.write_entry.side_effect = AuditWriteError("disk full")

        engine = PolicyEngine(_engine(), failing_audit)
        req = _req(action_type=ActionType.READ, category="file_read")

        with pytest.raises(PolicyEngineError, match="disk full"):
            engine.decide(req)

    def test_snapshot_contains_key_fields(self, tmp_path):
        """Audit snapshot must contain enough fields to reconstruct the decision."""
        import json
        audit = _audit(tmp_path)
        engine = PolicyEngine(_engine(), audit)
        req = _req(
            action_type=ActionType.READ,
            category="file_read",
            inside_workspace=True,
        )
        engine.decide(req)

        with (tmp_path / "audit_log.jsonl").open() as f:
            record = json.loads(f.readline())

        snap = record["snapshot"]
        assert snap["tool_name"] == "read_file"
        assert snap["action"] == "allow"
        assert snap["decided_by"] == "rule"
        assert snap["matched_rule_id"] == "al-001"
        assert snap["policy_version"] == "2026-11-04-v1"
        # prompt_text now included in snapshot (Christina's change 4)
        assert "prompt_text" in snap
        # for_review_only: False for rule-based allow (not a hard rail)
        assert snap["for_review_only"] is False

    def test_hard_rail_snapshot_for_review_only_true(self, tmp_path):
        """Hard-rail deny snapshots must have for_review_only=True."""
        import json
        audit = _audit(tmp_path)
        engine = PolicyEngine(_engine(), audit)
        req = _req(
            action_type=ActionType.WRITE,
            category="supervisor_path_access",
        )
        engine.decide(req)

        with (tmp_path / "audit_log.jsonl").open() as f:
            record = json.loads(f.readline())

        snap = record["snapshot"]
        assert snap["decided_by"] == "hard_rail"
        assert snap["for_review_only"] is True

    def test_escalation_writes_feedback_entry(self, tmp_path):
        """After escalation resolves, a second audit entry with record_type=human_feedback must be written."""
        import json
        from supervisor.escalation import EscalationManager
        import threading, time

        audit = _audit(tmp_path)
        escalation = EscalationManager(base_url="http://127.0.0.1:9001", timeout_seconds=5.0)
        engine = PolicyEngine(_engine(), audit, escalation)

        req = _req(
            tool_name="execute_command",
            action_type=ActionType.EXEC,
            category="shell_exec",
            inside_workspace=False,
        )

        decide_result = {}
        def _run():
            decide_result["result"] = engine.decide(req)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.15)

        # Human approves
        esc_id = next(iter(escalation._pending), None)
        assert esc_id is not None
        escalation.resolve(esc_id, approved=True)
        t.join(timeout=3.0)

        assert decide_result["result"]["action"] == "allow"

        # Two audit entries: decision + feedback
        with (tmp_path / "audit_log.jsonl").open() as f:
            entries = [json.loads(line) for line in f if line.strip()]

        assert len(entries) == 2
        feedback = entries[1]["snapshot"]
        assert feedback["record_type"] == "human_feedback"
        assert feedback["outcome"] == "human_allow"
        assert feedback["sent_to_pipeline"] is True
        assert feedback["for_review_only"] is False
