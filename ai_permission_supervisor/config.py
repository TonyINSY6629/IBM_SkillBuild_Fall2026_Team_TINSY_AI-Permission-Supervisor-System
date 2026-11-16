"""
ai_permission_supervisor/config.py — Where the Supervisor finds its files.

Shared by the Supervisor service and the `aps` CLI, so both resolve paths the
same way whether the package runs from a source checkout or from an installed
wheel. Standard library only.

State directory (~/.ai_permission_supervisor, or APS_STATE_DIR):
  backups/         Bob settings backups written by `aps install`
  policy/          your editable policy (created by `aps policy init`)
  .env             settings for the service, e.g. COS credentials
  supervisor.log   service log
  supervisor.pid   pid of a service started with `aps start`
The normalizer treats this directory as a Supervisor path, so the agent can
neither read nor change it.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR: Path = Path(__file__).resolve().parent
PACKAGED_POLICY_DIR: Path = PACKAGE_DIR / "policy"
HOOK_SCRIPT: Path = PACKAGE_DIR / "hook_client" / "main.py"

# A source checkout has pyproject.toml next to the package; an installed
# wheel does not.
SOURCE_ROOT: Path | None = (
    PACKAGE_DIR.parent if (PACKAGE_DIR.parent / "pyproject.toml").is_file() else None
)


def state_dir() -> Path:
    return Path(os.environ.get("APS_STATE_DIR", Path.home() / ".ai_permission_supervisor"))


def policy_dir() -> Path:
    """POLICY_DIR if set, else your policy in the state directory, else the packaged default."""
    if os.environ.get("POLICY_DIR"):
        return Path(os.environ["POLICY_DIR"])
    user_policy = state_dir() / "policy"
    if (user_policy / "policy.yaml").is_file():
        return user_policy
    return PACKAGED_POLICY_DIR


def env_files() -> list[Path]:
    """.env files the service loads, highest priority first. Only existing files."""
    candidates: list[Path] = []
    if os.environ.get("APS_ENV_FILE"):
        candidates.append(Path(os.environ["APS_ENV_FILE"]))
    candidates.append(state_dir() / ".env")
    if SOURCE_ROOT is not None:
        candidates.append(SOURCE_ROOT / ".env")  # team development setup
    return [p for p in candidates if p.is_file()]


def load_env_files() -> None:
    """Load .env files without overriding variables already set. Needs python-dotenv."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for path in env_files():
        load_dotenv(dotenv_path=path, override=False)
