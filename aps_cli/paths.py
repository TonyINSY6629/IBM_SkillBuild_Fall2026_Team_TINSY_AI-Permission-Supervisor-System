"""
aps_cli/paths.py — Locations used by the `aps` CLI.

Every location can be overridden with an environment variable, which the
tests use to work in a temporary directory.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root (aps_cli/ lives directly inside it).
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
HOOK_SCRIPT: Path = REPO_ROOT / "hook_client" / "main.py"
POLICY_DIR: Path = REPO_ROOT / "policy"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9001


def state_dir() -> Path:
    """Directory for hook-settings backups, the pid file, and the service log.

    It is outside every workspace, and the normalizer treats it as a
    Supervisor path, so the agent cannot read or change it.
    """
    return Path(os.environ.get("APS_STATE_DIR", Path.home() / ".ai_permission_supervisor"))


def bob_settings_path() -> Path:
    """Bob's global settings file (shared by the Bob IDE and Bob Shell)."""
    return Path(os.environ.get("BOB_SETTINGS_PATH", Path.home() / ".bob" / "settings" / "settings.json"))


def supervisor_host() -> str:
    return os.environ.get("SUPERVISOR_HOST", DEFAULT_HOST)


def supervisor_port() -> int:
    return int(os.environ.get("SUPERVISOR_PORT", str(DEFAULT_PORT)))


def supervisor_url() -> str:
    return os.environ.get("SUPERVISOR_URL", f"http://{supervisor_host()}:{supervisor_port()}").rstrip("/")


def repo_is_complete() -> bool:
    """True when the CLI runs from a full clone (the supported v0.1 install)."""
    return HOOK_SCRIPT.is_file() and (POLICY_DIR / "policy.yaml").is_file() and (REPO_ROOT / "supervisor" / "app.py").is_file()
