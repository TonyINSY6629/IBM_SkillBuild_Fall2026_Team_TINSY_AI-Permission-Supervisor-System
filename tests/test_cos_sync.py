"""
tests/test_cos_sync.py

Tests for supervisor/cos_sync.py. No real IBM Cloud connection is used:
a fake COS resource records uploads.

Key regression: the sync must never modify audit_log.jsonl. An entry written
while an upload is in progress must survive, and the hash chain must stay
intact.

Run with:
    python -m pytest tests/test_cos_sync.py -v
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import supervisor.cos_sync as cs
from supervisor.audit import AuditLog


class _FakeObject:
    def __init__(self, store, key, delay, fail):
        self._store, self._key, self._delay, self._fail = store, key, delay, fail

    def put(self, Body):
        time.sleep(self._delay)
        if self._key in self._fail:
            raise RuntimeError("simulated upload failure")
        self._store[self._key] = Body


class FakeCOS:
    def __init__(self, delay=0.0, fail=()):
        self.store = {}
        self.delay = delay
        self.fail = set(fail)

    def Object(self, bucket, key):
        return _FakeObject(self.store, key, self.delay, self.fail)


def _setup(tmp_path, monkeypatch):
    log_path = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr(cs, "_AUDIT_LOG_PATH", log_path)
    monkeypatch.setattr(cs, "_SYNCED_IDS_PATH", tmp_path / "cos_synced_ids.txt")
    return AuditLog(log_path=log_path), log_path


def test_uploads_every_entry_once(tmp_path, monkeypatch):
    log, _ = _setup(tmp_path, monkeypatch)
    for i in range(5):
        log.write_entry(f"d{i}", {"seq": i})
    cos = FakeCOS()
    assert cs._sync_once(cos) == 5
    assert len(cos.store) == 5
    # Second run uploads nothing new.
    assert cs._sync_once(cos) == 0


def test_never_modifies_audit_log(tmp_path, monkeypatch):
    log, log_path = _setup(tmp_path, monkeypatch)
    for i in range(3):
        log.write_entry(f"d{i}", {"seq": i})
    before = log_path.read_bytes()
    cs._sync_once(FakeCOS())
    assert log_path.read_bytes() == before


def test_entry_written_during_upload_is_not_lost(tmp_path, monkeypatch):
    """Regression for the old rewrite-the-log bug (7 written, 6 kept)."""
    log, log_path = _setup(tmp_path, monkeypatch)
    for i in range(5):
        log.write_entry(f"d{i}", {"seq": i})

    t = threading.Thread(target=cs._sync_once, args=(FakeCOS(delay=0.1),))
    t.start()
    time.sleep(0.15)                       # sync is mid-upload
    log.write_entry("d5", {"seq": 5, "record_type": "human_feedback"})
    t.join()
    log.write_entry("d6", {"seq": 6})

    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 7
    assert log.verify_chain().ok


def test_failed_upload_retries_next_cycle(tmp_path, monkeypatch):
    log, _ = _setup(tmp_path, monkeypatch)
    e0 = log.write_entry("d0", {"seq": 0})
    log.write_entry("d1", {"seq": 1})

    cos = FakeCOS(fail={f"audit/{e0.entry_id}.json"})
    assert cs._sync_once(cos) == 1          # d1 uploaded, d0 failed
    cos.fail.clear()
    assert cs._sync_once(cos) == 1          # d0 retried and uploaded
    assert len(cos.store) == 2


def test_incomplete_last_line_is_skipped(tmp_path, monkeypatch):
    log, log_path = _setup(tmp_path, monkeypatch)
    log.write_entry("d0", {"seq": 0})
    with log_path.open("a", encoding="utf-8") as f:
        f.write('{"entry_id": "half-writ')   # simulated partial write
    assert cs._sync_once(FakeCOS()) == 1
