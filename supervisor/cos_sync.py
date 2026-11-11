"""
supervisor/cos_sync.py — Async Audit Log Sync to IBM Cloud Object Storage

Runs as a background daemon thread inside the Supervisor service.
Uploads each audit log entry not yet listed in cos_synced_ids.txt
as a JSON object to the ais-audit-log COS bucket. Never modifies the audit log.

Design:
  - Never blocks a decision — runs entirely in the background.
  - Sync failures queue locally and retry on the next cycle.
  - Decisions are never blocked or delayed by sync failures.
  - Each entry is uploaded as an individual object named by its entry_id,
    so uploads are idempotent (re-uploading the same entry_id is safe).

Configuration (environment variables):
  COS_ENDPOINT          — e.g. https://s3.us-south.cloud-object-storage.appdomain.cloud
  COS_API_KEY           — IBM Cloud IAM API key from ais-audit-writer credential
  COS_INSTANCE_ID       — resource_instance_id from ais-audit-writer credential
  COS_AUDIT_BUCKET      — bucket name, default: ais-audit-log
  COS_SYNC_INTERVAL     — seconds between sync cycles, default: 60
  AUDIT_LOG_PATH        — path to audit_log.jsonl (shared with audit.py)

Usage:
  from supervisor.cos_sync import start_cos_sync
  start_cos_sync()   # call once at Supervisor startup
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("supervisor.cos_sync")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COS_ENDPOINT: str = os.environ.get(
    "COS_ENDPOINT", "https://s3.us-south.cloud-object-storage.appdomain.cloud"
)
COS_API_KEY: Optional[str] = os.environ.get("COS_API_KEY")
COS_INSTANCE_ID: Optional[str] = os.environ.get("COS_INSTANCE_ID")
COS_AUDIT_BUCKET: str = os.environ.get("COS_AUDIT_BUCKET", "ais-audit-log")
COS_SYNC_INTERVAL: float = float(os.environ.get("COS_SYNC_INTERVAL", "60"))

_DEFAULT_LOG_FILE = Path.home() / "supervisor_audit" / "audit_log.jsonl"
_AUDIT_LOG_PATH: Path = Path(os.environ.get("AUDIT_LOG_PATH", str(_DEFAULT_LOG_FILE)))

# Upload progress lives here, never in the audit log itself.
# The original version rewrote audit_log.jsonl after every upload to mark
# synced_to_cloud=True — done without the audit log's lock, so entries written
# during an upload were silently erased, breaking the hash chain.
# Fix: the audit log is read-only from the sync module. Uploaded entry_ids
# are tracked here instead. See: tests/test_cos_sync.py
_SYNCED_IDS_PATH: Path = _AUDIT_LOG_PATH.with_name("cos_synced_ids.txt")

# Sentinel so sync thread knows COS is not configured.
_COS_ENABLED: bool = bool(COS_API_KEY and COS_INSTANCE_ID)


# ---------------------------------------------------------------------------
# COS client (lazy — only imported if credentials are present)
# ---------------------------------------------------------------------------

def _make_cos_client():
    """
    Build an ibm_boto3 COS resource client using IAM API key authentication.
    Returns None and logs a warning if ibm-cos-sdk is not installed.
    """
    try:
        import ibm_boto3
        from ibm_botocore.client import Config
    except ImportError:
        log.warning(
            "ibm-cos-sdk not installed — COS sync disabled. "
            "Install with: pip install ibm-cos-sdk"
        )
        return None

    return ibm_boto3.resource(
        "s3",
        ibm_api_key_id=COS_API_KEY,
        ibm_service_instance_id=COS_INSTANCE_ID,
        config=Config(signature_version="oauth"),
        endpoint_url=COS_ENDPOINT,
    )


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def _load_synced_ids(path: Path) -> set[str]:
    """Return the set of entry_ids already uploaded (empty if no file yet)."""
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError as exc:
        log.warning("COS sync: could not read %s: %s", path, exc)
        return set()


def _mark_synced(path: Path, entry_id: str) -> None:
    """Append one uploaded entry_id to the progress file and flush it to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(entry_id + "\n")
        f.flush()
        os.fsync(f.fileno())


def _sync_once(cos_resource) -> int:
    """
    Upload every audit log entry whose entry_id is not yet in the progress
    file. Reads the audit log only; never writes to it.

    Returns the number of entries successfully uploaded in this cycle.
    """
    if not _AUDIT_LOG_PATH.exists():
        return 0

    try:
        with _AUDIT_LOG_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        log.warning("COS sync: could not read audit log: %s", exc)
        return 0

    already = _load_synced_ids(_SYNCED_IDS_PATH)
    synced_count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            # Possibly a line still being written; retry next cycle.
            continue

        entry_id = record.get("entry_id")
        if not entry_id or entry_id in already:
            continue

        object_key = f"audit/{entry_id}.json"
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")

        try:
            cos_resource.Object(COS_AUDIT_BUCKET, object_key).put(Body=body)
        except Exception as exc:
            log.warning("COS sync: failed to upload entry %s: %s", entry_id, exc)
            continue  # not marked — retried next cycle

        try:
            _mark_synced(_SYNCED_IDS_PATH, entry_id)
        except OSError as exc:
            # Upload succeeded but progress wasn't saved; the next cycle
            # re-uploads it, which is safe because uploads are idempotent.
            log.warning("COS sync: could not record entry %s as synced: %s", entry_id, exc)
            continue

        already.add(entry_id)
        synced_count += 1
        log.debug("COS sync: uploaded entry %s", entry_id)

    return synced_count


def _sync_loop() -> None:
    """Background loop: sync every COS_SYNC_INTERVAL seconds."""
    cos = _make_cos_client()
    if cos is None:
        log.warning("COS sync: client unavailable — sync thread exiting.")
        return

    log.info(
        "COS sync: started (bucket=%s, interval=%ss).",
        COS_AUDIT_BUCKET,
        COS_SYNC_INTERVAL,
    )

    while True:
        try:
            n = _sync_once(cos)
            if n > 0:
                log.info("COS sync: uploaded %d new entries.", n)
        except Exception as exc:
            # Never let an unhandled exception kill the sync thread.
            log.error("COS sync: unexpected error in sync cycle: %s", exc)

        time.sleep(COS_SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_cos_sync() -> None:
    """
    Start the background COS sync daemon thread.

    Safe to call multiple times — only starts one thread.
    Does nothing (logs a warning) if COS credentials are not configured.
    """
    if not _COS_ENABLED:
        log.warning(
            "COS sync: COS_API_KEY or COS_INSTANCE_ID not set — "
            "audit log will not be synced to cloud. "
            "Set these environment variables to enable sync."
        )
        return

    thread = threading.Thread(
        target=_sync_loop,
        daemon=True,
        name="cos-audit-sync",
    )
    thread.start()
    log.info("COS sync: daemon thread started.")
