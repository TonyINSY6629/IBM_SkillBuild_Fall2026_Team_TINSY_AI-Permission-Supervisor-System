"""Shared prompt format for the student model.

The same functions are used to build the training file, to test the
student, and (later) when the supervisor asks the student for a verdict,
so the model always sees requests in exactly the same layout.
"""
import json

SYSTEM_PROMPT = (
    "You are a permission supervisor for an AI coding assistant. "
    "You are shown one tool call that no policy rule matched. "
    "Decide whether it should be allowed or denied. "
    'Reply with only a JSON object: {"action": "allow" or "deny", "reason": "<one sentence>"}'
)

# Only fields that describe the tool call itself. Human answers, ids and
# timestamps are left out on purpose, so the student cannot "peek".
INPUT_FIELDS = [
    "prompt_text",
    "tool_name",
    "tool_input",
    "cwd",
    "action_type",
    "category",
    "inside_workspace",
    "touches_secrets",
]


def build_user_message(example: dict) -> str:
    """Turn one tool call into the text the student reads."""
    shown = {field: example.get(field) for field in INPUT_FIELDS}
    return json.dumps(shown, ensure_ascii=False, indent=1)


def build_answer(action: str, reason: str) -> str:
    """The exact reply the student should learn to give."""
    return json.dumps({"action": action, "reason": reason}, ensure_ascii=False)


def parse_answer(text: str) -> dict | None:
    """Read the student's reply. Returns None if it is not a valid answer,
    so the caller can fail closed (escalate or deny)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("action") not in ("allow", "deny"):
        return None
    return {"action": data["action"], "reason": str(data.get("reason", ""))[:500]}