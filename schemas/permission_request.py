"""
schemas/permission_request.py

Normalized representation of a Bob PreToolUse hook payload.
This is the input to the Rule Engine and Policy Engine.

DRAFT STATUS (Step 1 probe still running):
  - Read-path fields (tool_name, tool_input, cwd, session_id, tool_use_id,
    action_type, targets) are grounded in probe_log.jsonl observations.
  - Write-path tools (write_file, apply_diff, search_and_replace, insert_content)
    have not yet appeared in the log. Field shapes for those tools are inferred
    from Bob's documented tool signatures and will be confirmed once the probe
    log accumulates write-path entries.
  - Do a final field review after the first write-path entries appear in the log
    before freezing this schema for Step 3 (Normalizer implementation).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class ActionType(str, Enum):
    """
    Coarse action classification derived from tool_name + tool_input.
    Used by the Rule Engine to route to the correct category matcher.
    """
    READ   = "read"    # read_file, list_files, glob, grep, GetSymbolsOverview, ...
    WRITE  = "write"   # write_file, apply_diff, search_and_replace, insert_content, ...
    EXEC   = "exec"    # execute_command
    OTHER  = "other"   # office_read, office_edit, ask_followup_question, etc.


class PermissionRequest(BaseModel):
    """
    Normalized, tool-agnostic description of a single tool-call attempt.

    Produced by supervisor/normalizer.py from the raw PreToolUse JSON payload.
    Consumed by supervisor/rule_engine.py.
    """

    # ------------------------------------------------------------------
    # Identity — carried verbatim from the Bob hook payload
    # ------------------------------------------------------------------

    tool_use_id: str = Field(
        description="Bob's unique identifier for this tool-call attempt. "
                    "Primary key linking ToolCallRequest → Decision → AuditLogEntry."
    )
    session_id: str = Field(
        description="Bob session identifier. Used to look up the current prompt "
                    "from the Context Store."
    )
    tool_name: str = Field(
        description="Exact tool name as Bob reports it (e.g. 'write_file', "
                    "'execute_command'). Stored verbatim; not used for routing — "
                    "action_type and category handle routing."
    )
    tool_input: dict[str, Any] = Field(
        description="Raw tool_input dict from Bob's hook payload. Stored for "
                    "audit and for the Normalizer to extract targets."
    )

    # ------------------------------------------------------------------
    # Workspace context — from the hook payload
    # ------------------------------------------------------------------

    cwd: str = Field(
        description="Working directory at the time of the tool call, as reported "
                    "by Bob's hook payload."
    )

    # ------------------------------------------------------------------
    # Normalized action classification
    # ------------------------------------------------------------------

    action_type: ActionType = Field(
        description="Coarse action type derived from tool_name. "
                    "READ | WRITE | EXEC | OTHER."
    )
    category: str = Field(
        description="Taxonomy category id from taxonomy.yaml "
                    "(e.g. 'file_read', 'file_write', 'shell_exec', "
                    "'network_request', 'secret_access', "
                    "'supervisor_path_access', 'workspace_outside_write', "
                    "'unclassified')."
    )

    # ------------------------------------------------------------------
    # Target paths / resources
    # ------------------------------------------------------------------

    targets: list[str] = Field(
        default_factory=list,
        description="Resolved target paths or resource identifiers extracted "
                    "from tool_input. For file tools: the 'path' field. "
                    "For execute_command: the raw command string (single element). "
                    "May be empty for tools with no meaningful target "
                    "(e.g. ask_followup_question)."
    )
    inside_workspace: bool = Field(
        description="True if ALL targets resolve to paths within a known workspace "
                    "root. False if any target is outside, or if the target cannot "
                    "be resolved (treat unknown as outside — fail-closed)."
    )

    # ------------------------------------------------------------------
    # Risk flags — derived by Normalizer heuristics
    # ------------------------------------------------------------------

    touches_secrets: bool = Field(
        default=False,
        description="True if any target path or command substring matches a "
                    "known secret pattern from taxonomy.yaml "
                    "(e.g. .env, *.pem, *_TOKEN env vars)."
    )

    # ------------------------------------------------------------------
    # Context — attached by Context Store lookup
    # ------------------------------------------------------------------

    prompt_id: Optional[str] = Field(
        default=None,
        description="ID of the UserPromptSubmit event that initiated the current "
                    "task. Populated by supervisor/context_store.py lookup keyed "
                    "on session_id. None if no prompt has been recorded yet for "
                    "this session (e.g. tool called before first UserPromptSubmit)."
    )
    prompt_text: Optional[str] = Field(
        default=None,
        description="Text of the user prompt that initiated the current task. "
                    "Used by the Supervisor Model for context-aware classification. "
                    "None if prompt_id is None."
    )

    model_config = ConfigDict(use_enum_values=True)
