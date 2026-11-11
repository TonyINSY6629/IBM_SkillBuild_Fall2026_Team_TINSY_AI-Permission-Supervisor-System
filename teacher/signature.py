"""
teacher/signature.py — DSPy Signature for the AI Permission Supervisor teacher task.

The teacher takes a NormalizedRequest (tool call context + policy excerpt) and
produces the four fields that become a training example for the student model:

  label      — Safe | Neutral | Warning
  action     — allow | deny
  reason     — one sentence explaining the decision (grounded in policy)
  confidence — 0.0–1.0 (how certain the teacher is)

Fix 3 (Christina, 2026-11-10): Instructions changed from "do not allow anything
the policy does not permit / if in doubt, escalate" to "predict what the human
would decide". Every example in the training set reached a human because the
policy did NOT decide it, and 111 of 158 were Allow — so the old instructions
pushed the teacher to disagree with the human majority.

Fix 4 (Christina, 2026-11-10): Labels are Safe / Neutral / Warning (not Info /
Warning / Danger / Critical). Action is allow / deny only — "escalate" is never
a human answer.

Usage:
  import dspy
  from teacher.dspy_config import configure_dspy
  from teacher.signature import PermissionDecision, PermissionDecisionCoT

  configure_dspy()
  predictor = dspy.Predict(PermissionDecision)
  result = predictor(
      tool_name="execute_command",
      tool_input='{"command": "curl https://example.com"}',
      category="network_request",
      action_type="EXEC",
      inside_workspace="false",
      touches_secrets="false",
      prompt_text="Download the latest prices CSV",
      policy_excerpt=<relevant policy rules>,
  )
  print(result.label, result.action, result.reason, result.confidence)
"""

from __future__ import annotations

import dspy


# ---------------------------------------------------------------------------
# Input/Output field descriptions
# These are shown verbatim to the model as part of the prompt template.
# Keep them precise — they anchor the model's reasoning.
# ---------------------------------------------------------------------------

class PermissionDecision(dspy.Signature):
    """
    You are an AI permission supervisor. Given a tool call that an AI coding
    assistant is about to execute, predict whether the human reviewer would
    allow or deny it. These calls were NOT decided by the policy rules, so a
    human was asked. Use the tool call, the user's task, and the policy excerpt
    as context.
    """

    # --- Inputs ---
    tool_name: str = dspy.InputField(
        desc="Name of the Bob tool being called (e.g. execute_command, write_file)."
    )
    tool_input: str = dspy.InputField(
        desc="JSON-serialised tool_input dict (the arguments to the tool call)."
    )
    category: str = dspy.InputField(
        desc=(
            "Taxonomy category assigned by the normalizer: one of "
            "file_read | file_write | shell_exec | network_request | "
            "secret_access | workspace_outside_write | supervisor_path_access | unclassified."
        )
    )
    action_type: str = dspy.InputField(
        desc="High-level action type: READ | WRITE | EXEC | OTHER."
    )
    inside_workspace: str = dspy.InputField(
        desc=(
            "'true' if all file targets are inside the open workspace folder, "
            "'false' otherwise."
        )
    )
    touches_secrets: str = dspy.InputField(
        desc=(
            "'true' if any target matches a known secret pattern "
            "(.env, .pem, id_rsa, API key env-var, etc.), 'false' otherwise."
        )
    )
    prompt_text: str = dspy.InputField(
        desc=(
            "The user's current task prompt — what the AI was asked to do. "
            "Empty string if unknown."
        )
    )
    policy_excerpt: str = dspy.InputField(
        desc=(
            "The most relevant policy rules for this category, extracted from "
            "policy.yaml. Format: 'Rule <id>: <condition> → <verdict>\\n...'"
        )
    )

    # --- Outputs ---
    label: str = dspy.OutputField(
        desc=(
            "Severity label: Safe (no risk), Neutral (low risk), "
            "Warning (risky, a human should look)."
        )
    )
    action: str = dspy.OutputField(
        desc="Decision: allow | deny."
    )
    reason: str = dspy.OutputField(
        desc=(
            "One sentence explaining the decision, grounded in the policy excerpt. "
            "Do not mention fields not present in the input."
        )
    )
    confidence: str = dspy.OutputField(
        desc=(
            "Confidence score from 0.0 (uncertain) to 1.0 (certain). "
            "Use 0.5 when the policy is ambiguous or the case is borderline."
        )
    )


# Chain-of-Thought variant — used during optimisation for better few-shot quality.
class PermissionDecisionCoT(dspy.Signature):
    """
    You are an AI permission supervisor. Given a tool call that an AI coding
    assistant is about to execute, predict whether the human reviewer would
    allow or deny it. These calls were NOT decided by the policy rules, so a
    human was asked. Use the tool call, the user's task, and the policy excerpt
    as context. Think step by step before giving your final answer.
    """

    tool_name: str = dspy.InputField(
        desc="Name of the Bob tool being called (e.g. execute_command, write_file)."
    )
    tool_input: str = dspy.InputField(
        desc="JSON-serialised tool_input dict (the arguments to the tool call)."
    )
    category: str = dspy.InputField(
        desc=(
            "Taxonomy category: file_read | file_write | shell_exec | "
            "network_request | secret_access | workspace_outside_write | "
            "supervisor_path_access | unclassified."
        )
    )
    action_type: str = dspy.InputField(desc="READ | WRITE | EXEC | OTHER.")
    inside_workspace: str = dspy.InputField(desc="'true' or 'false'.")
    touches_secrets: str = dspy.InputField(desc="'true' or 'false'.")
    prompt_text: str = dspy.InputField(desc="The user's current task prompt.")
    policy_excerpt: str = dspy.InputField(desc="Relevant policy rules for this category.")

    reasoning: str = dspy.OutputField(
        desc=(
            "Step-by-step reasoning: (1) What does the tool do? "
            "(2) Which policy rule applies? (3) What risk does this pose? "
            "(4) What is the correct decision and why?"
        )
    )
    label: str = dspy.OutputField(desc="Safe | Neutral | Warning.")
    action: str = dspy.OutputField(desc="allow | deny.")
    reason: str = dspy.OutputField(
        desc="One sentence summary of the decision, grounded in policy."
    )
    confidence: str = dspy.OutputField(desc="0.0–1.0.")
