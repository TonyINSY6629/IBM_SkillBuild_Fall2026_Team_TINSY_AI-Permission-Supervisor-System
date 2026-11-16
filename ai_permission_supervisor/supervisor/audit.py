"""
supervisor/audit.py — Append-only Hash-Chained Audit Log

Stores every Supervisor decision as an immutable, tamper-evident record.

Design:
  - One JSONL file: one JSON object per line.
  - Hash chain: each entry commits to the previous entry's hash, so editing
    or deleting any entry breaks all subsequent hashes.
  - entry_hash = sha256(canonical_json({entry_id, written_at,
                                        prev_entry_hash, snapshot}))
    where canonical_json = json.dumps(sorted keys, no whitespace, ensure_ascii=False)
  - Write is synchronous and fsync'd before returning — the caller receives
    the entry_hash only after the bytes are on disk.
  - If the write fails for any reason, AuditWriteError is raised.
    The caller (Policy Engine) must treat this as a fatal error and exit 2.

Storage location:
  Default: ~/supervisor_audit/audit_log.jsonl
  Override: AUDIT_LOG_PATH environment variable.
  The directory is outside the agent workspace so the agent cannot reach it
  through normal file-write tools.

Public API:
  write_entry(decision_id, snapshot) -> AuditLogEntry
  verify_chain(log_path=None)        -> VerifyResult
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_LOG_DIR = Path.home() / "supervisor_audit"
_DEFAULT_LOG_FILE = _DEFAULT_LOG_DIR / "audit_log.jsonl"

AUDIT_LOG_PATH: Path = Path(
    os.environ.get("AUDIT_LOG_PATH", str(_DEFAULT_LOG_FILE))
)

# Sentinel for "no previous entry" (first entry in the chain).
GENESIS_HASH = "0" * 64


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuditWriteError(Exception):
    """Raised when an audit log entry cannot be written or flushed to disk."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AuditLogEntry:
    entry_id: str
    decision_id: str
    prev_entry_hash: str
    entry_hash: str
    snapshot: dict[str, Any]
    written_at: str          # ISO-8601 UTC
    synced_to_cloud: bool = False


@dataclass
class VerifyResult:
    ok: bool
    total_entries: int
    first_bad_index: Optional[int] = None   # 0-based index of first broken entry
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Canonical JSON helper
# ---------------------------------------------------------------------------

