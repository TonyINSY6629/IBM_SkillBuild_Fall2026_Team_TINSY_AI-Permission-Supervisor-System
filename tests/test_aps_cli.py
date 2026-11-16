"""
tests/test_aps_cli.py — the `aps` install / uninstall / autostart CLI.

The most important property: installing never destroys the user's other Bob
settings, is idempotent, and refuses to touch a settings file it cannot parse.

All tests work in tmp_path via BOB_SETTINGS_PATH and APS_STATE_DIR.

Run with:
    python -m pytest tests/test_aps_cli.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_permission_supervisor.cli import autostart, bob_hooks, paths
from ai_permission_supervisor.cli.main import main

CMD = bob_hooks.hook_command(sys.executable, paths.HOOK_SCRIPT)


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = tmp_path / "bob" / "settings.json"
    state = tmp_path / "state"
    monkeypatch.setenv("BOB_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("APS_STATE_DIR", str(state))
    monkeypatch.setenv("SUPERVISOR_URL", "http://127.0.0.1:9")  # nothing listens here
    return settings, state


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _supervisor_entries(settings: dict) -> int:
    return sum(
        1
        for groups in settings.get("hooks", {}).values()
        for group in groups
        for h in group.get("hooks", [])
        if bob_hooks.is_supervisor_command(h.get("command", ""))
    )


# ---------------------------------------------------------------------------
# bob_hooks (pure functions)
# ---------------------------------------------------------------------------

class TestMerge:
    def test_adds_all_four_events_with_timeouts(self):
        result = bob_hooks.add_supervisor_hooks({}, CMD)
        assert bob_hooks.installed_timeouts(result) == {
            "PreToolUse": 150, "UserPromptSubmit": 10, "PostToolUse": 10, "Stop": 10,
        }
        assert result["hooks"]["PreToolUse"][0]["matcher"] == ".*"
        assert "matcher" not in result["hooks"]["Stop"][0]

    def test_is_idempotent(self):
        once = bob_hooks.add_supervisor_hooks({}, CMD)
        twice = bob_hooks.add_supervisor_hooks(once, CMD)
        assert once == twice
        assert _supervisor_entries(twice) == 4

    def test_keeps_other_settings_and_hooks(self):
        original = {
            "approval": {"allowed_permissions": ["read"]},
            "hooks": {"PreToolUse": [{"matcher": "^write_file$", "hooks": [
                {"type": "command", "command": "sh ~/.bob/hooks/check.sh"}]}]},
        }
        result = bob_hooks.add_supervisor_hooks(original, CMD)
        assert result["approval"] == original["approval"]
        assert result["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "sh ~/.bob/hooks/check.sh"
        assert original["hooks"]["PreToolUse"][0]["matcher"] == "^write_file$"  # input not mutated

    def test_replaces_an_old_supervisor_entry_with_different_path(self):
        old = {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [
            {"type": "command", "command": 'py -3 "C:/old/place/hook_client/main.py"', "timeout": 150}]}]}}
        result = bob_hooks.add_supervisor_hooks(old, CMD)
        commands = [h["command"] for g in result["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert commands == [CMD]

    def test_windows_backslash_paths_are_recognised(self):
        assert bob_hooks.is_supervisor_command(r'"C:\Py\python.exe" "C:\Repo\hook_client\main.py"')
        assert not bob_hooks.is_supervisor_command("sh ~/.bob/hooks/check.sh")

    def test_remove_leaves_other_hooks(self):
        settings = bob_hooks.add_supervisor_hooks({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "echo bye"}]}]}}, CMD)
        result, removed = bob_hooks.remove_hooks(settings)
        assert removed == 4
        assert result == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}]}}

    def test_remove_probe(self):
        settings = {"hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "python ~/supervisor_probe/probe.py"}]}]}}
        assert bob_hooks.has_probe_hooks(settings)
        result, removed = bob_hooks.remove_probe_hooks(settings)
        assert removed == 1 and result == {}

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "settings.json"
        bad.write_text("{ not json", encoding="utf-8")
        with pytest.raises(bob_hooks.SettingsError):
            bob_hooks.load_settings(bad)

    def test_utf8_bom_is_accepted(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode())
        assert bob_hooks.load_settings(f) == {"a": 1}


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

class TestInstallCommand:
    def test_install_creates_file_and_backup(self, env):
        settings_path, state = env
        _write(settings_path, {"theme": "dark"})
        assert main(["install"]) == 0
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["theme"] == "dark"
        assert _supervisor_entries(data) == 4
        backups = list((state / "backups").glob("settings.*.json"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == {"theme": "dark"}

    def test_install_without_existing_file(self, env):
        settings_path, _ = env
        assert main(["install"]) == 0
        assert _supervisor_entries(json.loads(settings_path.read_text(encoding="utf-8"))) == 4

    def test_install_twice_is_stable(self, env, capsys):
        settings_path, _ = env
        main(["install"])
        first = settings_path.read_text(encoding="utf-8")
        assert main(["install"]) == 0
        assert settings_path.read_text(encoding="utf-8") == first
        assert "Already installed" in capsys.readouterr().out

    def test_install_refuses_invalid_json(self, env):
        settings_path, _ = env
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text("{ broken", encoding="utf-8")
        assert main(["install"]) == 1
        assert settings_path.read_text(encoding="utf-8") == "{ broken"

    def test_dry_run_writes_nothing(self, env):
        settings_path, _ = env
        assert main(["install", "--dry-run"]) == 0
        assert not settings_path.exists()

    def test_uninstall_round_trip(self, env):
        settings_path, _ = env
        _write(settings_path, {"theme": "dark"})
        main(["install"])
        assert main(["uninstall"]) == 0
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {"theme": "dark"}

    def test_status_reports_service_down(self, env, capsys):
        main(["install"])
        assert main(["status"]) == 1
        assert "NOT running" in capsys.readouterr().out


class TestDoctor:
    def test_doctor_flags_bad_timeout_ordering(self, env, capsys, monkeypatch):
        settings_path, _ = env
        settings = bob_hooks.add_supervisor_hooks({}, CMD)
        settings["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 10  # Bob default: fails open
        _write(settings_path, settings)
        monkeypatch.setenv("ESCALATION_TIMEOUT_SECONDS", "120")
        monkeypatch.setenv("HOOK_DEADLINE_SECONDS", "130")
        assert main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "[FAIL] Timeout ordering" in out
        assert "[FAIL] Supervisor service" in out

    def test_doctor_accepts_correct_ordering(self, env, capsys, monkeypatch):
        main(["install"])
        monkeypatch.setenv("ESCALATION_TIMEOUT_SECONDS", "120")
        monkeypatch.setenv("HOOK_DEADLINE_SECONDS", "130")
        main(["doctor"])
        out = capsys.readouterr().out
        assert "[OK  ] Timeout ordering" in out
        assert "[OK  ] Bob hooks" in out


# ---------------------------------------------------------------------------
# Autostart files (written to tmp, never activated)
# ---------------------------------------------------------------------------

class TestAutostart:
    @pytest.mark.parametrize("system", ["windows", "macos", "linux"])
    def test_enable_and_disable(self, env, tmp_path, monkeypatch, system):
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        plan = autostart.build_plan(python="/opt/py/bin/python", system=system, home=tmp_path / "home")
        assert str(tmp_path) in str(plan.file)
        autostart.enable(plan, activate=False)
        assert plan.file.exists()
        text = plan.file.read_text(encoding="utf-8")
        assert "ai_permission_supervisor.cli" in text
        existed, errors = autostart.disable(plan, activate=False)
        assert existed and not errors and not plan.file.exists()

    def test_systemd_unit_runs_in_foreground(self, tmp_path):
        plan = autostart.build_plan(python="/opt/py/bin/python", system="linux", home=tmp_path)
        assert 'ExecStart="/opt/py/bin/python" -m ai_permission_supervisor.cli start --foreground' in plan.content

    def test_windows_file_uses_crlf(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPDATA", str(tmp_path))
        plan = autostart.build_plan(python=tmp_path / "python.exe", system="windows", home=tmp_path)
        autostart.enable(plan, activate=False)
        assert b"\r\n" in plan.file.read_bytes()


# ---------------------------------------------------------------------------
# Bob auto-approve: added by install --bob-auto-approve, reverted by uninstall
# ---------------------------------------------------------------------------

class TestBobAutoApprove:
    def test_enable_and_revert_round_trip(self):
        original = {"approval": {"forbiddenApprovalGroups": [], "allowed_permissions": ["mode"]}}
        enabled, record = bob_hooks.enable_auto_approve(original)
        groups, commands = bob_hooks.auto_approve_status(enabled)
        assert {"read", "edit", "execute", "mode"} <= groups
        # Bob matches by prefix and ignores "*": real prefixes must be listed.
        assert {"git", "python", "npm"} <= commands and "*" not in commands
        assert not {"del", "rm", "rmdir", "format"} & commands  # destructive ones stay on Bob's prompt
        assert bob_hooks.revert_auto_approve(enabled, record) == original

    def test_revert_keeps_what_the_user_already_had(self):
        original = {"approval": {"allowed_permissions": ["execute"], "allowedExecutors": [
            {"toolId": "execute_command", "approvedCommands": ["git status"]}]}}
        enabled, record = bob_hooks.enable_auto_approve(original)
        assert record["groups"] == ["read", "edit"]
        assert bob_hooks.revert_auto_approve(enabled, record) == original

    def test_revert_from_empty_settings_leaves_nothing(self):
        enabled, record = bob_hooks.enable_auto_approve({})
        assert bob_hooks.revert_auto_approve(enabled, record) == {}

    def test_install_flag_then_uninstall_restores_settings(self, env):
        settings_path, state = env
        _write(settings_path, {"theme": "dark", "approval": {"forbiddenApprovalGroups": []}})
        assert main(["install", "--bob-auto-approve"]) == 0
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "execute" in data["approval"]["allowed_permissions"]
        assert (state / "bob_auto_approve.json").exists()
        assert main(["uninstall"]) == 0
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "theme": "dark", "approval": {"forbiddenApprovalGroups": []}}
        assert not (state / "bob_auto_approve.json").exists()

    def test_forbidden_group_is_refused(self, env):
        settings_path, _ = env
        _write(settings_path, {"approval": {"forbiddenApprovalGroups": ["execute"]}})
        assert main(["install", "--bob-auto-approve"]) == 1
        assert "hooks" not in json.loads(settings_path.read_text(encoding="utf-8"))

    def test_uninstall_warns_when_user_kept_auto_approve(self, env, capsys):
        settings_path, _ = env
        settings = bob_hooks.add_supervisor_hooks({"approval": {"allowed_permissions": ["execute"]}}, CMD)
        _write(settings_path, settings)
        main(["uninstall"])
        assert "run\nunchecked" in capsys.readouterr().out

    def test_doctor_fails_when_auto_approve_without_hooks(self, env, capsys):
        settings_path, _ = env
        _write(settings_path, {"approval": {"allowed_permissions": ["execute"]}})
        assert main(["doctor"]) == 1
        assert "[FAIL] Bob auto-approve" in capsys.readouterr().out


class TestPolicyCommand:
    def test_init_copies_default_policy_and_service_uses_it(self, env, capsys):
        _, state = env
        assert main(["policy", "init"]) == 0
        assert (state / "policy" / "policy.yaml").is_file()
        assert (state / "policy" / "taxonomy.yaml").is_file()
        assert paths.policy_dir() == state / "policy"
        assert main(["policy", "init"]) == 1  # does not overwrite without --force
        assert main(["policy", "init", "--force"]) == 0

    def test_default_is_packaged_policy(self, env):
        assert paths.policy_dir() == paths.PACKAGED_POLICY_DIR


class TestAutoApprovePrefixes:
    def test_record_lists_added_prefixes_and_keeps_existing(self):
        original = {"approval": {"allowedExecutors": [{"toolId": "execute_command", "approvedCommands": ["git", "make"]}]}}
        enabled, record = bob_hooks.enable_auto_approve(original)
        assert "git" not in record["added_commands"] and "npm" in record["added_commands"]
        reverted = bob_hooks.revert_auto_approve(enabled, record)
        assert reverted["approval"]["allowedExecutors"][0]["approvedCommands"] == ["git", "make"]

    def test_legacy_wildcard_record_is_still_reverted(self):
        settings = {"approval": {"allowed_permissions": ["read", "edit", "execute"],
                                 "allowedExecutors": [{"toolId": "execute_command", "approvedCommands": ["*"]}]}}
        legacy = {"groups": ["read", "edit", "execute"], "added_star": True, "created_executor": True,
                  "created_keys": ["allowed_permissions", "allowedExecutors"], "created_approval": True}
        assert bob_hooks.revert_auto_approve(settings, legacy) == {}

    def test_doctor_warns_when_only_wildcard_is_listed(self, env, capsys):
        settings_path, _ = env
        settings = bob_hooks.add_supervisor_hooks({"approval": {
            "allowed_permissions": ["read", "edit", "execute"],
            "allowedExecutors": [{"toolId": "execute_command", "approvedCommands": ["*"]}]}}, CMD)
        _write(settings_path, settings)
        main(["doctor"])
        assert "[WARN] Bob auto-approve" in capsys.readouterr().out

    def test_install_adds_prefixes_to_a_wildcard_only_setup(self, env):
        settings_path, _ = env
        _write(settings_path, {"approval": {"allowed_permissions": ["read", "edit", "execute"],
                                            "allowedExecutors": [{"toolId": "execute_command", "approvedCommands": ["*"]}]}})
        assert main(["install", "--bob-auto-approve"]) == 0
        commands = json.loads(settings_path.read_text(encoding="utf-8"))["approval"]["allowedExecutors"][0]["approvedCommands"]
        assert commands[0] == "*" and "git" in commands


class TestHints:
    def test_hint_uses_python_m_when_aps_is_not_on_path(self, monkeypatch):
        from ai_permission_supervisor.cli import main as cli_main
        monkeypatch.setattr(cli_main.shutil, "which", lambda name: None)
        assert cli_main._aps().endswith("-m ai_permission_supervisor.cli")
        monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/bin/aps")
        assert cli_main._aps() == "aps"
