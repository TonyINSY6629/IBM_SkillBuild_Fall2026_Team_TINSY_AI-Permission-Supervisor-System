"""
scripts/export_to_dataset.py — Export Audit Log to COS Dataset Store

Reads the local audit_log.jsonl, uploads the full export to ais-dataset-store,
and builds one labeled example per human Allow/Deny answer (joined to the
tool call it was about). Timeouts, Bob's housekeeping tools and ids listed
in dataset/exclude_tool_use_ids.txt are left out. Hard-rail records go to
dataset/review_only.jsonl and are never used for training.

  dataset/held_out_test_set.jsonl   — ~20% of examples, NEVER used for training
  dataset/training_set.jsonl        — the rest
  dataset/review_only.jsonl         — hard-rail records, review only

Held-out membership is decided by a hash of each tool_use_id, so re-running
with more data never moves an example between the two sets.

Usage:
  python scripts/export_to_dataset.py

Required environment variables (same as cos_sync.py):
  COS_ENDPOINT        e.g. https://s3.us-south.cloud-object-storage.appdomain.cloud
  COS_API_KEY         IBM Cloud IAM API key from ais-dataset-writer credential
  COS_INSTANCE_ID     resource_instance_id from ais-dataset-writer credential
  COS_DATASET_BUCKET  bucket name, default: ais-dataset-store
  AUDIT_LOG_PATH      path to audit_log.jsonl (default: ~/supervisor_audit/audit_log.jsonl)

Optional:
  HELD_OUT_FRACTION   fraction to reserve as held-out (default: 0.20)
                      Split is hash-based (tool_use_id), not random.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COS_ENDPOINT: str = os.environ.get(
    "COS_ENDPOINT", "https://s3.us-south.cloud-object-storage.appdomain.cloud"
)
COS_API_KEY: str | None = os.environ.get("COS_API_KEY")
COS_INSTANCE_ID: str | None = os.environ.get("COS_INSTANCE_ID")
COS_DATASET_BUCKET: str = os.environ.get("COS_DATASET_BUCKET", "ais-dataset-store")

_DEFAULT_LOG = Path.home() / "supervisor_audit" / "audit_log.jsonl"
AUDIT_LOG_PATH: Path = Path(os.environ.get("AUDIT_LOG_PATH", str(_DEFAULT_LOG)))

HELD_OUT_FRACTION: float = float(os.environ.get("HELD_OUT_FRACTION", "0.20"))

OUTPUT_DIR: Path = Path(__file__).parent.parent / "dataset"
EXCLUDE_FILE: Path = Path(__file__).parent.parent / "config" / "exclude_tool_use_ids.txt"

# Bob's own housekeeping tools — answers to these are not safety labels.
HOUSEKEEPING_TOOLS: frozenset[str] = frozenset({
    "ask_followup_question",
    "update_todo_list",
    "spawn_subagent",
    "attempt_completion",
    "switch_mode",
    "start_subtask",
    "new_task",
})

# Fields copied from the escalation decision into each example.
_REQUEST_FIELDS = (
    "tool_use_id", "session_id", "prompt_id", "prompt_text",
    "tool_name", "tool_input", "cwd", "action_type", "category",
    "targets", "inside_workspace", "touches_secrets",
    "label", "matched_rule_id", "reason", "policy_version",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_entries() -> list[dict]:
    if not AUDIT_LOG_PATH.exists():
        print(f"ERROR: Audit log not found at {AUDIT_LOG_PATH}", file=sys.stderr)
        sys.exit(1)

    entries = []
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"WARNING: skipping malformed line {i}: {exc}", file=sys.stderr)

    return entries


def _load_excluded_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as f:
        return {
            line.split("#", 1)[0].strip()
            for line in f
            if line.split("#", 1)[0].strip()
        }


def build_examples(
    entries: list[dict], excluded_ids: set[str] | None = None
) -> tuple[list[dict], list[dict], Counter]:
    """
    Return (examples, review_only, skipped).

    examples    — one dict per usable human answer (request fields + human label)
    review_only — hard-rail deny snapshots (never used for training)
    skipped     — counts of why answers were left out

    Changed from original (2026-11-06, Christina): replaced shuffle-based split
    with per-answer join logic. The old version shuffled raw log lines so a
    decision and its human answer could land in different sets, auto-allows and
    hard-rail records went into training, and adding one entry reshuffled the
    held-out set. Now: one example per human answer joined to its decision,
    timeouts/housekeeping/excluded ids left out, hard rails kept separate.
    """
    excluded_ids = excluded_ids or set()
    escalations: dict[str, dict] = {}
    review_only: list[dict] = []
    skipped: Counter = Counter()
    examples: list[dict] = []

    # First pass: index escalation decisions by tool_use_id.
    for e in entries:
        snap = e.get("snapshot", {})
        if snap.get("record_type") == "human_feedback":
            continue
        if snap.get("for_review_only"):
            review_only.append({"written_at": e.get("written_at"), **snap})
        elif snap.get("action") == "escalate" and snap.get("tool_use_id"):
            escalations[snap["tool_use_id"]] = {"written_at": e.get("written_at"), **snap}

    # Second pass: one example per human answer.
    seen: set[str] = set()
    for e in entries:
        fb = e.get("snapshot", {})
        if fb.get("record_type") != "human_feedback":
            continue
        tid = fb.get("tool_use_id")
        if not fb.get("sent_to_pipeline"):
            skipped["timeout (no human answer)"] += 1
            continue
        if tid in excluded_ids:
            skipped["listed in exclude_tool_use_ids.txt"] += 1
            continue
        if tid in seen:
            skipped["duplicate answer"] += 1
            continue
        decision = escalations.get(tid)
        if decision is None:
            skipped["no matching escalation decision"] += 1
            continue
        if decision.get("tool_name") in HOUSEKEEPING_TOOLS:
            skipped["housekeeping tool"] += 1
            continue

        seen.add(tid)
        example = {k: decision.get(k) for k in _REQUEST_FIELDS}
        example.update({
            "human_decision": fb.get("final_action"),   # "allow" | "deny"
            "human_outcome": fb.get("outcome"),         # human_allow | human_deny
            "escalation_id": fb.get("escalation_id"),
            "asked_at": decision.get("written_at"),
            "answered_at": e.get("written_at"),
        })
        examples.append(example)

    return examples, review_only, skipped


def is_held_out(tool_use_id: str, fraction: float = HELD_OUT_FRACTION) -> bool:
    """
    Stable assignment: depends only on the id, never on the other data.
    Re-running with more data never moves an example between sets.
    """
    digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0x100000000 < fraction


def split(examples: list[dict], fraction: float = HELD_OUT_FRACTION) -> tuple[list[dict], list[dict]]:
    """Return (held_out, training)."""
    held_out = [x for x in examples if is_held_out(x["tool_use_id"], fraction)]
    training = [x for x in examples if not is_held_out(x["tool_use_id"], fraction)]
    return held_out, training


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _upload_to_cos(entries: list[dict], timestamp: str) -> None:
    """Upload full export as a single JSONL object to ais-dataset-store."""
    if not COS_API_KEY or not COS_INSTANCE_ID:
        print(
            "WARNING: COS_API_KEY or COS_INSTANCE_ID not set — "
            "skipping COS upload. Split files will still be written locally.",
            file=sys.stderr,
        )
        return

    try:
        import ibm_boto3
        from ibm_botocore.client import Config
    except ImportError:
        print(
            "WARNING: ibm-cos-sdk not installed — skipping COS upload.\n"
            "Install with: pip install ibm-cos-sdk",
            file=sys.stderr,
        )
        return

    cos = ibm_boto3.resource(
        "s3",
        ibm_api_key_id=COS_API_KEY,
        ibm_service_instance_id=COS_INSTANCE_ID,
        config=Config(signature_version="oauth"),
        endpoint_url=COS_ENDPOINT,
    )

    object_key = f"exports/audit_export_{timestamp}.jsonl"
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries).encode("utf-8")

    cos.Object(COS_DATASET_BUCKET, object_key).put(Body=body)
    print(f"Uploaded {len(entries)} entries to cos://{COS_DATASET_BUCKET}/{object_key}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Loading audit log from: {AUDIT_LOG_PATH}")
    entries = _load_entries()
    print(f"Loaded {len(entries)} log entries.")

    excluded = _load_excluded_ids(EXCLUDE_FILE)
    if excluded:
        print(f"Excluding {len(excluded)} tool_use_ids listed in {EXCLUDE_FILE}")

    # 1. Upload the raw export to the COS dataset bucket (archive copy).
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _upload_to_cos(entries, timestamp)

    # 2. One labeled example per human answer, then the stable split.
    examples, review_only, skipped = build_examples(entries, excluded)
    held_out, training = split(examples)

    labels = Counter(x["human_decision"] for x in examples)
    print(f"\nLabeled examples: {len(examples)}  (allow={labels['allow']}, deny={labels['deny']})")
    for reason, n in skipped.most_common():
        print(f"  left out - {reason}: {n}")

    held_out_path = OUTPUT_DIR / "held_out_test_set.jsonl"
    training_path = OUTPUT_DIR / "training_set.jsonl"
    review_path = OUTPUT_DIR / "review_only.jsonl"
    _write_jsonl(held_out_path, held_out)
    _write_jsonl(training_path, training)
    _write_jsonl(review_path, review_only)

    print(f"\nSplit (held_out_fraction={HELD_OUT_FRACTION}, by hash of tool_use_id):")
    print(f"  Held-out test set : {len(held_out):>5} examples -> {held_out_path}")
    print(f"  Training set      : {len(training):>5} examples -> {training_path}")
    print(f"  Review only       : {len(review_only):>5} records  -> {review_path}")
    print(
        "\nIMPORTANT: The held-out test set must never be used for training or "
        "DSPy optimization. It is the final evaluation gate only."
    )


if __name__ == "__main__":
    main()
