"""
ai_permission_supervisor/cli/paths.py — Locations used by the `aps` CLI.

Every location can be overridden with an environment variable, which the
tests use to work in a temporary directory. Package locations come from
ai_permission_supervisor.config, shared with the service.
"""

from __future__ import annotations

import os
from pathlib import Path

from ai_permission_supervisor import config

PACKAGE_DIR: Path = config.PACKAGE_DIR
HOOK_SCRIPT: Path = config.HOOK_SCRIPT
PACKAGED_POLICY_DIR: Path = config.PACKAGED_POLICY_DIR

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9001


def state_dir() -> Path:
    """Hook-settings backups, user policy, .env, pid file, and service log."""
    return config.state_dir()


def policy_dir() -> Path:
    """The policy directory the service will load."""
    return config.policy_dir()


def bob_settings_path() -> Path:
    """Bob's global settings file (shared by the Bob IDE and Bob Shell)."""
    return Path(os.environ.get("BOB_SETTINGS_PATH", Path.home() / ".bob" / "settings" / "settings.json"))


def supervisor_host() -> str:
    return os.environ.get("SUPERVISOR_HOST", DEFAULT_HOST)


def supervisor_port() -> int:
    return int(os.environ.get("SUPERVISOR_PORT", str(DEFAULT_PORT)))


def supervisor_url() -> str:
    return os.environ.get("SUPERVISOR_URL", f"http://{supervisor_host()}:{supervisor_port()}").rstrip("/")


def package_is_complete() -> bool:
    """True when the hook client and the default policy shipped with the package."""
    return (
        HOOK_SCRIPT.is_file()
        and (PACKAGED_POLICY_DIR / "policy.yaml").is_file()
        and (PACKAGED_POLICY_DIR / "taxonomy.yaml").is_file()
        and (PACKAGE_DIR / "supervisor" / "app.py").is_file()
    )
