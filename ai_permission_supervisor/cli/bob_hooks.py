"""
ai_permission_supervisor/cli/bob_hooks.py — Supervisor hooks and auto-approve in Bob's settings.

Bob merges hooks from ~/.bob/settings/settings.json (global) and the
workspace's .bob/settings.json. The Supervisor is registered globally only:
workspace settings live inside the agent's workspace, where the agent could
edit them.

Rules followed here:
  - Never discard settings the user already has. Only hook entries whose
    command runs hook_client/main.py are touched.
  - Installing twice leaves exactly one Supervisor entry per event.
  - An unreadable settings file is never overwritten.
  - Every write is atomic (temp file + replace), preceded by a backup.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Event -> (uses a tool-name matcher, timeout in seconds).
# PreToolUse must outlast the Hook Client deadline (130 s), which outlasts the
# escalation timeout (120 s): 120 < 130 < 150. A hook killed by Bob's timeout
# fails OPEN, so this ordering is what keeps escalations fail-closed.
HOOK_EVENTS: dict[str, tuple[bool, int]] = {
    "PreToolUse": (True, 150),
    "UserPromptSubmit": (False, 10),
    "PostToolUse": (True, 10),
    "Stop": (False, 10),
}


class SettingsError(Exception):
    """The settings file cannot be read or has an unexpected shape."""


def hook_command(python: str | Path, script: str | Path) -> str:
    """Command string Bob runs (via `sh -c` or `cmd /c`). Both paths quoted."""
    return f'"{python}" "{script}"'


def _norm(command: str) -> str:
    return command.replace("\\", "/").lower()


def is_supervisor_command(command: str) -> bool:
    return "hook_client/main.py" in _norm(command)


def is_probe_command(command: str) -> bool:
    return "probe.py" in _norm(command)


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc}); fix it by hand, nothing was changed") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} must contain a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError(f'"hooks" in {path} must be an object')
    return data


def _strip(hooks: dict[str, Any], predicate) -> int:
    """Remove hook entries whose command matches predicate. Returns count removed."""
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            inner = group.get("hooks", []) if isinstance(group, dict) else []
            kept = [h for h in inner if not (isinstance(h, dict) and predicate(str(h.get("command", ""))))]
            removed += len(inner) - len(kept)
            if kept:
                kept_groups.append({**group, "hooks": kept})
            elif not inner:
                kept_groups.append(group)  # leave unrecognised groups alone
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return removed


def add_supervisor_hooks(settings: dict[str, Any], command: str) -> dict[str, Any]:
    """Return a copy of settings with exactly one Supervisor hook per event."""
    result = copy.deepcopy(settings)
    hooks = result.setdefault("hooks", {})
    _strip(hooks, is_supervisor_command)
    for event, (uses_matcher, timeout) in HOOK_EVENTS.items():
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if uses_matcher:
            group = {"matcher": ".*", **group}
        hooks.setdefault(event, []).append(group)
    return result


def remove_hooks(settings: dict[str, Any], *, include_probe: bool = False) -> tuple[dict[str, Any], int]:
    """Return a copy of settings without Supervisor (and optionally probe) hooks."""
    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result, 0
    removed = _strip(hooks, is_supervisor_command)
    if include_probe:
        removed += _strip(hooks, is_probe_command)
    if not hooks:
        del result["hooks"]
    return result, removed


def remove_probe_hooks(settings: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return a copy of settings without the Step 1 probe hooks."""
    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result, 0
    removed = _strip(hooks, is_probe_command)
    if not hooks:
        del result["hooks"]
    return result, removed


def installed_timeouts(settings: dict[str, Any]) -> dict[str, int | None]:
    """Event -> timeout of the Supervisor hook, for events where it is installed."""
    found: dict[str, int | None] = {}
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for h in group.get("hooks", []) if isinstance(group, dict) else []:
                if isinstance(h, dict) and is_supervisor_command(str(h.get("command", ""))):
                    found[event] = h.get("timeout")
    return found


def has_probe_hooks(settings: dict[str, Any]) -> bool:
    for groups in (settings.get("hooks") or {}).values():
        for group in groups if isinstance(groups, list) else []:
            for h in group.get("hooks", []) if isinstance(group, dict) else []:
                if isinstance(h, dict) and is_probe_command(str(h.get("command", ""))):
                    return True
    return False


def backup_settings(path: Path, backup_dir: Path) -> Path | None:
    """Copy the current settings file into backup_dir. None if there is no file."""
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"settings.{stamp}.json"
    shutil.copy2(path, target)
    return target


