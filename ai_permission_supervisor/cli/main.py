"""
ai_permission_supervisor/cli/main.py — `aps`: install, run, and check the Supervisor.

Commands:
  aps install [--bob-auto-approve]   Register the Supervisor hooks in Bob's global settings
  aps uninstall                      Remove them again (other settings are left untouched)
  aps start | stop | status          Run the Supervisor service in the background
  aps doctor                         Check the whole setup and explain what is wrong
  aps autostart enable|disable       Start the service automatically at login
  aps policy path | init             Show or create your editable policy

Once the hooks are installed, Bob's tool calls are DENIED whenever the
Supervisor service is not running (fail-closed). Keep it running, enable
autostart, or run `aps uninstall`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from ai_permission_supervisor import config
from ai_permission_supervisor.cli import __version__, autostart, bob_hooks, paths

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
AUTO_APPROVE_RECORD = "bob_auto_approve.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aps() -> str:
    """How to run this CLI on this machine, for hints: `aps` if it is on PATH,
    otherwise the `python -m` form (Python's Scripts folder is often not on PATH)."""
    if shutil.which("aps"):
        return "aps"
    return f'"{sys.executable}" -m ai_permission_supervisor.cli'


def _hook_python(explicit: str | None) -> Path:
    """Interpreter written into the hook command.

    Must be a console interpreter: the Hook Client reports deny reasons on
    stderr, which pythonw.exe does not have. An absolute path also avoids the
    Windows `python3` alias that opens the Microsoft Store.
    """
    python = Path(explicit or sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    return python


def _health(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{paths.supervisor_url()}/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _pid_file() -> Path:
    return paths.state_dir() / "supervisor.pid"


def _log_file() -> Path:
    return paths.state_dir() / "supervisor.log"


def _record_file() -> Path:
    return paths.state_dir() / AUTO_APPROVE_RECORD


def _smoke_test_hook(python: Path) -> tuple[bool, str]:
    """Run the Hook Client with a SessionStart payload. It must exit 0."""
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "aps-smoke-test", "cwd": str(Path.home())})
    try:
        done = subprocess.run(
            [str(python), str(paths.HOOK_SCRIPT)],
            input=payload, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, f"exit code {done.returncode}: {done.stderr.strip()}"
    return True, "hook client runs"


def _require_package() -> bool:
    if paths.package_is_complete():
        return True
    print(
        f"The Supervisor's files are missing from {paths.PACKAGE_DIR}.\n"
        "Reinstall with: pip install --force-reinstall ai-permission-supervisor",
        file=sys.stderr,
    )
    return False


def _read_settings(path: Path) -> dict | None:
    try:
        return bob_hooks.load_settings(path)
    except bob_hooks.SettingsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _dotenv() -> dict[str, str]:
    """Values from the .env files the service loads (first file wins)."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    merged: dict[str, str] = {}
    for path in reversed(config.env_files()):
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    return merged


def _timeouts_from_env() -> tuple[float, float]:
    """Escalation timeout (the service also reads .env) and Hook Client deadline
    (the Hook Client does not read .env, only the environment Bob gives it)."""
    service_env = {**_dotenv(), **os.environ}
    return (
        float(service_env.get("ESCALATION_TIMEOUT_SECONDS", "120")),
        float(os.environ.get("HOOK_DEADLINE_SECONDS", "130")),
    )


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    if not _require_package():
        return 1
    settings_path = paths.bob_settings_path()
    settings = _read_settings(settings_path)
    if settings is None:
        return 1

    python = _hook_python(args.python)
    if not python.exists():
        print(f"Error: Python interpreter not found: {python}", file=sys.stderr)
        return 1
    command = bob_hooks.hook_command(python, paths.HOOK_SCRIPT)
    updated = bob_hooks.add_supervisor_hooks(settings, command)
    if args.remove_probe:
        updated, _ = bob_hooks.remove_probe_hooks(updated)

    record = None
    if args.bob_auto_approve:
        forbidden = bob_hooks.forbidden_auto_approve_groups(updated)
        if forbidden:
            print(f"Error: your Bob settings forbid auto-approve for: {', '.join(forbidden)}", file=sys.stderr)
            return 1
        try:
            updated, record = bob_hooks.enable_auto_approve(updated)
        except bob_hooks.SettingsError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.dry_run:
        print(f"Would write {settings_path}:")
        print(json.dumps({k: updated[k] for k in ("hooks", "approval") if k in updated}, indent=2))
        return 0

    if updated == settings:
        print(f"Already installed in {settings_path}")
    else:
        backup = bob_hooks.backup_settings(settings_path, paths.state_dir() / "backups")
        bob_hooks.write_settings(settings_path, updated)
        print(f"Installed Supervisor hooks in {settings_path}")
        if backup:
            print(f"  Backup of previous settings: {backup}")
    if record and (record["groups"] or record["added_commands"]):
        previous = {}
        if _record_file().exists():
            previous = json.loads(_record_file().read_text(encoding="utf-8"))
        merged = {
            "groups": sorted(set(previous.get("groups", [])) | set(record["groups"])),
            "added_commands": sorted(set(previous.get("added_commands", [])) | set(record["added_commands"])),
            "added_star": previous.get("added_star", False),  # legacy records only
            "created_executor": previous.get("created_executor", False) or record["created_executor"],
            "created_keys": sorted(set(previous.get("created_keys", [])) | set(record["created_keys"])),
            "created_approval": previous.get("created_approval", False) or record["created_approval"],
        }
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        _record_file().write_text(json.dumps(merged, indent=2), encoding="utf-8")
        print("  Bob auto-approve turned on for read, edit, and everyday shell commands\n"
              "  (git, python, npm, ...); the Supervisor is now the single gate")

    ok, detail = _smoke_test_hook(python)
    print(f"  Hook client check: {'ok' if ok else 'FAILED - ' + detail}")
    if bob_hooks.has_probe_hooks(updated):
        print("  Note: Step 1 probe hooks are still installed. Remove them with: aps install --remove-probe")
    if not args.bob_auto_approve and "execute" not in bob_hooks.auto_approve_status(updated)[0]:
        print("  Note: Bob will also show its own prompt for shell commands. To avoid two prompts:\n"
              f"        {_aps()} install --bob-auto-approve")

    aps = _aps()
    print(
        "\nNext steps:\n"
        f"  1. Start the service:   {aps} start\n"
        f"     and at every login:  {aps} autostart enable\n"
        "  2. Restart Bob so it loads the new settings.\n"
        "While the service is not running, Bob's tool calls are denied (fail-closed)."
    )
    return 0 if ok else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    settings_path = paths.bob_settings_path()
    settings = _read_settings(settings_path)
    if settings is None:
        return 1
    updated, removed = bob_hooks.remove_hooks(settings, include_probe=args.remove_probe)

    reverted = False
    if _record_file().exists():
        record = json.loads(_record_file().read_text(encoding="utf-8"))
        updated = bob_hooks.revert_auto_approve(updated, record)
        reverted = True

    if updated == settings:
        print(f"No Supervisor hooks found in {settings_path}")
    else:
        backup = bob_hooks.backup_settings(settings_path, paths.state_dir() / "backups")
        bob_hooks.write_settings(settings_path, updated)
        print(f"Removed {removed} hook entr{'y' if removed == 1 else 'ies'} from {settings_path}")
        if reverted:
            print("  Turned off the Bob auto-approve settings that `aps install` had added")
        if backup:
            print(f"  Backup of previous settings: {backup}")
    if reverted:
        _record_file().unlink()

    # Bob's docs do not say what "execute" alone (no command list) approves,
    # so warn whenever it is on.
    groups, _commands = bob_hooks.auto_approve_status(updated)
    if "execute" in groups:
        print("\nWarning: Bob still auto-approves shell commands. Without the Supervisor they now run\n"
              "unchecked. Turn auto-approve off in Bob's settings unless you want that.")
    print(f"Restart Bob for the change to take effect. Disable autostart with: {_aps()} autostart disable")
    return 0


# ---------------------------------------------------------------------------
# start / stop / status
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    if not _require_package():
        return 1
    if _health():
        print(f"Supervisor already running at {paths.supervisor_url()}")
        return 0
    host, port = paths.supervisor_host(), paths.supervisor_port()
    paths.state_dir().mkdir(parents=True, exist_ok=True)

    # The React pop-up (UI/escalation-popup-src) only works while its dev server
    # runs. Installed users get the approval page built into the service instead,
    # unless they set ESCALATION_POPUP_URL in the environment or in a .env file.
    # Not "": Windows drops empty environment variables before the child sees them.
    if "ESCALATION_POPUP_URL" not in os.environ and "ESCALATION_POPUP_URL" not in _dotenv():
        os.environ["ESCALATION_POPUP_URL"] = "builtin"

    app_path = "ai_permission_supervisor.supervisor.app:app"
    if args.foreground:
        # Used by launchd / systemd, which manage the process themselves.
        import uvicorn
        uvicorn.run(app_path, host=host, port=port, log_level="info")
        return 0

    command = [sys.executable, "-m", "uvicorn", app_path, "--host", host, "--port", str(port)]
    kwargs: dict = {}
    if os.name == "nt":
        create_no_window, new_group = 0x08000000, 0x00000200
        kwargs["creationflags"] = create_no_window | new_group
    else:
        kwargs["start_new_session"] = True
    with open(_log_file(), "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=paths.state_dir(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )
    _pid_file().write_text(str(process.pid), encoding="utf-8")

    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"Supervisor exited during startup (code {process.returncode}). See {_log_file()}", file=sys.stderr)
            _pid_file().unlink(missing_ok=True)
            return 1
        info = _health(timeout=1)
        if info:
            print(f"Supervisor running at {paths.supervisor_url()} (pid {process.pid}, policy {info.get('policy_version')})")
            print(f"  Log: {_log_file()}")
            return 0
        time.sleep(0.5)
    print(f"Supervisor did not answer within {args.wait:.0f} s. See {_log_file()}", file=sys.stderr)
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    pid_file = _pid_file()
    if not pid_file.exists():
        print("No service started by `aps start` was found.")
        if _health():
            print("A Supervisor is running but was started another way (autostart service or by hand).")
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError):
        print("The recorded process is no longer running.")
    except OSError as exc:
        print(f"Could not stop process: {exc}. If it is no longer running, delete {pid_file}", file=sys.stderr)
        return 1
    pid_file.unlink(missing_ok=True)
    print(f"Supervisor stopped. Bob's tool calls are now denied until it is started again (or run: {_aps()} uninstall).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    info = _health()
    if info:
        print(f"Service : running at {paths.supervisor_url()} (policy {info.get('policy_version')})")
    else:
        print(f"Service : NOT running at {paths.supervisor_url()} - Bob's tool calls are denied while it is down")
    try:
        installed = bob_hooks.installed_timeouts(bob_hooks.load_settings(paths.bob_settings_path()))
    except bob_hooks.SettingsError as exc:
        print(f"Hooks   : cannot read Bob settings ({exc})")
        return 1
    missing = [e for e in bob_hooks.HOOK_EVENTS if e not in installed]
    if not installed:
        print("Hooks   : not installed (run: aps install)")
    elif missing:
        print(f"Hooks   : partially installed, missing {', '.join(missing)} (run: aps install)")
    else:
        print("Hooks   : installed")
    print(f"Policy  : {paths.policy_dir()}")
    return 0 if info and not missing else 1


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    results: list[tuple[str, str, str]] = []

    def check(status: str, name: str, detail: str = "") -> None:
        results.append((status, name, detail))

    check(OK if sys.version_info >= (3, 10) else FAIL, "Python 3.10+", sys.version.split()[0])
    check(OK if paths.package_is_complete() else FAIL, "Supervisor files", str(paths.PACKAGE_DIR))

    missing = []
    for module in ("fastapi", "uvicorn", "yaml", "pydantic", "dotenv"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    check(FAIL if missing else OK, "Runtime packages",
          f"missing: {', '.join(missing)} (pip install ai-permission-supervisor)" if missing else "")

    policy_dir = paths.policy_dir()
    try:
        import yaml
        policy = yaml.safe_load((policy_dir / "policy.yaml").read_text(encoding="utf-8")) or {}
        rails = len(policy.get("hard_rails", []))
        check(OK if rails else FAIL, "Policy file",
              f"version {policy.get('meta', {}).get('version_tag')}, {rails} hard rails, {policy_dir}")
    except Exception as exc:  # noqa: BLE001 - report any problem
        check(FAIL, "Policy file", str(exc))

    settings_path = paths.bob_settings_path()
    installed: dict = {}
    try:
        settings = bob_hooks.load_settings(settings_path)
        installed = bob_hooks.installed_timeouts(settings)
        missing_events = [e for e in bob_hooks.HOOK_EVENTS if e not in installed]
        if not installed:
            check(FAIL, "Bob hooks", f"not installed in {settings_path} (run: aps install)")
        elif missing_events:
            check(FAIL, "Bob hooks", f"missing {', '.join(missing_events)} (run: aps install)")
        else:
            check(OK, "Bob hooks", str(settings_path))
        if bob_hooks.has_probe_hooks(settings):
            check(WARN, "Probe hooks", "Step 1 probe hooks still installed (aps install --remove-probe)")
        groups, commands = bob_hooks.auto_approve_status(settings)
        if not installed and "execute" in groups:
            check(FAIL, "Bob auto-approve", "shell commands are auto-approved but no Supervisor hook is installed: they run unchecked")
        elif installed and "execute" not in groups:
            check(WARN, "Bob auto-approve", "off for shell commands, so Bob also prompts (aps install --bob-auto-approve)")
        elif installed and not bob_hooks.has_effective_command_prefixes(commands):
            check(WARN, "Bob auto-approve", "no command prefixes listed (Bob ignores \"*\"), so Bob also prompts "
                  "(aps install --bob-auto-approve)")
        elif installed:
            check(OK, "Bob auto-approve", f"on for {len(commands)} command prefixes; the Supervisor is the single gate")
    except bob_hooks.SettingsError as exc:
        check(FAIL, "Bob hooks", str(exc))

    escalation, deadline = _timeouts_from_env()
    hook_timeout = installed.get("PreToolUse")
    if hook_timeout is None:
        check(WARN, "Timeout ordering", "PreToolUse hook not installed")
    elif hook_timeout == 0 or escalation < deadline < hook_timeout:
        check(OK, "Timeout ordering", f"escalation {escalation:g}s < hook client {deadline:g}s < Bob hook {hook_timeout}s")
    else:
        check(FAIL, "Timeout ordering",
              f"need escalation < hook client < Bob hook, got {escalation:g}s / {deadline:g}s / {hook_timeout}s; "
              "a hook killed by Bob's timeout lets the tool run")

    if paths.package_is_complete():
        ok, detail = _smoke_test_hook(_hook_python(None))
        check(OK if ok else FAIL, "Hook client", detail)

    info = _health()
    check(OK if info else FAIL, "Supervisor service",
          f"running at {paths.supervisor_url()}" if info else f"not reachable at {paths.supervisor_url()} (run: aps start)")

    width = max(len(name) for _, name, _ in results)
    for status, name, detail in results:
        print(f"[{status}] {name.ljust(width)}  {detail}".rstrip())
    failed = sum(1 for status, _, _ in results if status == FAIL)
    print(f"\n{failed} problem{'s' if failed != 1 else ''} found." if failed else "\nAll checks passed.")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# autostart / policy
# ---------------------------------------------------------------------------

def cmd_autostart(args: argparse.Namespace) -> int:
    if not _require_package():
        return 1
    plan = autostart.build_plan()
    if args.action == "enable":
        errors = autostart.enable(plan, activate=not args.no_activate)
        print(f"Autostart enabled: {plan.file}")
        for error in errors:
            print(f"  Warning: {error}", file=sys.stderr)
        if plan.system == "windows":
            print(f"  The service will start at your next login. Start it now with: {_aps()} start")
        return 1 if errors else 0
    existed, errors = autostart.disable(plan, activate=not args.no_activate)
    print(f"Autostart disabled: removed {plan.file}" if existed else "Autostart was not enabled.")
    for error in errors:
        print(f"  Warning: {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_policy(args: argparse.Namespace) -> int:
    if args.action == "path":
        print(paths.policy_dir())
        return 0
    target = paths.state_dir() / "policy"
    if (target / "policy.yaml").exists() and not args.force:
        print(f"Your policy already exists: {target} (use --force to replace it with the default)")
        return 1
    target.mkdir(parents=True, exist_ok=True)
    for name in ("policy.yaml", "taxonomy.yaml"):
        shutil.copy2(paths.PACKAGED_POLICY_DIR / name, target / name)
    print(f"Created your editable policy in {target}")
    print("Edit policy.yaml there, then restart the service (aps stop, aps start) to switch to it.\n"
          "After that, edits are picked up within 5 minutes. The agent cannot read or change this folder.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aps", description="AI Permission Supervisor for IBM Bob.")
    parser.add_argument("--version", action="version", version=f"aps {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="register the Supervisor hooks in Bob's global settings")
    p.add_argument("--python", help="interpreter for the hook command (default: this one)")
    p.add_argument("--bob-auto-approve", action="store_true",
                   help="turn on Bob's own auto-approve for read/edit/execute so the Supervisor is the single gate")
    p.add_argument("--remove-probe", action="store_true", help="also remove Step 1 probe hooks")
    p.add_argument("--dry-run", action="store_true", help="show the result without writing anything")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove the Supervisor hooks (and auto-approve it added)")
    p.add_argument("--remove-probe", action="store_true", help="also remove Step 1 probe hooks")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("start", help="start the Supervisor service")
    p.add_argument("--foreground", action="store_true", help="run in this process (for launchd/systemd)")
    p.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the service (default 20)")
    p.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="stop a service started with `aps start`").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="show service and hook status").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="check the whole setup").set_defaults(func=cmd_doctor)

    p = sub.add_parser("autostart", help="start the service automatically at login")
    p.add_argument("action", choices=["enable", "disable"])
    p.add_argument("--no-activate", action="store_true", help="only write/remove the file, do not call launchctl/systemctl")
    p.set_defaults(func=cmd_autostart)

    p = sub.add_parser("policy", help="show or create your editable policy")
    p.add_argument("action", choices=["path", "init"])
    p.add_argument("--force", action="store_true", help="init: replace an existing policy with the default")
    p.set_defaults(func=cmd_policy)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
