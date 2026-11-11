"""
tests/test_context_store.py

Tests for supervisor/context_store.py.

Done-condition (from plan Step 3.7):
  Submit a prompt, trigger tool call, verify PermissionRequest.prompt_id
  is populated.

Also tests:
  - record_prompt() stores and returns a PromptContext
  - get_prompt() returns None for unknown session
  - Overwrite: second record_prompt for same session replaces the first
  - clear_session() removes the entry
  - LRU eviction: oldest session evicted when MAX_SESSIONS exceeded
  - Thread safety: concurrent writes don't corrupt the store
  - End-to-end through normalize(): prompt_id and prompt_text attached

Run with:
    python -m pytest tests/test_context_store.py -v
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from supervisor.context_store import ContextStore, PromptContext

PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())


# ---------------------------------------------------------------------------
# Basic record and retrieve
# ---------------------------------------------------------------------------

class TestBasic:
    def test_record_prompt_returns_context(self):
        store = ContextStore()
        ctx = store.record_prompt("s-001", "Do something useful")
        assert ctx.session_id == "s-001"
        assert ctx.prompt_text == "Do something useful"
        assert ctx.prompt_id  # non-empty UUID
        assert ctx.received_at

    def test_get_prompt_returns_stored(self):
        store = ContextStore()
        store.record_prompt("s-001", "Hello world")
        ctx = store.get_prompt("s-001")
        assert ctx is not None
        assert ctx.prompt_text == "Hello world"

    def test_get_prompt_unknown_session_returns_none(self):
        store = ContextStore()
        assert store.get_prompt("no-such-session") is None

    def test_custom_prompt_id_respected(self):
        store = ContextStore()
        ctx = store.record_prompt("s-001", "text", prompt_id="custom-id-123")
        assert ctx.prompt_id == "custom-id-123"
        assert store.get_prompt("s-001").prompt_id == "custom-id-123"

    def test_overwrite_replaces_previous(self):
        store = ContextStore()
        store.record_prompt("s-001", "First prompt")
        store.record_prompt("s-001", "Second prompt")
        ctx = store.get_prompt("s-001")
        assert ctx.prompt_text == "Second prompt"

    def test_len_reflects_sessions(self):
        store = ContextStore()
        store.record_prompt("s-001", "a")
        store.record_prompt("s-002", "b")
        assert len(store) == 2


# ---------------------------------------------------------------------------
# clear_session
# ---------------------------------------------------------------------------

class TestClearSession:
    def test_clear_session_removes_entry(self):
        store = ContextStore()
        store.record_prompt("s-001", "text")
        store.clear_session("s-001")
        assert store.get_prompt("s-001") is None

    def test_clear_unknown_session_is_safe(self):
        store = ContextStore()
        store.clear_session("never-existed")  # must not raise


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------

class TestEviction:
    def test_oldest_session_evicted_when_full(self):
        store = ContextStore(max_sessions=3)
        store.record_prompt("s-001", "a")
        store.record_prompt("s-002", "b")
        store.record_prompt("s-003", "c")
        assert len(store) == 3

        # Adding a 4th session evicts the oldest (s-001)
        store.record_prompt("s-004", "d")
        assert len(store) == 3
        assert store.get_prompt("s-001") is None
        assert store.get_prompt("s-004") is not None

    def test_accessed_session_not_evicted_first(self):
        """Re-writing a session moves it to most-recent; it should not be evicted next."""
        store = ContextStore(max_sessions=3)
        store.record_prompt("s-001", "a")
        store.record_prompt("s-002", "b")
        # Re-write s-001 → moves to most-recent end
        store.record_prompt("s-001", "a-updated")
        store.record_prompt("s-003", "c")
        # Now full. Adding s-004 should evict s-002 (oldest), NOT s-001.
        store.record_prompt("s-004", "d")
        assert store.get_prompt("s-001") is not None   # still alive
        assert store.get_prompt("s-002") is None        # evicted


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_writes_do_not_corrupt(self):
        store = ContextStore(max_sessions=512)
        errors = []

        def _write(session_id: str, n: int):
            try:
                for i in range(n):
                    store.record_prompt(session_id, f"prompt {i}")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_write, args=(f"s-{j:04d}", 20))
            for j in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        # Each session should have its last prompt stored
        for j in range(50):
            ctx = store.get_prompt(f"s-{j:04d}")
            assert ctx is not None


# ---------------------------------------------------------------------------
# Done-condition: end-to-end through normalize()
# ---------------------------------------------------------------------------

class TestDoneCondition:
    def test_prompt_id_attached_to_permission_request(self):
        """
        Done-condition: submit prompt, trigger tool call, verify
        PermissionRequest.prompt_id and prompt_text are populated.
        """
        from supervisor.context_store import ContextStore
        from supervisor.normalizer import normalize

        store = ContextStore()

        # Step 1: UserPromptSubmit — record the prompt
        session_id = "session-done-condition"
        ctx = store.record_prompt(
            session_id=session_id,
            prompt_text="Please list the files in the project.",
        )

        # Step 2: PreToolUse — look up prompt and pass to normalize()
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_use_id": "u-dc-001",
            "session_id": session_id,
            "tool_name": "list_files",
            "tool_input": {"path": PROJECT_ROOT},
            "cwd": PROJECT_ROOT,
        }
        prompt_ctx = store.get_prompt(session_id)
        req = normalize(
            payload,
            workspace_roots=[PROJECT_ROOT],
            prompt_id=prompt_ctx.prompt_id if prompt_ctx else None,
            prompt_text=prompt_ctx.prompt_text if prompt_ctx else None,
        )

        # Done-condition assertion
        assert req.prompt_id == ctx.prompt_id, (
            "PermissionRequest.prompt_id must match the recorded prompt id."
        )
        assert req.prompt_text == "Please list the files in the project."

    def test_no_prompt_yields_none_fields(self):
        """
        If no UserPromptSubmit was recorded for the session,
        PermissionRequest.prompt_id must be None (not crash).
        """
        from supervisor.normalizer import normalize

        payload = {
            "hook_event_name": "PreToolUse",
            "tool_use_id": "u-dc-002",
            "session_id": "session-no-prompt",
            "tool_name": "read_file",
            "tool_input": {"path": PROJECT_ROOT + "/foo.py", "context_focus_query": "x"},
            "cwd": PROJECT_ROOT,
        }
        req = normalize(payload, workspace_roots=[PROJECT_ROOT])
        assert req.prompt_id is None
        assert req.prompt_text is None

    def test_second_prompt_replaces_first_in_request(self):
        """
        The most recent prompt for a session is what gets attached —
        not the first one.
        """
        from supervisor.context_store import ContextStore
        from supervisor.normalizer import normalize

        store = ContextStore()
        session_id = "session-second-prompt"
        store.record_prompt(session_id, "First task")
        ctx2 = store.record_prompt(session_id, "Second task")

        prompt_ctx = store.get_prompt(session_id)
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_use_id": "u-dc-003",
            "session_id": session_id,
            "tool_name": "list_files",
            "tool_input": {"path": PROJECT_ROOT},
            "cwd": PROJECT_ROOT,
        }
        req = normalize(
            payload,
            workspace_roots=[PROJECT_ROOT],
            prompt_id=prompt_ctx.prompt_id if prompt_ctx else None,
            prompt_text=prompt_ctx.prompt_text if prompt_ctx else None,
        )
        assert req.prompt_text == "Second task"
        assert req.prompt_id == ctx2.prompt_id
