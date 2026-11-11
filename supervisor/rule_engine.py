"""
supervisor/rule_engine.py — Policy Rule Matcher

Loads policy.yaml + taxonomy.yaml and evaluates a PermissionRequest
against the rule set, returning a Verdict.

Evaluation order (mirrors policy.yaml comments):
  1. hard_rails  — first match → Verdict(deny, hard_rail)
  2. allow_rules — first match → Verdict(allow, rule)
  3. default     → Verdict(escalate, rule)

Condition fields (all optional; omitted = match anything):
  category         — str, must equal PermissionRequest.category
  action_type      — str, must equal PermissionRequest.action_type value
  inside_workspace — bool
  touches_secrets  — bool
  target_pattern   — glob pattern matched against any element of targets[]

Public API:
  RuleEngine(policy_dict)          — construct from already-loaded policy dict
  RuleEngine.evaluate(req)         -> Verdict
  load_policy(path)                -> dict   (convenience loader)
"""

from __future__ import annotations

import fnmatch
from typing import Any

from schemas.permission_request import PermissionRequest
from schemas.verdict import Action, DecidedBy, Label, Verdict

# ---------------------------------------------------------------------------
# Policy loader (used by app.py's _PolicyStore — kept thin here)
# ---------------------------------------------------------------------------

import yaml
from pathlib import Path


def load_policy(path: str | Path) -> dict[str, Any]:
    """Parse a policy.yaml file and return the raw dict."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Condition matching
# ---------------------------------------------------------------------------

def _match_condition(condition: dict[str, Any], req: PermissionRequest) -> bool:
    """
    Return True if the PermissionRequest satisfies all fields in condition.
    An omitted field matches anything.
    """
    # category
    if "category" in condition:
        if req.category != condition["category"]:
            return False

    # action_type — policy stores as e.g. "WRITE"; req stores enum value string
    if "action_type" in condition:
        if str(req.action_type).upper() != condition["action_type"].upper():
            return False

    # inside_workspace
    if "inside_workspace" in condition:
        if req.inside_workspace != bool(condition["inside_workspace"]):
            return False

    # touches_secrets
    if "touches_secrets" in condition:
        if req.touches_secrets != bool(condition["touches_secrets"]):
            return False

    # target_pattern — must match at least one element of req.targets
    if "target_pattern" in condition:
        pattern = condition["target_pattern"]
        # Normalise separators for cross-platform glob matching
        targets_norm = [t.replace("\\", "/") for t in req.targets]
        if not any(fnmatch.fnmatch(t, pattern) for t in targets_norm):
            return False

    return True


# ---------------------------------------------------------------------------
# Verdict builder from a rule's verdict dict
# ---------------------------------------------------------------------------

def _build_verdict(
    rule_id: str,
    verdict_dict: dict[str, Any],
    policy_version: str,
) -> Verdict:
    raw_reason: str = verdict_dict.get("reason", "")
    # YAML block scalars add a trailing newline — strip it.
    reason = raw_reason.strip()

    return Verdict(
        label=Label(verdict_dict["label"]),
        action=Action(verdict_dict["action"]),
        decided_by=DecidedBy(verdict_dict["decided_by"]),
        reason=reason,
        matched_rule_id=rule_id,
        policy_version=policy_version,
    )


# ---------------------------------------------------------------------------
# RuleEngine
# ---------------------------------------------------------------------------

class RuleEngine:
    """
    Stateless rule evaluator constructed from a loaded policy dict.

    app.py's _PolicyStore creates a new RuleEngine each time it reloads
    policy.yaml, so the engine always reflects the current policy without
    requiring a restart.
    """

    def __init__(self, policy: dict[str, Any]) -> None:
        self._policy = policy
        self._version: str = policy.get("meta", {}).get("version_tag", "unknown")
        self._hard_rails: list[dict] = policy.get("hard_rails", [])
        self._allow_rules: list[dict] = policy.get("allow_rules", [])
        self._default: dict = policy.get("default", {
            "action": "escalate",
            "label": "Warning",
            "reason": "No policy rule matched. Escalating to human.",
        })

    @property
    def policy_version(self) -> str:
        return self._version

    def evaluate(self, req: PermissionRequest) -> Verdict:
        """
        Match req against the rule set and return the applicable Verdict.

        Steps:
          1. Evaluate hard_rails in order — first match → deny (hard_rail).
          2. Evaluate allow_rules in order — first match → allow (rule).
          3. No match → default escalate (rule).
        """
        # 1. Hard rails
        for rule in self._hard_rails:
            condition = rule.get("condition", {})
            if _match_condition(condition, req):
                return _build_verdict(rule["id"], rule["verdict"], self._version)

        # 2. Allow rules
        for rule in self._allow_rules:
            condition = rule.get("condition", {})
            if _match_condition(condition, req):
                return _build_verdict(rule["id"], rule["verdict"], self._version)

        # 3. Default — escalate
        default = self._default
        reason = default.get("reason", "No policy rule matched.").strip()
        return Verdict(
            label=Label(default.get("label", "Warning")),
            action=Action(default.get("action", "escalate")),
            decided_by=DecidedBy.RULE,
            reason=reason,
            matched_rule_id=None,
            policy_version=self._version,
        )
