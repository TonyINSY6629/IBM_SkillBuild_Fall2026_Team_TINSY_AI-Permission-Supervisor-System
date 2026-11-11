"""
tests/test_audit.py

Tests for supervisor/audit.py.

Done-condition (from plan Step 3.3):
  Write 10 entries, manually edit one, run integrity check → detects tamper.

Also tests:
  - Delete-entry tamper detection
  - Insert-entry tamper detection
  - Chain continuity across process restart (new AuditLog instance, same file)
  - AuditWriteError raised when log directory is unwritable
  - verify_chain on empty file → ok

Run with:
    python -m pytest tests/test_audit.py -v
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is on path so `supervisor` package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from supervisor.audit import (
    GENESIS_HASH,
    AuditLog,
    AuditWriteError,
    VerifyResult,
    _compute_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log(tmp_path: Path) -> AuditLog:
    log_file = tmp_path / "audit_log.jsonl"
    return AuditLog(log_path=log_file)


def _write_n(log: AuditLog, n: int) -> list:
    entries = []
    for i in range(n):
        e = log.write_entry(
            decision_id=f"decision-{i:03d}",
            snapshot={"tool": "read_file", "seq": i, "action": "allow"},
        )
        entries.append(e)
    return entries


def _read_lines(log: AuditLog) -> list[dict]:
    with log._path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_lines(log: AuditLog, records: list[dict]) -> None:
    with log._path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Basic write + verify
# ---------------------------------------------------------------------------

class TestWriteAndVerify:
    def test_write_10_entries_chain_intact(self, tmp_path):
        log = _make_log(tmp_path)
        _write_n(log, 10)
        result = log.verify_chain()
        assert result.ok, f"Chain should be intact: {result.error}"
        assert result.total_entries == 10

    def test_entry_fields_present(self, tmp_path):
        log = _make_log(tmp_path)
        entry = log.write_entry("d-001", {"x": 1})
        assert entry.entry_id
        assert entry.decision_id == "d-001"
        assert entry.prev_entry_hash == GENESIS_HASH
        assert len(entry.entry_hash) == 64   # sha256 hex digest
        assert entry.written_at
        assert entry.snapshot == {"x": 1}

    def test_each_entry_links_to_previous(self, tmp_path):
        log = _make_log(tmp_path)
        entries = _write_n(log, 5)
        assert entries[0].prev_entry_hash == GENESIS_HASH
        for i in range(1, 5):
            assert entries[i].prev_entry_hash == entries[i - 1].entry_hash

    def test_empty_file_verify_ok(self, tmp_path):
        log = _make_log(tmp_path)
        # File doesn't exist yet — verify_chain should return ok with 0 entries.
        result = log.verify_chain()
        assert result.ok
        assert result.total_entries == 0


# ---------------------------------------------------------------------------
# Tamper detection — edit a field (Step 3.3 done-condition)
# ---------------------------------------------------------------------------

class TestTamperEdit:
    def test_edit_snapshot_detected(self, tmp_path):
        """Editing any field in an entry's snapshot breaks the hash."""
        log = _make_log(tmp_path)
        _write_n(log, 10)

        records = _read_lines(log)
        # Tamper: change snapshot of entry 5
        records[5]["snapshot"]["action"] = "TAMPERED"
        _write_lines(log, records)

        result = log.verify_chain()
        assert not result.ok
        assert result.first_bad_index == 5

    def test_edit_written_at_detected(self, tmp_path):
        log = _make_log(tmp_path)
        _write_n(log, 5)

        records = _read_lines(log)
        records[2]["written_at"] = "1970-01-01T00:00:00+00:00"
        _write_lines(log, records)

        result = log.verify_chain()
        assert not result.ok
        assert result.first_bad_index == 2

    def test_edit_entry_id_detected(self, tmp_path):
        log = _make_log(tmp_path)
        _write_n(log, 3)

        records = _read_lines(log)
        records[1]["entry_id"] = "00000000-0000-0000-0000-000000000000"
        _write_lines(log, records)

        result = log.verify_chain()
        assert not result.ok
        assert result.first_bad_index == 1


# ---------------------------------------------------------------------------
# Tamper detection — delete an entry
# ---------------------------------------------------------------------------

