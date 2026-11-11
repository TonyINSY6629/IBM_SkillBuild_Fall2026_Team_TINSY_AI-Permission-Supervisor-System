"""
tests/test_normalizer.py

Tests for supervisor/normalizer.py.

Done-condition (from plan Step 3.4):
  Replay every PreToolUse payload from probe_log.jsonl → zero conversion errors.

Also tests:
  - ActionType routing per tool
  - Target extraction per tool / input shape
  - inside_workspace classification
  - touches_secrets detection
  - Category assignment (supervisor_path_access, network_request, etc.)
  - NormalizerError on missing required fields

Run with:
    python -m pytest tests/test_normalizer.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from schemas.permission_request import ActionType
from supervisor.normalizer import NormalizerError, normalize

# ---------------------------------------------------------------------------
# Shared workspace root for all tests (project directory)
# ---------------------------------------------------------------------------

PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())
WORKSPACE = [PROJECT_ROOT]


def _payload(tool_name: str, tool_input: dict, cwd: str = PROJECT_ROOT,
             tool_use_id: str = "u-001", session_id: str = "s-001") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_use_id": tool_use_id,
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": cwd,
    }


# ---------------------------------------------------------------------------
# ActionType routing
# ---------------------------------------------------------------------------

class TestActionType:
    @pytest.mark.parametrize("tool", [
        "read_file", "list_files", "glob", "grep",
        "GetSymbolsOverview", "FindSymbol", "FindReferencingSymbols",
        "office_read",
    ])
    def test_read_tools_get_read_action(self, tool):
        req = normalize(_payload(tool, {"path": PROJECT_ROOT + "/README.md"}), WORKSPACE)
        assert req.action_type == ActionType.READ

    @pytest.mark.parametrize("tool", [
        "write_file", "apply_diff", "search_and_replace", "insert_content",
    ])
    def test_write_tools_get_write_action(self, tool):
        req = normalize(_payload(tool, {"path": PROJECT_ROOT + "/out.txt"}), WORKSPACE)
        assert req.action_type == ActionType.WRITE

    def test_execute_command_gets_exec_action(self):
        req = normalize(_payload("execute_command", {"command": "echo hi"}), WORKSPACE)
        assert req.action_type == ActionType.EXEC

    def test_unknown_tool_gets_other_action(self):
        req = normalize(_payload("update_todo_list", {"todos": "- [ ] task"}), WORKSPACE)
        assert req.action_type == ActionType.OTHER


# ---------------------------------------------------------------------------
# Target extraction — all shapes seen in probe_log.jsonl
# ---------------------------------------------------------------------------

class TestTargetExtraction:
    def test_read_file_extracts_path(self):
        p = PROJECT_ROOT + "/foo.py"
        req = normalize(_payload("read_file", {"path": p, "context_focus_query": "x"}), WORKSPACE)
        assert req.targets == [p]

    def test_read_file_with_range_extracts_path(self):
        p = PROJECT_ROOT + "/foo.py"
        req = normalize(_payload("read_file", {"path": p, "context_focus_query": "x", "range": "1-10"}), WORKSPACE)
        assert req.targets == [p]

    def test_list_files_extracts_path(self):
        req = normalize(_payload("list_files", {"path": PROJECT_ROOT}), WORKSPACE)
        assert req.targets == [PROJECT_ROOT]

    def test_list_files_recursive_extracts_path(self):
        req = normalize(_payload("list_files", {"path": PROJECT_ROOT, "recursive": True}), WORKSPACE)
        assert req.targets == [PROJECT_ROOT]

    def test_write_file_extracts_path(self):
        p = PROJECT_ROOT + "/new.txt"
        req = normalize(_payload("write_file", {"path": p, "content": "x", "line_count": 1}), WORKSPACE)
        assert req.targets == [p]

    def test_apply_diff_extracts_path(self):
        p = PROJECT_ROOT + "/file.py"
        req = normalize(_payload("apply_diff", {"path": p, "diff": "<<<"}), WORKSPACE)
        assert req.targets == [p]

    def test_execute_command_extracts_command(self):
        cmd = "python --version"
        req = normalize(_payload("execute_command", {"command": cmd}), WORKSPACE)
        assert req.targets == [cmd]

    def test_execute_command_with_timeout_extracts_command(self):
        cmd = "npm test"
        req = normalize(_payload("execute_command", {"command": cmd, "timeout_seconds": 60}), WORKSPACE)
        assert req.targets == [cmd]

    def test_execute_command_with_background_extracts_command(self):
        cmd = "python supervisor/app.py"
        req = normalize(_payload("execute_command", {"command": cmd, "background": True}), WORKSPACE)
        assert req.targets == [cmd]

    def test_update_todo_list_has_no_targets(self):
        req = normalize(_payload("update_todo_list", {"todos": "- [ ] x"}), WORKSPACE)
        assert req.targets == []


# ---------------------------------------------------------------------------
# inside_workspace classification
# ---------------------------------------------------------------------------

class TestInsideWorkspace:
    def test_workspace_file_is_inside(self):
        p = PROJECT_ROOT + "/supervisor/app.py"
        req = normalize(_payload("read_file", {"path": p, "context_focus_query": "x"}), WORKSPACE)
        assert req.inside_workspace is True

    def test_outside_path_is_outside(self):
        req = normalize(_payload("write_file", {
            "path": "C:/Windows/system32/evil.dll",
            "content": "x", "line_count": 1,
        }), WORKSPACE)
        assert req.inside_workspace is False

    def test_empty_targets_is_inside(self):
        req = normalize(_payload("update_todo_list", {"todos": ""}), WORKSPACE)
        assert req.inside_workspace is True

    def test_no_workspace_roots_is_outside(self):
        p = PROJECT_ROOT + "/foo.txt"
        req = normalize(_payload("read_file", {"path": p, "context_focus_query": "x"}), [])
        assert req.inside_workspace is False

    def test_execute_command_is_outside_workspace(self):
        # Commands have no filesystem path to check → always outside
        req = normalize(_payload("execute_command", {"command": "echo hi"}), WORKSPACE)
        assert req.inside_workspace is False


# ---------------------------------------------------------------------------
# touches_secrets detection
# ---------------------------------------------------------------------------

class TestTouchesSecrets:
    @pytest.mark.parametrize("path_suffix", [
        "/.env", "/.env.local", "/server.pem", "/id_rsa",
        "/certs/api.key", "/.aws/credentials",
    ])
    def test_secret_paths_detected(self, path_suffix):
        req = normalize(_payload("read_file", {
            "path": PROJECT_ROOT + path_suffix,
            "context_focus_query": "x",
        }), WORKSPACE)
        assert req.touches_secrets is True

    def test_normal_python_file_not_secret(self):
        req = normalize(_payload("read_file", {
            "path": PROJECT_ROOT + "/supervisor/app.py",
            "context_focus_query": "x",
        }), WORKSPACE)
        assert req.touches_secrets is False

    def test_env_in_command_detected(self):
        req = normalize(_payload("execute_command", {
            "command": "echo $env:OPENAI_API_KEY",
        }), WORKSPACE)
        assert req.touches_secrets is True


# ---------------------------------------------------------------------------
# Category assignment
# ---------------------------------------------------------------------------

class TestCategory:
    def test_workspace_read_is_file_read(self):
        req = normalize(_payload("read_file", {
            "path": PROJECT_ROOT + "/foo.py",
            "context_focus_query": "x",
        }), WORKSPACE)
        assert req.category == "file_read"

    def test_workspace_write_is_file_write(self):
        req = normalize(_payload("write_file", {
            "path": PROJECT_ROOT + "/out.py",
            "content": "x", "line_count": 1,
        }), WORKSPACE)
        assert req.category == "file_write"

    def test_outside_write_is_workspace_outside_write(self):
        req = normalize(_payload("write_file", {
            "path": "/etc/passwd",
            "content": "x", "line_count": 1,
        }), WORKSPACE)
        assert req.category == "workspace_outside_write"

    def test_network_command_is_network_request(self):
        req = normalize(_payload("execute_command", {"command": "curl https://example.com"}), WORKSPACE)
        assert req.category == "network_request"

    def test_pip_install_is_network_request(self):
        req = normalize(_payload("execute_command", {"command": "pip install requests"}), WORKSPACE)
        assert req.category == "network_request"

    def test_plain_exec_is_shell_exec(self):
        req = normalize(_payload("execute_command", {"command": "python --version"}), WORKSPACE)
        assert req.category == "shell_exec"

    def test_secret_file_read_is_secret_access(self):
        req = normalize(_payload("read_file", {
            "path": PROJECT_ROOT + "/.env",
            "context_focus_query": "x",
        }), WORKSPACE)
        assert req.category == "secret_access"

    def test_supervisor_path_write_is_supervisor_path_access(self):
        req = normalize(_payload("write_file", {
            "path": str(Path.home() / "supervisor_probe" / "probe_log.jsonl"),
            "content": "x", "line_count": 1,
        }), WORKSPACE)
        assert req.category == "supervisor_path_access"

    def test_bob_settings_write_is_supervisor_path_access(self):
        req = normalize(_payload("write_file", {
            "path": str(Path.home() / ".bob" / "settings" / "settings.json"),
            "content": "{}", "line_count": 1,
        }), WORKSPACE)
        assert req.category == "supervisor_path_access"

    def test_unknown_tool_is_unclassified(self):
        req = normalize(_payload("update_todo_list", {"todos": "x"}), WORKSPACE)
        assert req.category == "unclassified"


# ---------------------------------------------------------------------------
# NormalizerError on missing required fields
# ---------------------------------------------------------------------------

class TestNormalizerError:
    def test_missing_tool_use_id_raises(self):
        p = _payload("read_file", {"path": PROJECT_ROOT + "/f.py", "context_focus_query": "x"})
        del p["tool_use_id"]
        with pytest.raises(NormalizerError, match="tool_use_id"):
            normalize(p, WORKSPACE)

    def test_missing_session_id_raises(self):
        p = _payload("read_file", {"path": PROJECT_ROOT + "/f.py", "context_focus_query": "x"})
        del p["session_id"]
        with pytest.raises(NormalizerError, match="session_id"):
            normalize(p, WORKSPACE)

    def test_missing_tool_name_raises(self):
        p = _payload("read_file", {"path": PROJECT_ROOT + "/f.py", "context_focus_query": "x"})
        del p["tool_name"]
        with pytest.raises(NormalizerError, match="tool_name"):
            normalize(p, WORKSPACE)


# ---------------------------------------------------------------------------
# Context Store fields are passed through
# ---------------------------------------------------------------------------

class TestContextPassthrough:
    def test_prompt_id_and_text_attached(self):
        req = normalize(
            _payload("read_file", {"path": PROJECT_ROOT + "/x.py", "context_focus_query": "q"}),
            WORKSPACE,
            prompt_id="p-001",
            prompt_text="Do something useful",
        )
        assert req.prompt_id == "p-001"
        assert req.prompt_text == "Do something useful"

    def test_no_prompt_defaults_to_none(self):
        req = normalize(
            _payload("read_file", {"path": PROJECT_ROOT + "/x.py", "context_focus_query": "q"}),
            WORKSPACE,
        )
        assert req.prompt_id is None
        assert req.prompt_text is None


# ---------------------------------------------------------------------------
# Done-condition: replay every PreToolUse entry from probe_log.jsonl
# ---------------------------------------------------------------------------

PROBE_LOG = Path.home() / "supervisor_probe" / "probe_log.jsonl"


@pytest.mark.skipif(
    not PROBE_LOG.exists() or (PROBE_LOG.exists() and PROBE_LOG.stat().st_size == 0),
    reason="probe_log.jsonl not found or empty — run the probe hook first (Step 1).",
)
class TestProbeLogReplay:
    def test_all_pretooluse_entries_normalize_without_error(self):
        """
        Done-condition: replay every PreToolUse payload from the Step 1 probe
        log and assert zero NormalizerError exceptions.
        """
        records = []
        with PROBE_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip malformed lines (partial writes, encoding artefacts)

        pre_tool_payloads = [
            r["payload"]
            for r in records
            if r.get("payload", {}).get("hook_event_name") == "PreToolUse"
            and "parse_error" not in r.get("payload", {})
        ]

        assert len(pre_tool_payloads) > 0, (
            "No PreToolUse entries found in probe_log.jsonl — "
            "use Bob normally first to populate the log."
        )

        errors = []
        for i, payload in enumerate(pre_tool_payloads):
            cwd = payload.get("cwd", PROJECT_ROOT)
            # Use the cwd reported by the hook as the workspace root,
            # since different sessions may have different project roots.
            try:
                req = normalize(payload, workspace_roots=[cwd, PROJECT_ROOT])
                # Basic sanity: required fields must be populated
                assert req.tool_use_id
                assert req.session_id
                assert req.tool_name
                assert req.category  # must not be empty string
            except NormalizerError as exc:
                errors.append(f"Entry {i} ({payload.get('tool_name')}): {exc}")
            except Exception as exc:
                errors.append(f"Entry {i} ({payload.get('tool_name')}): unexpected error: {exc}")

        assert errors == [], (
            f"{len(errors)} normalization error(s):\n" + "\n".join(errors)
        )
