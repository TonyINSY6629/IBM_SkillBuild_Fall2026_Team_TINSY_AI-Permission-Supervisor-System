"""
aps_cli/main.py — `aps`: install, run, and check the AI Permission Supervisor.

Commands:
  aps install    Register the Supervisor hooks in Bob's global settings
  aps uninstall  Remove them again (other settings are left untouched)
  aps start      Start the Supervisor service in the background
  aps stop       Stop a service started with `aps start`
  aps status     Is the service running? Are the hooks installed?
  aps doctor     Check the whole setup and explain what is wrong
  aps autostart enable|disable   Start the service automatically at login

Once the hooks are installed, Bob's tool calls are DENIED whenever the
Supervisor service is not running (fail-closed). Keep it running, enable
autostart, or run `aps uninstall`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from aps_cli import __version__, autostart, bob_hooks, paths

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _smoke_test_hook(python: Path) -> tuple[bool, str]:
    """Run the Hook Client with a SessionStart payload. It must exit 0."""
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "aps-smoke-test", "cwd": str(paths.REPO_ROOT)})
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


def _require_repo() -> bool:
    if paths.repo_is_complete():
        return True
    print(
        "The Supervisor files were not found next to the aps CLI.\n"
        "v0.1 must be installed from a clone of the repository:\n"
        "  git clone <repo-url> && cd <repo> && pip install -e .",
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
    """Values from the repository's .env file (the service loads it at startup)."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    return {k: v for k, v in dotenv_values(paths.REPO_ROOT / ".env").items() if v is not None}


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
    if not _require_repo():
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

    if args.dry_run:
        print(f"Would write {settings_path}:")
        print(json.dumps(updated.get("hooks", {}), indent=2))
        return 0

    if updated == settings:
        print(f"Hooks already installed in {settings_path}")
    else:
        backup = bob_hooks.backup_settings(settings_path, paths.state_dir() / "backups")
        bob_hooks.write_settings(settings_path, updated)
        print(f"Installed Supervisor hooks in {settings_path}")
        if backup:
            print(f"  Backup of previous settings: {backup}")

    ok, detail = _smoke_test_hook(python)
    print(f"  Hook client check: {'ok' if ok else 'FAILED - ' + detail}")
    if bob_hooks.has_probe_hooks(updated):
        print("  Note: Step 1 probe hooks are still installed. Remove them with: aps install --remove-probe")

    print(
        "\nNext steps:\n"
        "  1. Start the service:   aps start        (or at every login: aps autostart enable)\n"
        "  2. Restart Bob so it loads the new hooks.\n"
        "While the service is not running, Bob's tool calls are denied (fail-closed)."
    )
    return 0 if ok else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    settings_path = paths.bob_settings_path()
    settings = _read_settings(settings_path)
    if settings is None:
        return 1
    updated, removed = bob_hooks.remove_hooks(settings, include_probe=args.remove_probe)
    if removed == 0:
        print(f"No Supervisor hooks found in {settings_path}")
        return 0
    backup = bob_hooks.backup_settings(settings_path, paths.state_dir() / "backups")
    bob_hooks.write_settings(settings_path, updated)
    print(f"Removed {removed} hook entr{'y' if removed == 1 else 'ies'} from {settings_path}")
    if backup:
        print(f"  Backup of previous settings: {backup}")
    print("Restart Bob for the change to take effect. Disable autostart with: aps autostart disable")
    return 0


# ---------------------------------------------------------------------------
# start / stop / status
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    if not _require_repo():
        return 1
    if _health():
        print(f"Supervisor already running at {paths.supervisor_url()}")
        return 0
    host, port = paths.supervisor_host(), paths.supervisor_port()

    # The React pop-up (UI/escalation-popup-src) only works while its dev server
    # runs. Installed users get the approval page built into the service instead,
    # unless they set ESCALATION_POPUP_URL in the environment or in .env.
    if "ESCALATION_POPUP_URL" not in os.environ and "ESCALATION_POPUP_URL" not in _dotenv():
        os.environ["ESCALATION_POPUP_URL"] = ""

    if args.foreground:
        # Used by launchd / systemd, which manage the process themselves.
        os.chdir(paths.REPO_ROOT)
        sys.path.insert(0, str(paths.REPO_ROOT))
        import uvicorn
        uvicorn.run("supervisor.app:app", host=host, port=port, log_level="info")
        return 0

    paths.state_dir().mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "uvicorn", "supervisor.app:app", "--host", host, "--port", str(port)]
    kwargs: dict = {}
    if os.name == "nt":
        create_no_window, new_group = 0x08000000, 0x00000200
        kwargs["creationflags"] = create_no_window | new_group
    else:
        kwargs["start_new_session"] = True
    with open(_log_file(), "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=paths.REPO_ROOT, stdin=subprocess.DEVNULL,
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
    print("Supervisor stopped. Bob's tool calls are now denied until it is started again (or run: aps uninstall).")
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
    return 0 if info and not missing else 1


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    results: list[tuple[str, str, str]] = []

    def check(status: str, name: str, detail: str = "") -> None:
        results.append((status, name, detail))

    check(OK if sys.version_info >= (3, 10) else FAIL, "Python 3.10+", sys.version.split()[0])
    check(OK if paths.repo_is_complete() else FAIL, "Supervisor files", str(paths.REPO_ROOT))

    missing = []
    for module in ("fastapi", "uvicorn", "yaml", "pydantic", "dotenv"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    check(FAIL if missing else OK, "Runtime packages", f"missing: {', '.join(missing)} (pip install -e .)" if missing else "")

    try:
        import yaml
        policy = yaml.safe_load((paths.POLICY_DIR / "policy.yaml").read_text(encoding="utf-8")) or {}
        rails = len(policy.get("hard_rails", []))
        check(OK if rails else FAIL, "Policy file", f"version {policy.get('meta', {}).get('version_tag')}, {rails} hard rails")
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

    if paths.repo_is_complete():
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
# autostart
# ---------------------------------------------------------------------------

def cmd_autostart(args: argparse.Namespace) -> int:
    if not _require_repo():
        return 1
    plan = autostart.build_plan()
    if args.action == "enable":
        errors = autostart.enable(plan, activate=not args.no_activate)
        print(f"Autostart enabled: {plan.file}")
        for error in errors:
            print(f"  Warning: {error}", file=sys.stderr)
        if plan.system == "windows":
            print("  The service will start at your next login. Start it now with: aps start")
        return 1 if errors else 0
    existed, errors = autostart.disable(plan, activate=not args.no_activate)
    print(f"Autostart disabled: removed {plan.file}" if existed else "Autostart was not enabled.")
    for error in errors:
        print(f"  Warning: {error}", file=sys.stderr)
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aps", description="AI Permission Supervisor for IBM Bob.")
    parser.add_argument("--version", action="version", version=f"aps {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="register the Supervisor hooks in Bob's global settings")
    p.add_argument("--python", help="interpreter for the hook command (default: this one)")
    p.add_argument("--remove-probe", action="store_true", help="also remove Step 1 probe hooks")
    p.add_argument("--dry-run", action="store_true", help="show the hooks without writing anything")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove the Supervisor hooks from Bob's settings")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
