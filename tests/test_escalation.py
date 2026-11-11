"""
tests/test_escalation.py

Tests for supervisor/escalation.py.

Done-condition (from plan Step 3.6):
  Send escalation, wait for timeout without responding → deny, every time.

Also tests:
  - Human approve → allow
  - Human deny → deny
  - resolve() returns False for unknown/already-timed-out escalation_id
  - Multiple concurrent escalations resolve independently
  - PolicyEngine with EscalationManager: timeout → escalate_denied
  - PolicyEngine with EscalationManager: human allow → allow

Run with:
    python -m pytest tests/test_escalation.py -v
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from supervisor.escalation import EscalationManager, EscalationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manager(timeout: float = 2.0) -> EscalationManager:
    """Short timeout so tests don't hang."""
    return EscalationManager(base_url="http://127.0.0.1:9001", timeout_seconds=timeout)


def _request_in_thread(manager, escalation_id, results: dict):
    """Run manager.request() in a background thread; store result in dict."""
    result = manager.request(
        escalation_id=escalation_id,
        tool_name="execute_command",
        category="shell_exec",
        reason="Test escalation",
    )
    results[escalation_id] = result


# ---------------------------------------------------------------------------
# Done-condition: timeout → deny, every time
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_timeout_returns_deny(self):
        """Done-condition: no response within timeout → deny."""
        manager = _manager(timeout=1.0)
        result = manager.request(
            escalation_id="esc-timeout-001",
            tool_name="execute_command",
            category="shell_exec",
            reason="Test timeout",
        )
        assert result.action == "deny"
        assert result.source == "timeout"

    def test_timeout_returns_deny_repeatedly(self):
        """Timeout must deny every time — not just once."""
        manager = _manager(timeout=0.5)
        for i in range(3):
            result = manager.request(
                escalation_id=f"esc-timeout-{i:03d}",
                tool_name="read_file",
                category="file_read",
                reason=f"Repeat {i}",
            )
            assert result.action == "deny", f"Iteration {i} did not deny"
            assert result.source == "timeout"

    def test_timeout_fires_before_hook_client_deadline(self):
        """
        The escalation timeout (1s in this test) must fire and return
        before the Hook Client deadline would fire (represented here by
        waiting 1.5s total). Validates the timeout ordering invariant.
        """
        manager = _manager(timeout=1.0)
        start = time.monotonic()
        result = manager.request(
            escalation_id="esc-timing-001",
            tool_name="execute_command",
            category="shell_exec",
            reason="Timing test",
        )
        elapsed = time.monotonic() - start

        assert result.action == "deny"
        assert result.source == "timeout"
        # Must have returned within 1.5s (timeout=1.0s + small buffer)
        assert elapsed < 1.5, f"Timeout took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Human approve → allow
# ---------------------------------------------------------------------------

class TestHumanApprove:
    def test_human_approve_returns_allow(self):
        manager = _manager(timeout=5.0)
        results = {}
        esc_id = "esc-approve-001"

        # Start escalation in background thread
        t = threading.Thread(
            target=_request_in_thread,
            args=(manager, esc_id, results),
            daemon=True,
        )
        t.start()

        # Give the request a moment to register
        time.sleep(0.1)

        # Human clicks Allow
        found = manager.resolve(esc_id, approved=True)
        assert found, "resolve() should find the pending escalation"

        t.join(timeout=2.0)
        assert esc_id in results
        assert results[esc_id].action == "allow"
        assert results[esc_id].source == "human_allow"


# ---------------------------------------------------------------------------
# Human deny → deny
# ---------------------------------------------------------------------------

class TestHumanDeny:
    def test_human_deny_returns_deny(self):
        manager = _manager(timeout=5.0)
        results = {}
        esc_id = "esc-deny-001"

        t = threading.Thread(
            target=_request_in_thread,
            args=(manager, esc_id, results),
            daemon=True,
        )
        t.start()
        time.sleep(0.1)

        found = manager.resolve(esc_id, approved=False)
        assert found

        t.join(timeout=2.0)
        assert results[esc_id].action == "deny"
        assert results[esc_id].source == "human_deny"


# ---------------------------------------------------------------------------
# resolve() on unknown / already-timed-out escalation
# ---------------------------------------------------------------------------

