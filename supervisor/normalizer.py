"""
supervisor/normalizer.py — Bob Payload → PermissionRequest

Converts a raw Bob PreToolUse hook payload into a normalized PermissionRequest.

Grounded in probe_log.jsonl observations (Step 1):
  read_file:        {path, context_focus_query}  or  {path, context_focus_query, range}
  list_files:       {path}  or  {path, recursive}
  write_file:       {content, line_count, path}
  apply_diff:       {diff, path}
  execute_command:  {command}  or  {command, timeout_seconds}  or  {command, background}
  update_todo_list: {todos}

Tool names not yet seen in the probe log but known from Bob's tool schema:
  grep, glob, GetSymbolsOverview, FindSymbol, FindReferencingSymbols,
  search_and_replace, insert_content, office_read, office_edit,
  ask_followup_question, create_html_artifact, create_chart,
  switch_mode, start_subtask, start_workflow, spawn_subagent,
  use_skill, list_ibm_doc_libraries, search_ibm_docs

DRAFT NOTE: The field list in PermissionRequest is grounded in the log
entries seen so far. Run the done-condition test (replay all log entries)
after each new batch of probe log entries to confirm no regressions.

Public API:
  normalize(payload, workspace_roots, prompt_id, prompt_text) -> PermissionRequest
  NormalizerError — raised for unrecoverable payload problems
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional

from schemas.permission_request import ActionType, PermissionRequest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tools that perform read-only access and produce no side-effects.
_READ_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_files",
    "glob",
    "grep",
    "GetSymbolsOverview",
    "FindSymbol",
    "FindReferencingSymbols",
    "office_read",
    "list_ibm_doc_libraries",
    "search_ibm_docs",
})

# Tools that write or modify files.
_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "apply_diff",
    "search_and_replace",
    "insert_content",
    "office_edit",
})

# Tools that execute shell commands.
_EXEC_TOOLS: frozenset[str] = frozenset({
    "execute_command",
})

# Secret file/path patterns (matched case-insensitively as substrings).
_SECRET_PATH_PATTERNS: tuple[str, ...] = (
    ".env",
    ".pem", ".key", ".pfx", ".p12",
    "id_rsa", "id_ed25519", ".ppk",
    ".aws/credentials",
    ".netrc",
    ".git-credentials",
)

# Secret env-var / command substrings (matched case-insensitively).
_SECRET_CMD_PATTERNS: tuple[str, ...] = (
    "_KEY", "_TOKEN", "_SECRET", "_PASSWORD",
    "HUGGING_FACE_HUB_TOKEN", "OPENAI_API_KEY",
)

# Network command substrings (matched case-insensitively).
_NETWORK_CMD_PATTERNS: tuple[str, ...] = (
    "curl ", "wget ", "invoke-webrequest", "invoke-restmethod",
    "pip install", "pip3 install", "npm install", "yarn add",
    "git clone", "git fetch", "git pull", "git push",
    "ssh ", "scp ", "sftp ",
    "nslookup ", "ping ", "tracert ", "traceroute ",
)

# Supervisor / hook-client paths (matched as substrings in the resolved path).
_SUPERVISOR_PATH_PATTERNS: tuple[str, ...] = (
    "supervisor_probe",
    "supervisor_audit",
    "hook_client/main.py",
    "hook_client\\main.py",
    "hook_client/probe.py",
    "hook_client\\probe.py",
    ".bob/settings",
    ".bob\\settings",
)

# Path-traversal patterns.
_TRAVERSAL_PATTERNS: tuple[str, ...] = ("../", "..\\", "%2e%2e")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NormalizerError(Exception):
    """Raised when the payload is missing required fields and cannot be normalized."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _action_type(tool_name: str) -> ActionType:
    if tool_name in _READ_TOOLS:
        return ActionType.READ
    if tool_name in _WRITE_TOOLS:
        return ActionType.WRITE
    if tool_name in _EXEC_TOOLS:
        return ActionType.EXEC
    return ActionType.OTHER


