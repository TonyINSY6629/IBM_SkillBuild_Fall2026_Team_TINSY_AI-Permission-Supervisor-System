"""
supervisor/context_store.py — Session Prompt Context Store

Captures UserPromptSubmit payloads and makes them available to the
Normalizer so each PermissionRequest carries the prompt that initiated
the current task.

Design:
  - Keyed by session_id → most-recent (prompt_id, prompt_text)
  - In-memory only. Intentionally not persisted — prompts are only useful
    during the session they belong to. The audit log snapshot carries
    prompt_id for durable record-keeping.
  - Thread-safe: write from the UserPromptSubmit hook handler; read from
    the PreToolUse /decide handler.
  - Memory bounded: old sessions are evicted after MAX_SESSIONS entries
    (LRU-style eviction keeps recent sessions, drops oldest).

Bob hook integration:
  The /hook endpoint (or the existing /decide handler checking hook_event_name)
  receives UserPromptSubmit payloads and calls ContextStore.record_prompt().
  The /decide handler calls ContextStore.get_prompt(session_id) before
  normalizing, then passes prompt_id + prompt_text to normalize().

Public API:
  ContextStore()
  ContextStore.record_prompt(session_id, prompt_id, prompt_text) -> None
  ContextStore.get_prompt(session_id) -> PromptContext | None

  PromptContext.prompt_id   — str
  PromptContext.prompt_text — str
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Maximum number of sessions held in memory at once.
# Oldest sessions are evicted when the limit is reached.
MAX_SESSIONS: int = 256


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PromptContext:
    prompt_id: str
    prompt_text: str
    session_id: str
    received_at: str   # ISO-8601 UTC


# ---------------------------------------------------------------------------
# ContextStore
# ---------------------------------------------------------------------------

class ContextStore:
    """
    Thread-safe, in-memory store mapping session_id → most-recent PromptContext.
    """

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._lock = threading.Lock()
        # OrderedDict used as an LRU cache: oldest entry at the front.
        self._store: OrderedDict[str, PromptContext] = OrderedDict()
        self._max = max_sessions

    def record_prompt(
        self,
        session_id: str,
        prompt_text: str,
        prompt_id: Optional[str] = None,
    ) -> PromptContext:
        """
        Store the latest prompt for a session.

        If prompt_id is not provided, a UUID is generated.
        Overwrites any previous prompt for this session (we only need the
        most recent — the model uses it as task context for the current
        tool call).

        Returns the created PromptContext.
        """
        ctx = PromptContext(
            prompt_id=prompt_id or str(uuid.uuid4()),
            prompt_text=prompt_text,
            session_id=session_id,
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            # Move to end (most recently used) if already present,
            # or insert at end. Then evict oldest if over limit.
            self._store.pop(session_id, None)
            self._store[session_id] = ctx
            while len(self._store) > self._max:
                self._store.popitem(last=False)  # evict oldest
        return ctx

    def get_prompt(self, session_id: str) -> Optional[PromptContext]:
        """
        Return the most recent PromptContext for this session, or None
        if no prompt has been recorded yet.
        """
        with self._lock:
            return self._store.get(session_id)

    def clear_session(self, session_id: str) -> None:
        """Remove the prompt context for a session (called on Stop event)."""
        with self._lock:
            self._store.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

context_store = ContextStore()
