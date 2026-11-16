"""
schemas/verdict.py

The Policy Engine's decision on a single tool-call attempt.
Returned by supervisor/policy_engine.py to the Hook Client.

All fields are fully specified — no probe-log dependencies.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class Label(str, Enum):
    """
    Classifier output — describes the risk level of the tool call.

    SAFE:    Clearly benign. Auto-allow, no human prompt. Logged silently.
    NEUTRAL: Low-risk but not trivially safe. Auto-allow, passive log entry.
             Distinguished from Safe for audit granularity.
    WARNING: Elevated risk. Escalate to human OR deny, per policy rules.
    """
    SAFE    = "Safe"
    NEUTRAL = "Neutral"
    WARNING = "Warning"


class Action(str, Enum):
    """
    Policy Engine verdict — what actually happens to the tool call.

    ALLOW:    Tool runs immediately. No human prompt.
    ESCALATE: Human approval required before the tool runs.
              Hook Client waits; timeout → deny.
    DENY:     Tool is blocked. Hook Client exits 2 with reason on stderr.
    """
    ALLOW    = "allow"
    ESCALATE = "escalate"
    DENY     = "deny"


class DecidedBy(str, Enum):
    """
    Records which component produced the verdict.

    RULE:      Rule Engine matched a clear-cut allow rule in policy.yaml.
    HARD_RAIL: Rule Engine matched a hard-rail deny rule. Cannot be overridden
               by human feedback — HumanFeedback.for_review_only = True.
    MODEL:     Supervisor Model (student) classified the call on the ambiguous path.
               (Placeholder until Step 5 — ambiguous calls escalate via RULE path
               until the model is deployed.)
    """
    RULE      = "rule"
    HARD_RAIL = "hard_rail"
    MODEL     = "model"


class Verdict(BaseModel):
    """
    The complete decision record for a single tool-call attempt.

    Produced by supervisor/policy_engine.py.
    Written to the AuditLogEntry snapshot before any exit code is issued.
    Returned to the Hook Client, which exits 0 (allow) or 2 (deny/escalate-timeout).
    """

    # ------------------------------------------------------------------
    # Core decision
    # ------------------------------------------------------------------

    label: Label = Field(
        description="Risk classification of the tool call: Safe | Neutral | Warning."
    )
    action: Action = Field(
        description="What happens to the tool call: allow | escalate | deny."
    )
    decided_by: DecidedBy = Field(
        description="Which component produced this verdict: rule | hard_rail | model."
    )

    # ------------------------------------------------------------------
    # Traceability
    # ------------------------------------------------------------------

    reason: str = Field(
        description="Human-readable explanation of why this verdict was reached. "
                    "Sent to the model on stderr when action=deny, so it must be "
                    "clear and actionable. Also shown in the escalation prompt."
    )
    matched_rule_id: Optional[str] = Field(
        default=None,
        description="ID of the policy.yaml rule that produced this verdict. "
                    "None when decided_by=model (no single rule applies)."
    )
    policy_version: str = Field(
        description="Version tag of the policy.yaml that was active when this "
                    "verdict was produced (e.g. '2026-11-04-v1'). "
                    "Matches PolicyVersion.version_tag in the data model."
    )

    # ------------------------------------------------------------------
    # Model-path fields (populated only when decided_by=model)
    # ------------------------------------------------------------------

    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Classifier confidence score [0.0–1.0]. "
                    "None when decided_by=rule or hard_rail. "
                    "Used by the Policy Engine to decide escalate vs. deny on "
                    "the ambiguous path (low confidence → escalate; "
                    "very low confidence → deny)."
    )
    model_version_id: Optional[str] = Field(
        default=None,
        description="Version ID of the Supervisor Model that produced this verdict. "
                    "None when decided_by=rule or hard_rail. "
                    "Matches ModelVersion.version_tag in the data model."
    )
    index_version_id: Optional[str] = Field(
        default=None,
        description="Version ID of the Retriever index used alongside the model. "
                    "None when decided_by=rule or hard_rail. "
                    "Matches IndexVersion.version_tag in the data model."
    )

    model_config = ConfigDict(use_enum_values=True)
