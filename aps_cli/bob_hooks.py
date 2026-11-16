"""
aps_cli/bob_hooks.py — Add and remove the Supervisor hooks in Bob's settings.

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