def _extract_targets(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """
    Extract meaningful target identifiers from tool_input.

    For file-access tools: the 'path' field (resolved if possible).
    For execute_command:   the 'command' string (single element list).
    For others:            empty list.
    """
    if tool_name in _READ_TOOLS | _WRITE_TOOLS:
        path = tool_input.get("path", "")
        return [str(path)] if path else []
    if tool_name in _EXEC_TOOLS:
        cmd = tool_input.get("command", "")
        return [str(cmd)] if cmd else []
    return []


def _resolve_path(raw: str, cwd: str) -> Path:
    """
    Resolve a raw path string to an absolute Path.
    Handles ~ expansion, relative paths (resolved against cwd),
    and already-absolute paths.
    """
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve()


def _inside_workspace(targets: list[str], cwd: str,
                      workspace_roots: list[str]) -> bool:
    """
    Return True if ALL targets resolve to paths inside at least one
    known workspace root.  Unknown = outside (fail-closed).
    """
    if not targets:
        return True  # No targets → no write risk

    roots = [Path(r).resolve() for r in workspace_roots]
    if not roots:
        return False  # No known roots → treat as outside

    for raw in targets:
        # For execute_command targets (commands), there's no meaningful
        # path to check — treat as outside workspace.
        if raw.startswith(("cat ", "ls ", "dir ", "type ", "echo ", "python",
                            "npm ", "pip ", "git ", "curl ", "wget ",
                            "ssh ", "scp ", "Get-", "Invoke-", "Start-")):
            return False
        try:
            resolved = _resolve_path(raw, cwd)
        except (OSError, ValueError):
            return False  # Unresolvable path → treat as outside
        if not any(
            _is_relative_to(resolved, root) for root in roots
        ):
            return False
    return True


def _is_relative_to(child: Path, parent: Path) -> bool:
    """Path.is_relative_to() was added in Python 3.9; provide compat shim."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _touches_secrets(targets: list[str]) -> bool:
    """Return True if any target matches a known secret pattern."""
    for t in targets:
        t_lower = t.lower()
        for pat in _SECRET_PATH_PATTERNS:
            if pat.lower() in t_lower:
                return True
        for pat in _SECRET_CMD_PATTERNS:
            if pat.lower() in t_lower:
                return True
    return False


def _categorize(
    tool_name: str,
    action_type: ActionType,
    targets: list[str],
    inside_workspace: bool,
    touches_secrets: bool,
) -> str:
    """
    Map a normalized tool call to a taxonomy category id.

    Evaluation order mirrors policy.yaml:
      1. supervisor_path_access  (any action — hard-rail category)
      2. workspace_outside_write (write outside workspace)
      3. network_request         (exec matching network patterns)
      4. secret_access           (touches secrets)
      5. file_read / file_write  (normal workspace access)
      6. shell_exec              (exec not matched above)
      7. unclassified            (catch-all)
    """
    # 1. Supervisor path — checked on any action type
    for t in targets:
        t_lower = t.lower().replace("\\", "/")
        for pat in _SUPERVISOR_PATH_PATTERNS:
            if pat.lower() in t_lower:
                return "supervisor_path_access"

    # 2. Write outside workspace
    if action_type == ActionType.WRITE and not inside_workspace:
        return "workspace_outside_write"

    # 3. Network request (exec tools only)
    if action_type == ActionType.EXEC:
        cmd_lower = " ".join(targets).lower()
        for pat in _NETWORK_CMD_PATTERNS:
            if pat in cmd_lower:
                return "network_request"

    # 4. Secret access (any tool that touches a secret pattern)
    if touches_secrets:
        return "secret_access"

    # 5. Normal file access
    if action_type == ActionType.READ:
        return "file_read"
    if action_type == ActionType.WRITE:
        return "file_write"

    # 6. Shell execution (non-network, non-secret)
    if action_type == ActionType.EXEC:
        return "shell_exec"

    # 7. Catch-all
    return "unclassified"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize(
    payload: dict[str, Any],
    workspace_roots: list[str],
    prompt_id: Optional[str] = None,
    prompt_text: Optional[str] = None,
) -> PermissionRequest:
    """
    Convert a raw Bob PreToolUse hook payload into a PermissionRequest.

    Args:
        payload:         Raw JSON dict from Bob's PreToolUse hook stdin.
        workspace_roots: List of absolute paths that count as workspace.
                         Typically one path (the open project folder).
        prompt_id:       From Context Store — the UserPromptSubmit id for
                         the current task.  None if not yet recorded.
        prompt_text:     From Context Store — the prompt text.  None if
                         prompt_id is None.

    Returns:
        A fully populated PermissionRequest.

    Raises:
        NormalizerError: if required fields (tool_use_id, session_id,
                         tool_name) are missing from the payload.
    """
    tool_use_id: str = payload.get("tool_use_id", "")
    session_id: str = payload.get("session_id", "")
    tool_name: str = payload.get("tool_name", "")
    tool_input: dict = payload.get("tool_input", {})
    cwd: str = payload.get("cwd", "")

    if not tool_use_id:
        raise NormalizerError("Payload missing required field: tool_use_id")
    if not session_id:
        raise NormalizerError("Payload missing required field: session_id")
    if not tool_name:
        raise NormalizerError("Payload missing required field: tool_name")

    action_type = _action_type(tool_name)
    targets = _extract_targets(tool_name, tool_input)
    inside_ws = _inside_workspace(targets, cwd, workspace_roots)
    sec = _touches_secrets(targets)
    category = _categorize(tool_name, action_type, targets, inside_ws, sec)

    return PermissionRequest(
        tool_use_id=tool_use_id,
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        cwd=cwd,
        action_type=action_type,
        category=category,
        targets=targets,
        inside_workspace=inside_ws,
        touches_secrets=sec,
        prompt_id=prompt_id,
        prompt_text=prompt_text,
    )
