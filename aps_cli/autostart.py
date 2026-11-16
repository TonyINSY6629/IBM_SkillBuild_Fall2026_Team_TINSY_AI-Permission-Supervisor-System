"""
aps_cli/autostart.py — Start the Supervisor automatically at login.

The Supervisor must be running before Bob is used; otherwise every tool call
is denied (fail-closed). Starting it from a Bob SessionStart hook is not
reliable: SessionStart cannot block, and a server launched from a hook can be
killed together with the hook process. So we register it with the OS instead:

  Windows : a .cmd file in the user's Startup folder (no admin rights needed)
  macOS   : a launchd LaunchAgent (~/Library/LaunchAgents)
  Linux   : a systemd user service (~/.config/systemd/user)
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from aps_cli import paths

LABEL = "com.team-tinsy.ai-permission-supervisor"
SYSTEMD_UNIT = "ai-permission-supervisor.service"
WINDOWS_FILE = "ai-permission-supervisor.cmd"


@dataclass
class AutostartPlan:
    system: str          # "windows" | "macos" | "linux"
    file: Path           # file to write / remove
    content: str         # file contents
    activate: list[list[str]]    # commands to run after writing
    deactivate: list[list[str]]  # commands to run before removing


def _system() -> str:
    name = platform.system()
    if name == "Windows":
        return "windows"
    if name == "Darwin":
        return "macos"
    return "linux"


def _windowless_python(python: Path) -> Path:
    """pythonw.exe avoids a console window at login, when it exists."""
    candidate = python.with_name("pythonw.exe")
    return candidate if candidate.exists() else python


def build_plan(python: str | Path | None = None, system: str | None = None, home: Path | None = None) -> AutostartPlan:
    python = Path(python or sys.executable)
    system = system or _system()
    home = home or Path.home()
    repo = paths.REPO_ROOT
    log_file = paths.state_dir() / "supervisor.log"

    if system == "windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        content = (
            "@echo off\r\n"
            "rem AI Permission Supervisor - starts the local Supervisor service at login.\r\n"
            "rem Remove with: aps autostart disable\r\n"
            f'cd /d "{repo}"\r\n'
            f'start "" "{_windowless_python(python)}" -m aps_cli start\r\n'
        )
        return AutostartPlan("windows", startup / WINDOWS_FILE, content, [], [])

    if system == "macos":
        plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        args = "".join(f"\n    <string>{escape(str(a))}</string>" for a in (python, "-m", "aps_cli", "start", "--foreground"))
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>WorkingDirectory</key>
  <string>{escape(str(repo))}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{escape(str(log_file))}</string>
  <key>StandardErrorPath</key>
  <string>{escape(str(log_file))}</string>
</dict>
</plist>
"""
        return AutostartPlan(
            "macos", plist, content,
            activate=[["launchctl", "load", "-w", str(plist)]],
            deactivate=[["launchctl", "unload", "-w", str(plist)]],
        )

    unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    content = f"""[Unit]
Description=AI Permission Supervisor (local guardrail for AI coding agents)

[Service]
WorkingDirectory={repo}
ExecStart="{python}" -m aps_cli start --foreground
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
    return AutostartPlan(
        "linux", unit, content,
        activate=[["systemctl", "--user", "daemon-reload"],
                  ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT]],
        deactivate=[["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT]],
    )


def _run(commands: list[list[str]]) -> list[str]:
    """Run commands; return error messages instead of raising."""
    errors = []
    for cmd in commands:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{' '.join(cmd)}: {exc}")
            continue
        if done.returncode != 0:
            errors.append(f"{' '.join(cmd)}: {(done.stderr or done.stdout).strip()}")
    return errors


def enable(plan: AutostartPlan, activate: bool = True) -> list[str]:
    plan.file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    newline = "" if plan.system == "windows" else None
    with open(plan.file, "w", encoding="utf-8", newline=newline) as f:
        f.write(plan.content)
    return _run(plan.activate) if activate else []


def disable(plan: AutostartPlan, activate: bool = True) -> tuple[bool, list[str]]:
    errors = _run(plan.deactivate) if activate and plan.file.exists() else []
    existed = plan.file.exists()
    if existed:
        plan.file.unlink()
    return existed, errors