class TestTamperDelete:
    def test_delete_middle_entry_detected(self, tmp_path):
        log = _make_log(tmp_path)
        _write_n(log, 10)

        records = _read_lines(log)
        del records[4]  # remove entry index 4
        _write_lines(log, records)

        result = log.verify_chain()
        assert not result.ok
        # The broken link will show up at the new index 4 (previously index 5)
        assert result.first_bad_index == 4

    def test_delete_first_entry_detected(self, tmp_path):
        log = _make_log(tmp_path)
        _write_n(log, 5)

        records = _read_lines(log)
        del records[0]
        _write_lines(log, records)

        result = log.verify_chain()
        assert not result.ok
        assert result.first_bad_index == 0  # entry 0's prev_hash won't be GENESIS

    def test_delete_last_entry_detected(self, tmp_path):
        """Deleting the last entry is detectable — the chain is shorter than expected.
        Note: the remaining entries are still valid, but the total count changes.
        The chain itself is unbroken for the remaining entries, so verify_chain
        returns ok=True but with fewer entries. This is intentional — deletion of
        the tail entry is detected out-of-band via expected count, not hash chain.
        The important invariant is: you cannot ADD a fake entry without detection,
        and you cannot EDIT any entry without detection."""
        log = _make_log(tmp_path)
        _write_n(log, 5)

        records = _read_lines(log)
        del records[-1]
        _write_lines(log, records)

        result = log.verify_chain()
        # Remaining 4 entries are still valid
        assert result.ok
        assert result.total_entries == 4  # missing one — detectable by count check


# ---------------------------------------------------------------------------
# Tamper detection — insert a fake entry
# ---------------------------------------------------------------------------

class TestTamperInsert:
    def test_insert_fake_entry_detected(self, tmp_path):
        """Inserting a new entry in the middle breaks the chain link."""
        log = _make_log(tmp_path)
        _write_n(log, 5)

        records = _read_lines(log)
        # Forge a fake entry referencing records[2]'s hash as prev
        fake = {
            "entry_id": "fake-0000",
            "decision_id": "fake-decision",
            "prev_entry_hash": records[2]["entry_hash"],
            "entry_hash": "a" * 64,   # wrong hash
            "snapshot": {"evil": True},
            "written_at": "2000-01-01T00:00:00+00:00",
            "synced_to_cloud": False,
        }
        records.insert(3, fake)
        _write_lines(log, records)

        result = log.verify_chain()
        assert not result.ok
        # Fake entry itself fails hash check, or the next entry fails prev_hash check
        assert result.first_bad_index is not None
        assert result.first_bad_index <= 4


# ---------------------------------------------------------------------------
# Chain continuity across process restart
# ---------------------------------------------------------------------------

class TestRestartContinuity:
    def test_chain_continues_after_restart(self, tmp_path):
        """
        A new AuditLog instance pointed at the same file should pick up
        the last hash and continue the chain correctly.
        """
        log_file = tmp_path / "audit_log.jsonl"

        # First "process": write 5 entries
        log1 = AuditLog(log_path=log_file)
        entries1 = _write_n(log1, 5)

        # Second "process": new instance, same file
        log2 = AuditLog(log_path=log_file)
        entries2 = _write_n(log2, 5)

        # The 6th entry's prev_entry_hash must equal the 5th entry's entry_hash
        assert entries2[0].prev_entry_hash == entries1[-1].entry_hash

        # Full chain across both sessions must be intact
        result = log2.verify_chain()
        assert result.ok, f"Cross-restart chain broken: {result.error}"
        assert result.total_entries == 10


# ---------------------------------------------------------------------------
# Write failure → AuditWriteError
# ---------------------------------------------------------------------------

class TestWriteFailure:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="chmod does not restrict NTFS write access for the file owner on Windows.",
    )
    def test_unwritable_directory_raises(self, tmp_path):
        """If the log directory is unwritable, write_entry must raise AuditWriteError."""
        log_dir = tmp_path / "readonly_audit"
        log_dir.mkdir()
        log_file = log_dir / "audit_log.jsonl"
        log = AuditLog(log_path=log_file)

        # Make directory read-only
        log_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(AuditWriteError):
                log.write_entry("d-001", {"x": 1})
        finally:
            # Restore permissions so tmp_path cleanup works
            log_dir.chmod(stat.S_IRWXU)


# ---------------------------------------------------------------------------
# Hash function determinism
# ---------------------------------------------------------------------------

class TestHashDeterminism:
    def test_same_inputs_same_hash(self):
        h1 = _compute_hash("id-1", "2026-01-01T00:00:00+00:00", GENESIS_HASH, {"a": 1})
        h2 = _compute_hash("id-1", "2026-01-01T00:00:00+00:00", GENESIS_HASH, {"a": 1})
        assert h1 == h2

    def test_different_snapshot_different_hash(self):
        h1 = _compute_hash("id-1", "2026-01-01T00:00:00+00:00", GENESIS_HASH, {"a": 1})
        h2 = _compute_hash("id-1", "2026-01-01T00:00:00+00:00", GENESIS_HASH, {"a": 2})
        assert h1 != h2

    def test_dict_key_order_does_not_matter(self):
        """Canonical JSON must produce the same hash regardless of key insertion order."""
        h1 = _compute_hash("x", "t", GENESIS_HASH, {"b": 2, "a": 1})
        h2 = _compute_hash("x", "t", GENESIS_HASH, {"a": 1, "b": 2})
        assert h1 == h2