class TestResolveUnknown:
    def test_resolve_unknown_id_returns_false(self):
        manager = _manager(timeout=1.0)
        found = manager.resolve("non-existent-id", approved=True)
        assert not found

    def test_resolve_after_timeout_returns_false(self):
        """After the escalation times out, resolve() should return False."""
        manager = _manager(timeout=0.3)
        esc_id = "esc-post-timeout-001"

        # Run escalation and let it time out
        result = manager.request(
            escalation_id=esc_id,
            tool_name="read_file",
            category="file_read",
            reason="Will time out",
        )
        assert result.source == "timeout"

        # Now try to resolve — escalation is no longer pending
        found = manager.resolve(esc_id, approved=True)
        assert not found


# ---------------------------------------------------------------------------
# Multiple concurrent escalations
# ---------------------------------------------------------------------------

class TestConcurrent:
    def test_two_concurrent_escalations_resolve_independently(self):
        manager = _manager(timeout=5.0)
        results = {}

        esc_a = "esc-concurrent-A"
        esc_b = "esc-concurrent-B"

        t_a = threading.Thread(
            target=_request_in_thread, args=(manager, esc_a, results), daemon=True
        )
        t_b = threading.Thread(
            target=_request_in_thread, args=(manager, esc_b, results), daemon=True
        )
        t_a.start()
        t_b.start()
        time.sleep(0.1)

        manager.resolve(esc_a, approved=True)
        manager.resolve(esc_b, approved=False)

        t_a.join(timeout=2.0)
        t_b.join(timeout=2.0)

        assert results[esc_a].action == "allow"
        assert results[esc_b].action == "deny"


# ---------------------------------------------------------------------------
# PolicyEngine integration: timeout → escalate_denied
# ---------------------------------------------------------------------------

class TestPolicyEngineEscalation:
    def test_escalation_timeout_returns_escalate_denied(self, tmp_path):
        """
        End-to-end: PolicyEngine with a short-timeout EscalationManager,
        shell_exec request → escalate → timeout → escalate_denied.
        """
        from schemas.permission_request import ActionType, PermissionRequest
        from supervisor.audit import AuditLog
        from supervisor.policy_engine import PolicyEngine
        from supervisor.rule_engine import RuleEngine, load_policy

        policy_path = Path(__file__).parent.parent / "policy" / "policy.yaml"
        audit = AuditLog(log_path=tmp_path / "audit.jsonl")
        escalation = _manager(timeout=0.5)
        engine = PolicyEngine(load_policy.__wrapped__(policy_path) if hasattr(load_policy, '__wrapped__') else
                              RuleEngine(load_policy(policy_path)), audit, escalation)

        req = PermissionRequest(
            tool_use_id="u-esc-001",
            session_id="s-001",
            tool_name="execute_command",
            tool_input={"command": "python --version"},
            cwd=str(tmp_path),
            action_type=ActionType.EXEC,
            category="shell_exec",
            targets=["python --version"],
            inside_workspace=False,
            touches_secrets=False,
        )
        result = engine.decide(req)
        assert result["action"] == "escalate_denied"

    def test_escalation_human_allow_returns_allow(self, tmp_path):
        """
        End-to-end: human approves within timeout → allow.
        """
        from schemas.permission_request import ActionType, PermissionRequest
        from supervisor.audit import AuditLog
        from supervisor.policy_engine import PolicyEngine
        from supervisor.rule_engine import RuleEngine, load_policy

        policy_path = Path(__file__).parent.parent / "policy" / "policy.yaml"
        audit = AuditLog(log_path=tmp_path / "audit.jsonl")
        escalation = EscalationManager(
            base_url="http://127.0.0.1:9001", timeout_seconds=5.0
        )

        engine = PolicyEngine(
            RuleEngine(load_policy(policy_path)), audit, escalation
        )

        req = PermissionRequest(
            tool_use_id="u-esc-002",
            session_id="s-001",
            tool_name="execute_command",
            tool_input={"command": "python --version"},
            cwd=str(tmp_path),
            action_type=ActionType.EXEC,
            category="shell_exec",
            targets=["python --version"],
            inside_workspace=False,
            touches_secrets=False,
        )

        decide_result = {}

        def _run():
            decide_result["result"] = engine.decide(req)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.2)  # Let request register

        # Find and approve the pending escalation
        esc_id = next(iter(escalation._pending), None)
        assert esc_id is not None, "No pending escalation found"
        escalation.resolve(esc_id, approved=True)

        t.join(timeout=3.0)
        assert decide_result["result"]["action"] == "allow"
