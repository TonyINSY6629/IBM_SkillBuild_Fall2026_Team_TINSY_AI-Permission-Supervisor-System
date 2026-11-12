"""
supervisor/policy_engine.py — Verdict Applier + Audit Writer

Sits between the Rule Engine and the Hook Client response.
Receives a PermissionRequest, asks the Rule Engine for a Verdict,
writes an AuditLogEntry (synchronously, before returning anything),
then returns the final response dict for the /decide endpoint.

Fail-closed invariant:
  The audit log entry MUST be written and confirmed before any response
  is returned. If the write fails (AuditWriteError), the engine raises
  PolicyEngineError, which the /decide endpoint converts to a deny response.
  The Hook Client then exits 2.

Public API:
  PolicyEngine(rule_engine, audit_log, escalation_manager=None)
  PolicyEngine.decide(req) -> dict
      Returns: {"action": str, "reason": str, "policy_version": str}
      "action" is one of: "allow" | "deny" | "escalate_denied"
      (escalate is resolved here by the Escalation Manager — the Hook
       Client only ever sees allow or deny/escalate_denied)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from schemas.permission_request import PermissionRequest
from schemas.verdict import Action, Verdict
from supervisor.audit import AuditLog, AuditWriteError
from supervisor.rule_engine import RuleEngine

log = logging.getLogger("supervisor.policy_engine")


class PolicyEngineError(Exception):
    """Raised when the audit log write fails — caller must deny the tool call."""


class PolicyEngine:
    """
    Orchestrates: Rule Engine → Audit Log → Escalation (if needed) → response.
    """

    def __init__(
        self,
        rule_engine: RuleEngine,
        audit_log: AuditLog,
        escalation_manager: Optional[Any] = None,  # EscalationManager | None
    ) -> None:
        self._rule_engine = rule_engine
        self._audit_log = audit_log
        self._escalation = escalation_manager

    def decide(self, req: PermissionRequest) -> dict[str, Any]:
        """
        Evaluate a PermissionRequest and return the /decide response dict.

        Steps:
          1. Rule Engine → Verdict
          2. Write AuditLogEntry (synchronous, fsync'd) — raises PolicyEngineError
             if the write fails.
          3. If action == escalate: forward to EscalationManager (blocks until
             human responds or timeout fires → deny).
          4. Return {"action", "reason", "policy_version"}.

        The returned action is always "allow" or "deny"/"escalate_denied".
        The Hook Client never sees a raw "escalate".
        """
        # --- Step 1: get verdict from Rule Engine ---
        verdict: Verdict = self._rule_engine.evaluate(req)

        # --- Step 2: write audit log entry before any response ---
        snapshot = _build_snapshot(req, verdict)
        try:
            entry = self._audit_log.write_entry(
                decision_id=req.tool_use_id,
                snapshot=snapshot,
            )
            log.info(
                "audit: entry=%s tool=%s action=%s decided_by=%s rule=%s",
                entry.entry_id,
                req.tool_name,
                verdict.action,
                verdict.decided_by,
                verdict.matched_rule_id,
            )
        except AuditWriteError as exc:
            log.error("Audit log write failed — denying tool call: %s", exc)
            raise PolicyEngineError(
                f"Audit log write failed: {exc}"
            ) from exc

        # --- Step 3: resolve escalate ---
        action = verdict.action
        reason = verdict.reason

        if action == Action.ESCALATE:
            if self._escalation is None:
                # No escalation manager wired in — deny immediately (safe default).
                log.info(
                    "escalation: tool=%s no manager → deny", req.tool_name
                )
                return {
                    "action": "escalate_denied",
                    "reason": (
                        f"[Escalation required] {reason} "
                        f"(No Escalation Manager — denied by default.)"
                    ),
                    "policy_version": verdict.policy_version,
                }

            escalation_id = str(uuid.uuid4())
            result = self._escalation.request(
                escalation_id=escalation_id,
                tool_name=req.tool_name,
                category=req.category,
                reason=reason,
                details={
                    "tool_use_id": req.tool_use_id,
                    "session_id": req.session_id,
                    "action_type": getattr(req.action_type, "value", str(req.action_type)),
                    "targets": list(req.targets),
                    "inside_workspace": req.inside_workspace,
                    "touches_secrets": req.touches_secrets,
                    "prompt_text": req.prompt_text,
                    "tool_input": req.tool_input,
                    "label": getattr(verdict.label, "value", str(verdict.label)),
                    "policy_version": verdict.policy_version,
                    "matched_rule_id": getattr(verdict, "matched_rule_id", None),
                    "model_version_id": getattr(verdict, "model_version_id", None),
                    "confidence": getattr(verdict, "confidence", None),
                },
            )

            log.info(
                "escalation: id=%s source=%s action=%s tool=%s",
                escalation_id, result.source, result.action, req.tool_name,
            )

            # Record the human's answer as a feedback entry (Step 4 training data).
            # A timeout is not a human answer — it is not sent to the training pipeline.
            feedback_snapshot = {
                "record_type": "human_feedback",
                "tool_use_id": req.tool_use_id,
                "session_id": req.session_id,
                "prompt_id": req.prompt_id,
                "escalation_id": escalation_id,
                "outcome": result.source,           # human_allow | human_deny | timeout
                "final_action": result.action,      # allow | deny
                "note": result.note,                # NEW: Bug 3 - the human's note from the pop-up ("" if none)
                "label": verdict.label,
                "category": req.category,
                "policy_version": verdict.policy_version,
                "sent_to_pipeline": result.source != "timeout",
                "for_review_only": False,
            }
            try:
                self._audit_log.write_entry(
                    decision_id=req.tool_use_id,
                    snapshot=feedback_snapshot,
                )
            except AuditWriteError as exc:
                log.error("Feedback audit write failed — denying tool call: %s", exc)
                raise PolicyEngineError(
                    f"Audit log write failed: {exc}"
                ) from exc

            final_action = result.action          # "allow" or "deny"
            final_reason = (
                reason if final_action == "allow"
                else f"[Escalation {result.source}] {reason}"
            )
            return {
                "action": final_action if final_action == "allow" else "escalate_denied",
                "reason": final_reason,
                "policy_version": verdict.policy_version,
            }

        # --- Step 4: allow or deny ---
        return {
            "action": action,      # "allow" or "deny"
            "reason": reason,
            "policy_version": verdict.policy_version,
        }


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def _build_snapshot(req: PermissionRequest, verdict: Verdict) -> dict[str, Any]:
    """
    Build the audit log snapshot dict. Contains everything needed to
    reconstruct the decision independently of any running service.
    Stored verbatim in AuditLogEntry.snapshot.
    """
    return {
        # ToolCallRequest fields
        "tool_use_id": req.tool_use_id,
        "session_id": req.session_id,
        "tool_name": req.tool_name,
        "tool_input": req.tool_input,
        "cwd": req.cwd,
        "action_type": req.action_type,
        "category": req.category,
        "targets": req.targets,
        "inside_workspace": req.inside_workspace,
        "touches_secrets": req.touches_secrets,
        "prompt_id": req.prompt_id,
        "prompt_text": req.prompt_text,
        # Decision fields
        "label": verdict.label,
        "action": verdict.action,
        "decided_by": verdict.decided_by,
        "reason": verdict.reason,
        "matched_rule_id": verdict.matched_rule_id,
        "policy_version": verdict.policy_version,
        "confidence": verdict.confidence,
        "model_version_id": verdict.model_version_id,
        "index_version_id": verdict.index_version_id,
        # Hard-rail denies are review-only — must never train the model.
        "for_review_only": verdict.decided_by == "hard_rail",
    }