def write_settings(path: Path, settings: dict[str, Any]) -> None:
    """Write settings atomically: a crash mid-write never leaves a broken file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Bob auto-approve
#
# Bob has its own approval prompt in front of tools. With the Supervisor
# installed that means two prompts per command. Turning on Bob's auto-approve
# for the groups the Supervisor checks makes the Supervisor the single gate.
# The hook still runs first, so this is only safe while the hooks are
# installed: `aps uninstall` reverts exactly what `aps install` added.
#
# Format (Bob docs, "Approval settings"):
#   "approval": {
#     "allowed_permissions": ["read", "edit", "execute", ...],
#     "allowedExecutors": [{"toolId": "execute_command", "approvedCommands": [...]}]
#   }
# MCP is deliberately not auto-approved: it is not yet confirmed that Bob's
# PreToolUse hook fires for MCP tool calls.
# ---------------------------------------------------------------------------

AUTO_APPROVE_GROUPS: tuple[str, ...] = ("read", "edit", "execute")

# Bob matches approvedCommands by PREFIX ("git" approves "git push ..."), and
# does not document a wildcard: in testing, "*" alone left Bob prompting for
# every unlisted command. So we list everyday command prefixes. Every command
# still passes through the Supervisor's hook first; this only removes Bob's
# second prompt. Destructive commands (del, rm, rmdir, format, ...) are left
# off on purpose, so Bob keeps asking for those as an extra layer.
COMMAND_PREFIXES: tuple[str, ...] = (
    "git", "python", "py", "pip", "node", "npm", "npx", "yarn", "pnpm",
    "pytest", "uvicorn", "dir", "ls", "cd", "pwd", "echo", "type", "cat",
    "where", "which", "mkdir", "Get-", "Test-Path", "Select-String",
)
LEGACY_WILDCARD = "*"


def _execute_entry(approval: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (e for e in approval.get("allowedExecutors", []) or []
         if isinstance(e, dict) and e.get("toolId") == "execute_command"),
        None,
    )


def auto_approve_status(settings: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(auto-approved groups, approved shell command prefixes)."""
    approval = settings.get("approval") if isinstance(settings.get("approval"), dict) else {}
    groups = {g for g in approval.get("allowed_permissions", []) or [] if isinstance(g, str)}
    entry = _execute_entry(approval) or {}
    commands = {c for c in entry.get("approvedCommands", []) or [] if isinstance(c, str)}
    return groups, commands


def has_effective_command_prefixes(commands: set[str]) -> bool:
    """True if Bob would actually auto-approve everyday commands (not just "*")."""
    return any(c != LEGACY_WILDCARD for c in commands)


def forbidden_auto_approve_groups(settings: dict[str, Any]) -> list[str]:
    """Groups an administrator has forbidden from auto-approval."""
    approval = settings.get("approval") if isinstance(settings.get("approval"), dict) else {}
    forbidden = approval.get("forbiddenApprovalGroups") or []
    return [g for g in AUTO_APPROVE_GROUPS if g in forbidden]


def enable_auto_approve(settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (new settings, record of what was added) — the record is used to undo it."""
    result = copy.deepcopy(settings)
    created_approval = "approval" not in result
    approval = result.setdefault("approval", {})
    if not isinstance(approval, dict):
        raise SettingsError('"approval" in Bob settings must be an object')
    created_keys = [k for k in ("allowed_permissions", "allowedExecutors") if k not in approval]
    permissions = approval.setdefault("allowed_permissions", [])
    added_groups = [g for g in AUTO_APPROVE_GROUPS if g not in permissions]
    permissions.extend(added_groups)

    executors = approval.setdefault("allowedExecutors", [])
    entry = _execute_entry(approval)
    created_entry = entry is None
    if created_entry:
        entry = {"toolId": "execute_command", "approvedCommands": []}
        executors.append(entry)
    commands = entry.setdefault("approvedCommands", [])
    added_commands = [c for c in COMMAND_PREFIXES if c not in commands]
    commands.extend(added_commands)
    return result, {
        "groups": added_groups, "added_commands": added_commands, "created_executor": created_entry,
        "created_keys": created_keys, "created_approval": created_approval,
    }


def revert_auto_approve(settings: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Undo exactly what enable_auto_approve added; leave anything else alone."""
    result = copy.deepcopy(settings)
    approval = result.get("approval")
    if not isinstance(approval, dict):
        return result
    permissions = approval.get("allowed_permissions")
    if isinstance(permissions, list):
        approval["allowed_permissions"] = [g for g in permissions if g not in record.get("groups", [])]
    executors = approval.get("allowedExecutors")
    if isinstance(executors, list):
        kept = []
        for e in executors:
            if isinstance(e, dict) and e.get("toolId") == "execute_command":
                added = set(record.get("added_commands", []))
                if record.get("added_star"):  # records written by aps 0.1.0 before this fix
                    added.add(LEGACY_WILDCARD)
                if added:
                    e = {**e, "approvedCommands": [c for c in e.get("approvedCommands", []) if c not in added]}
                if record.get("created_executor") and not e.get("approvedCommands") and set(e) <= {"toolId", "approvedCommands"}:
                    continue
            kept.append(e)
        approval["allowedExecutors"] = kept
    for key in record.get("created_keys", []):
        if approval.get(key) == []:
            del approval[key]
    if record.get("created_approval") and not approval:
        del result["approval"]
    return result
