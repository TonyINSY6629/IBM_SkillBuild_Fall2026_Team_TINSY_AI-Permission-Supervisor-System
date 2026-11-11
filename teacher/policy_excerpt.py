"""
teacher/policy_excerpt.py — Extract relevant policy rules for a given category.

Loads policy.yaml and returns a compact text excerpt of the rules most
relevant to a given category. This is injected as the policy_excerpt input
field in every teacher prediction call.

Usage:
  from teacher.policy_excerpt import build_excerpt
  excerpt = build_excerpt(category="shell_exec", policy_path="policy/policy.yaml")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT_POLICY = Path(__file__).parent.parent / "policy" / "policy.yaml"


def load_policy(path: Path = _DEFAULT_POLICY) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_excerpt(
    category: str,
    policy: dict[str, Any] | None = None,
    policy_path: Path = _DEFAULT_POLICY,
    max_rules: int = 6,
) -> str:
    """
    Return a compact text representation of the policy rules most relevant
    to the given category.

    Includes:
      - All hard_rail rules whose condition matches category (always included first)
      - Allow rules matching category (up to max_rules total)
      - The default verdict

    Format per rule:
      Rule <id> [hard_rail|allow]: <condition summary> → <action> (<label>) — <reason>
    """
    if policy is None:
        policy = load_policy(policy_path)

    lines: list[str] = []
    version = policy.get("meta", {}).get("version_tag", "unknown")
    lines.append(f"Policy version: {version}")
    lines.append("")

    def _condition_summary(cond: dict) -> str:
        parts = []
        for k, v in cond.items():
            parts.append(f"{k}={v!r}")
        return ", ".join(parts) if parts else "(any)"

    def _add_rule(rule: dict, rule_type: str) -> None:
        rid = rule.get("id", "?")
        cond = rule.get("condition", {})
        verdict = rule.get("verdict", {})
        action = verdict.get("action", "?")
        label = verdict.get("label", "?")
        reason = verdict.get("reason", "").strip().replace("\n", " ")
        cond_str = _condition_summary(cond)
        lines.append(f"Rule {rid} [{rule_type}]: {cond_str} → {action} ({label}) — {reason}")

    count = 0
    for rule in policy.get("hard_rails", []):
        cond = rule.get("condition", {})
        if not cond or cond.get("category") == category or "category" not in cond:
            _add_rule(rule, "hard_rail")
            count += 1

    for rule in policy.get("allow_rules", []):
        if count >= max_rules:
            break
        cond = rule.get("condition", {})
        if not cond or cond.get("category") == category or "category" not in cond:
            _add_rule(rule, "allow")
            count += 1

    # Always include the default.
    default = policy.get("default", {})
    if default:
        action = default.get("action", "escalate")
        label = default.get("label", "Warning")
        reason = default.get("reason", "").strip().replace("\n", " ")
        lines.append(f"Default (no match): → {action} ({label}) — {reason}")

    return "\n".join(lines)