def _canonical_json(obj: dict) -> str:
    """
    Produce a deterministic JSON string: keys sorted recursively, no whitespace.
    Used to compute entry_hash so the result is identical across platforms and
    Python versions regardless of dict insertion order.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_hash(entry_id: str, written_at: str,
                  prev_entry_hash: str, snapshot: dict) -> str:
    """
    Compute entry_hash = sha256(canonical_json({entry_id, written_at,
                                                prev_entry_hash, snapshot})).
    The four fields are combined into one dict so the hash commits to all of them.
    """
    hashable = {
        "entry_id": entry_id,
        "written_at": written_at,
        "prev_entry_hash": prev_entry_hash,
        "snapshot": snapshot,
    }
    canonical = _canonical_json(hashable)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AuditLog class
# ---------------------------------------------------------------------------

class AuditLog:
    """
    Thread-safe, append-only, hash-chained audit log.

    One instance should be created per Supervisor service process and shared
    across all request handlers (it is safe to call write_entry concurrently).

    Usage:
        log = AuditLog()
        entry = log.write_entry(decision_id="d-001", snapshot={...})
        # entry.entry_hash is now on disk — safe to return verdict to caller.
    """

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._path: Path = Path(log_path) if log_path else AUDIT_LOG_PATH
        self._lock = threading.Lock()
        self._last_hash: str = GENESIS_HASH
        self._initialized: bool = False

    def _ensure_initialized(self) -> None:
        """
        Lazy initialization: create the log directory and read the last
        entry_hash from the existing file (if any) so the chain continues
        correctly across process restarts.

        Called inside the lock on the first write.
        """
        if self._initialized:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            # Walk to the last line to recover the previous hash.
            last_hash = GENESIS_HASH
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            record = json.loads(line)
                            last_hash = record.get("entry_hash", GENESIS_HASH)
            except (json.JSONDecodeError, OSError):
                # If we can't read the file we still proceed — the chain
                # will be broken at this point, but we never suppress writes.
                pass
            self._last_hash = last_hash

        self._initialized = True

    def write_entry(
        self,
        decision_id: str,
        snapshot: dict[str, Any],
    ) -> AuditLogEntry:
        """
        Append one entry to the audit log synchronously.

        The call blocks until the entry is flushed and fsync'd to disk.
        Returns the completed AuditLogEntry including its entry_hash.

        Raises AuditWriteError on any I/O failure — callers must treat
        this as fatal and deny the tool call (exit 2).
        """
        with self._lock:
            try:
                self._ensure_initialized()

                entry_id = str(uuid.uuid4())
                written_at = datetime.now(timezone.utc).isoformat()
                prev_hash = self._last_hash

                entry_hash = _compute_hash(entry_id, written_at, prev_hash, snapshot)

                entry = AuditLogEntry(
                    entry_id=entry_id,
                    decision_id=decision_id,
                    prev_entry_hash=prev_hash,
                    entry_hash=entry_hash,
                    snapshot=snapshot,
                    written_at=written_at,
                    synced_to_cloud=False,
                )

                record = {
                    "entry_id": entry.entry_id,
                    "decision_id": entry.decision_id,
                    "prev_entry_hash": entry.prev_entry_hash,
                    "entry_hash": entry.entry_hash,
                    "snapshot": entry.snapshot,
                    "written_at": entry.written_at,
                    "synced_to_cloud": entry.synced_to_cloud,
                }

                line = json.dumps(record, ensure_ascii=False) + "\n"

                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())

                # Only update in-memory state after confirmed disk write.
                self._last_hash = entry_hash
                return entry

            except AuditWriteError:
                raise
            except Exception as exc:
                raise AuditWriteError(
                    f"Failed to write audit log entry: {exc}"
                ) from exc

    def verify_chain(self) -> VerifyResult:
        """
        Read the entire log file and verify the hash chain.

        Returns a VerifyResult indicating whether the chain is intact.
        Detects:
          - Any entry where entry_hash does not match the recomputed hash
            (content was modified)
          - Any entry where prev_entry_hash does not match the previous
            entry's entry_hash (an entry was deleted or inserted)
        """
        if not self._path.exists():
            return VerifyResult(ok=True, total_entries=0)

        entries: list[dict] = []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError) as exc:
            return VerifyResult(
                ok=False,
                total_entries=len(entries),
                first_bad_index=len(entries),
                error=f"Could not read log file: {exc}",
            )

        expected_prev = GENESIS_HASH

        for i, record in enumerate(entries):
            # 1. Check the chain link — prev_entry_hash must match the
            #    previous entry's entry_hash (or GENESIS_HASH for first entry).
            if record.get("prev_entry_hash") != expected_prev:
                return VerifyResult(
                    ok=False,
                    total_entries=len(entries),
                    first_bad_index=i,
                    error=(
                        f"Entry {i} (id={record.get('entry_id')}): "
                        f"prev_entry_hash mismatch — "
                        f"expected {expected_prev!r}, "
                        f"got {record.get('prev_entry_hash')!r}. "
                        f"An entry may have been deleted or inserted."
                    ),
                )

            # 2. Recompute entry_hash from the four committed fields and compare.
            recomputed = _compute_hash(
                entry_id=record["entry_id"],
                written_at=record["written_at"],
                prev_entry_hash=record["prev_entry_hash"],
                snapshot=record["snapshot"],
            )
            if recomputed != record.get("entry_hash"):
                return VerifyResult(
                    ok=False,
                    total_entries=len(entries),
                    first_bad_index=i,
                    error=(
                        f"Entry {i} (id={record.get('entry_id')}): "
                        f"entry_hash mismatch — content was modified."
                    ),
                )

            expected_prev = record["entry_hash"]

        return VerifyResult(ok=True, total_entries=len(entries))


# ---------------------------------------------------------------------------
# Module-level singleton (used by supervisor/app.py and policy_engine.py)
# ---------------------------------------------------------------------------

audit_log = AuditLog()
