# AI Permission Supervisor — Implementation Plan
**Team TINSY** | IBM SkillBuild Fall 2026
**Scope:** IBM Bob IDE (runtime) + IBM Cloud / watsonx.ai (build-time)

---

## Overview

A runtime guardrail that intercepts every tool-call Bob Agent attempts, classifies it, and
acts before the tool runs. The three possible outcomes for any tool-call are:

| Action | Meaning |
|---|---|
| allow | Tool runs immediately, no prompt to human |
| escalate | Human approval required before tool runs |
| deny | Tool is blocked; reason returned to model |

Fail-closed is not a separate action — it is a condition (Supervisor error, log write
failure, or timeout) that always results in `deny` / exit 2.

The system is split across two environments:
- **Local machine (Bob IDE + Supervisor process):** interception, normalization, rule evaluation, escalation UI, audit log
- **IBM Cloud / watsonx.ai:** policy versioning, audit sync, build-time distillation pipeline

---

## Architecture Decisions (Locked)

### Bob API Constraints
- Bob supports exactly **two meaningful exit codes** from `PreToolUse`:
  - `exit 0` → tool runs
  - `exit 2` → tool blocked
  - Any other non-zero → non-blocking failure, **tool runs** (fails open)
- **If the Hook Client process itself crashes**, Bob gets a non-2 exit and the tool runs.
  The Hook Client must therefore be kept minimal and catch every error, exiting 2 on any
  unhandled exception. The safety guarantee only holds while the Hook Client is alive.
- Block reason: **Assumed:** `exit 2` + message on **stderr** (or stdout if stderr is empty)
  is passed back to the model by Bob, preventing blind retries. (Unconfirmed — see Open Items.)
- Hook **default timeout is 10 seconds** — fails open on expiry. The PreToolUse hook timeout
  must be set longer than the maximum human approval wait time.
- `Stop`, `PostToolUse`, `SessionStart` **cannot block** — exit 2 from these is a
  non-blocking failure. Fail-closed enforcement lives entirely in `PreToolUse`.
- The hook list is: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`.
  There is no `PermissionRequest` hook.

### Label and Action Schema
- **Labels** (classifier output): `Safe` | `Neutral` | `Warning`
  - `Safe` — auto-allow, no prompt to human. Every decision is logged before its exit
    code — "silent" means no human prompt, not no log entry.
  - `Neutral` — auto-allow, passive log entry (same as Safe, distinguished for audit granularity)
  - `Warning` — escalate to human OR deny depending on policy
- **Actions** (Policy Engine verdict): `allow` | `deny` | `escalate`
- **decided_by** values: `rule` (clear-cut allow) | `hard_rail` (hard-rail deny) | `model`
- There is no `Block` label. A block is `action=deny`.

### Fail-Closed Invariant
Every decision path must write and confirm an audit log entry **before** any exit code is
issued. If the local log write fails (entry_hash not confirmed), the Hook Client exits 2.
No action ever takes effect unrecorded.

### Audit Log Properties
- Append-only, hash-chained (`prev_entry_hash` + `entry_hash` on every entry)
- Local write is **synchronous** — flushed to disk before verdict is returned
- Stored in the Supervisor's own directory, outside the agent's workspace reach
- Async batch sync to IBM Cloud Object Storage in the background
- Sync failures queue locally and retry — decisions are never blocked by sync failures

### Human Override Semantics
- A blocked tool-call (deny) stays blocked for the current session — no session-level unlock
- Human override is a **feedback signal only** — sent to the training pipeline for future
  reclassification
- Hard-rail deny feedback: `for_review_only=true`, `sent_to_pipeline=false` — cannot
  loosen a hard rail through training
- Human-deny (escalation rejected by human) feedback: `sent_to_pipeline=true`
- Model-deny overridden by human: `sent_to_pipeline=true` — these are the most useful
  training examples, as they directly correct model errors

### Component Location Split

| Component | Lives On |
|---|---|
| Hook configuration (Bob global settings + Hook Client script) | IBM Bob IDE |
| Hook Client / Supervisor Process | Local Machine |
| Rule Engine (policy.yaml + taxonomy.yaml local copy) | Local Machine |
| Policy Engine | Local Machine |
| Context Store | Local Machine |
| Escalation Manager (approval prompt UI) | Local Machine |
| Audit Log (primary) | Local Machine |
| Retriever + Supervisor Model (student, ambiguous path) | Local Machine |
| MCP Gateway / Proxy | Local Machine (separate process, not part of Bob) |
| MCP Server & Tools | Local or Remote (third-party) |
| policy.yaml + taxonomy.yaml (versioned source) | IBM Cloud |
| Audit Log sync target + Dataset Store | IBM Cloud Object Storage |
| Teacher Models, DSPy, Distillation, Evaluation, Registry | IBM Cloud / watsonx.ai |

### Retriever / Model Co-location Rule
The Retriever and Supervisor Model always move together. If a machine cannot run the
student model locally, both Retriever and Model fall back to IBM Cloud watsonx.ai in the
same call. The Rule Engine and Policy Engine never move — they always run locally.

### MCP Gateway as Sole Choke Point
Bob Agent must reach MCP servers only through the Gateway. There is no direct path from
agent to tools. The Gateway forwards allowed calls and returns results to the agent.

---

## Data Model (Runtime — Local DB)

Core entities and their key fields. Full Data Dictionary maintained separately in
`AI_Permission_Supervisor_System_Bob_DataSchema.pdf`.

| Entity | Key Fields |
|---|---|
| Session | session_id, cwd, source, started_at, ended_at |
| Prompt | prompt_id, session_id, prompt_text, received_at |
| ToolCallRequest | tool_use_id, session_id, prompt_id, tool_name, tool_input_json, action_type, targets_json, inside_workspace, touches_secrets |
| Decision | tool_use_id, label, action, decided_by, category, matched_rule_id, policy_version, **model_version_id**, **index_version_id**, **confidence**, reason, decided_at |
| ToolOutcome | tool_use_id, tool_response, completed_at |
| HumanFeedback | feedback_id, tool_use_id, outcome, source, note, for_review_only, sent_to_pipeline, logged_at |
| AuditLogEntry | entry_id, decision_id, prev_entry_hash, entry_hash, snapshot, written_at, synced_to_cloud |
| PolicyVersion | policy_version_id, version_tag, synced_at, is_active |
| PolicyRule | rule_id, policy_version_id, rule_name, condition, verdict, priority |
| TaxonomyLabel | label_id, policy_version_id, label_name, description, severity_rank |

Hash chain serialization: `entry_hash = sha256(canonical_json(entry_id + written_at + prev_entry_hash + snapshot))`.
Use canonical JSON (sorted keys, no whitespace) to ensure deterministic hashing across platforms.

Build-time entities (TrainingRecord, TeacherLabel, DSPyRun, ModelVersion, IndexVersion,
TestCase, EvaluationRun) live on IBM Cloud. The two layers connect only through:
- Audit Log → async sync → IBM Cloud Object Storage → Dataset Store (runtime → build-time)
- Model & Index Registry → distribute → local Supervisor Model + Retriever (build-time → runtime)

---

## Implementation Roadmap

### Step 1 — Probe Bob with a Logging-Only Hook (~1 hour)

**Do this before writing anything else.**

Install `PreToolUse`, `UserPromptSubmit`, `PostToolUse`, and `Stop` hooks that only
append the raw payload to a local file and exit 0. Use Bob normally for a day. This confirms:

1. Real `tool_name` values and `tool_input` shapes — the Normalizer depends on these
2. Whether `PreToolUse` fires before or after Bob's own approval prompt
3. Whether the stderr reason from `exit 2` reaches the model (unconfirmed — see Open Items)
4. Whether a hook can hold open (long timeout) without Bob misbehaving
5. **Whether `PreToolUse` fires for MCP tool calls.** If yes, the hook already covers MCP
   in Bob and the Gateway becomes optional for the MVP.

The log from this step becomes the first dataset and grounds the Normalizer field mappings.
**Do not finalize the PermissionRequest Pydantic model until after this step.**

- [ ] pending

---

### Step 2 — Write Minimum Specs as Files

Only these three files block the first build. Everything else can wait.

#### 2a. Pydantic Schemas
`schemas/permission_request.py` and `schemas/verdict.py`
- `PermissionRequest`: fields derived from the Step 1 log
- `Verdict`: label (enum: Safe/Neutral/Warning), action (enum: allow/deny/escalate),
  decided_by, reason, matched_rule_id, policy_version, confidence (optional)

#### 2b. Taxonomy
`policy/taxonomy.yaml` — category list with:
- id, description, example tools, example command patterns, default_label

Initial categories to define (expand from Step 1 observations):
- `file_read`, `file_write`, `shell_exec`, `network_request`, `secret_access`,
  `supervisor_path_access`, `workspace_outside_write`

#### 2c. Policy
`policy/policy.yaml` — decision table and hard rails including:
- Hard rail: **deny any action targeting the Supervisor's own files/paths**
- Hard rail: **deny any write to `~/.bob/` and Hook Client files** — prevents the agent
  from editing `~/.bob/settings/settings.json` to remove the hooks
- Hard rail: deny writes outside workspace roots
- Clear-cut allow: read-only operations on workspace files
- Everything else: escalate (until model is in place)

**Hook registration:** Register hooks **globally** (in user-level Bob settings) or via
`EnforcedHooks` policy — not in workspace settings, which the agent can edit.

**The model I/O spec, threat model, and evaluation thresholds wait until the teacher
and distillation phases.**

- [ ] pending

---

### Step 3 — Build the Rules-Only Runtime

Build in this exact order. Each item has a done-condition that must pass before the next starts.

#### 3.1 Hook Client
**File:** `hook_client/main.py` (or shell script)
**Done when:** Killing the Supervisor service makes every tool-call blocked, not allowed.
- Reads stdin (Bob hook payload JSON)
- Calls Supervisor service with payload
- Enforces its own deadline (shorter than Bob hook timeout)
- Exits 0 on allow, 2 + stderr reason on deny
- Top-level try/except: any unhandled exception → exit 2
- **Known limitation:** if the Hook Client process itself crashes, Bob receives a non-2
  exit and the tool runs (fails open). This cannot be prevented by the Hook Client.
  Mitigation: keep the Hook Client minimal and dependency-light to minimize crash surface.

**Required timeout ordering:**
  `escalation timeout (e.g. 120 s) < Hook Client deadline (e.g. 130 s) < Bob hook timeout (e.g. 150 s)`
  Set Bob hook `timeout: 150` in settings. Hook Client deadline: 130 s. Escalation wait: 120 s.
  This ensures the Hook Client always exits before Bob kills it, and always exits 2 on
  escalation timeout before Bob's timeout fires.

- Test: start Hook Client with Supervisor service down → every call exits 2

#### 3.2 Supervisor Service Skeleton
**File:** `supervisor/app.py`
**Done when:** Hook Client reaches it and gets a response.
- Local process (FastAPI or equivalent)
- Health endpoint `GET /health` → 200
- Stub `/decide` endpoint → always returns escalate for now
- **Policy sync lives here**, not in the Hook Client. The Supervisor service pulls
  `policy.yaml` + `taxonomy.yaml` from IBM Cloud at startup and on a background refresh
  interval. The Hook Client remains thin and does not touch policy files.

#### 3.3 Audit Log
**File:** `supervisor/audit.py`
**Done when:** Tampering with any entry (edit or delete) is detectable.
- Append-only file, stored in `supervisor_paths` (outside agent workspace)
- Each entry: `entry_id`, `prev_entry_hash`, `entry_hash`, `snapshot`, `written_at`
- Hash chain: `entry_hash = sha256(canonical_json(entry_id + written_at + prev_entry_hash + snapshot))`
  Use canonical JSON (sorted keys, no whitespace) for deterministic cross-platform hashing.
- Local write is synchronous; returns `entry_hash` before caller proceeds
- If write fails: caller must exit 2
- Test: write 10 entries, manually edit one, run integrity check → detects tamper

#### 3.4 Normalizer
**File:** `supervisor/normalizer.py`
**Done when:** All logged payloads from Step 1 convert to `PermissionRequest` without error.
- Input: raw `PreToolUse` payload (tool_name, tool_input, cwd, session_id, tool_use_id)
- Output: `PermissionRequest` (action_type, targets, inside_workspace, touches_secrets, ...)
- Test: replay every payload from Step 1 log → zero conversion errors

#### 3.5 Rule Engine + Policy Engine
**Files:** `supervisor/rule_engine.py`, `supervisor/policy_engine.py`
**Done when:** Hard rails deny; known-safe commands allow; everything else escalates.
- Rule Engine: loads `policy.yaml` + `taxonomy.yaml`, matches `PermissionRequest` to rules
- Clear-cut allow → `Verdict(action=allow, decided_by=rule)`
- Hard-rail deny → `Verdict(action=deny, decided_by=hard_rail)`
- Ambiguous → deferred (escalate until model is in place)
- Policy Engine: applies verdict, writes AuditLogEntry, returns exit instruction
- Test: supervisor-path write → hard-rail deny; workspace file read → allow; unknown → escalate

#### 3.6 Escalation Manager
**File:** `supervisor/escalation.py`
**Done when:** Timeout → deny, every time.
- Displays approval prompt (tool_name, category, reason) to human
- UI mechanism: **unconfirmed** — options are IDE extension UI, OS notification, or local
  web page. Confirm whether Bob accepts custom VS Code extensions (see Open Items).
- Waits up to configurable timeout (default: 120 seconds — must be < Hook Client deadline)
- Human approves → `action=allow`; human denies → `action=deny`; timeout → `action=deny`
- Test: send escalation, wait for timeout without responding → Hook Client exits 2
  (Bob's hook timeout does NOT fire first, because Hook Client deadline < Bob timeout)

#### 3.7 Context Store
**File:** `supervisor/context_store.py`
**Done when:** Task text from `UserPromptSubmit` is attached to each `PermissionRequest`.
- Captures `UserPromptSubmit` payload → stores `prompt_id` + `prompt_text` keyed by `session_id`
- Normalizer looks up current prompt for session and attaches to `PermissionRequest`
- Test: submit prompt, trigger tool-call, verify `PermissionRequest.prompt_id` is populated

**At this point:** working, fail-closed supervisor with no ML. Ambiguous cases go to human.
Each human-answered escalation is a labeled example for the training pipeline.

---

### Step 4 — Collect Labeled Data

Run the rules-only supervisor during real work.
- Every human-answered escalation produces one labeled example (HumanFeedback with
  `sent_to_pipeline=true`)
- Every hard-rail deny produces a review-only record (`for_review_only=true`)
- Collect until the dataset is large enough for teacher labeling (~200–500 examples minimum)

---

### Step 5 — Teacher Pipeline on watsonx.ai

After sufficient labeled data is collected:
1. Export AuditLog entries to IBM Cloud Object Storage (Dataset Store)
2. Reserve a **held-out hand-labeled test set** that is never used for training or DSPy
   optimization — do this before any optimization sees the data
3. **Optimize the teacher prompt with DSPy** (on the non-held-out sample) before bulk labeling —
   do not bulk-label until the prompt is validated
4. Run teacher ensemble (large watsonx.ai models) on ambiguous examples via DSPy
5. Distillation: fine-tune small student model on teacher outputs
6. Quantize student model
7. Run Evaluation Harness against held-out test set + `policy.yaml` + `taxonomy.yaml` test cases
8. If pass: push to Model & Index Registry
9. Distribute to local machines → student model plugs into the **ambiguous path after the
   Rule Engine** (not into the Rule Engine itself — Rule Engine remains YAML-only)

---

### Write Tests Alongside Every Component

Especially the fail-closed cases — these must never regress:
- Hook Client crash → tool runs (document this known limitation — cannot be tested as
  pass/fail; instead document it and minimize crash surface)
- Supervisor service down → Hook Client exits 2
- Audit log write failure → Hook Client exits 2
- **Hook Client deadline expiry → Hook Client exits 2** (Bob's hook timeout must not
  fire first — validate the timeout ordering in integration tests)
- Hard-rail path → always deny, regardless of human override
- `supervisor_paths` write attempt → always deny
- `~/.bob/` write attempt → always deny

---

## What to Do on watsonx.ai Now (In Parallel)

While the rules-only runtime is being built locally, these watsonx.ai tasks can be done
in parallel — none of them block Step 3, but all of them need to be ready before Step 5.

**Platform note:** The project uses **IBM watsonx.ai** (teacher labeling, fine-tuning,
model registry) and **IBM watsonx Orchestrate** (available but not used in the core
pipeline — reserved for future automation or escalation UI if needed). There is no
separate IBM Cloud account required; watsonx.ai is the sole build-time platform.

### 1. Provision the watsonx.ai Project and Storage

**IBM Cloud Object Storage is created automatically** when you create a watsonx.ai project.
Do not create a separate COS instance — use the one linked to the project.

Steps:
- Create a watsonx.ai project named `ai-permission-supervisor` at
  [dataplatform.ibm.com](https://dataplatform.ibm.com)
- Record the **Project ID** (UUID from Manage → General) — needed in Supervisor config
- In the auto-created COS instance (IBM Cloud → Resource list → Storage), create two
  additional buckets:
  - **`ais-audit-log`** — ⚠️ **Object Lock not available on Lite COS plan** (requires
    Standard plan). Object Lock is skipped for now. Mitigation: use a Writer-only IAM
    credential (no delete permission) to prevent accidental deletion by the Supervisor
    process. Known limitation: a bucket admin can still manually delete entries. Acceptable
    for a hackathon/student project; revisit if the COS plan is upgraded to Standard.
  - **`ais-dataset-store`** — no Object Lock; used for exported training data
- **IAM credential note:** COS has no append-only IAM role. Use a Writer credential
  (cannot delete objects) as the primary deletion guard on the audit bucket.
- Create two Service Credentials (Writer role each): `ais-audit-writer` and
  `ais-dataset-writer`. Store credential JSON securely — never commit to the repo.
- Configure the Supervisor's `SupervisorConfig.audit_db` sync endpoint with the
  `ais-audit-log` bucket endpoint and `ais-audit-writer` credential.

### 2. Set Up policy.yaml and taxonomy.yaml Version Store
- Create a Cloud Object Storage bucket (or a Git repo on IBM Cloud) for policy versioning
- Define the sync mechanism: **Supervisor service** (not Hook Client) pulls `policy.yaml`
  + `taxonomy.yaml` at startup and on a background refresh interval, falling back to
  cached copy on failure
- Version tag format: `YYYY-MM-DD-vN` matching `PolicyVersion.version_tag`

### 3. Explore watsonx.ai for Teacher Labeling
- Provision a watsonx.ai project
- Identify which foundation models will serve as teacher ensemble candidates —
  **check the current watsonx.ai model catalog** for available instruction-tuned models
  (`granite-13b-instruct` may no longer be offered; verify before planning around it)
- Draft the teacher prompt template: given a `NormalizedRequest` + context, output
  `{label, action, reason, confidence}`
- Test the prompt on 10–20 examples from the Step 1 log (even before Step 4 data is ready)
- This validates the teacher output format before the full distillation pipeline is built

### 4. Set Up DSPy on watsonx.ai
- Configure DSPy with the watsonx.ai backend
- Define the DSPy `Signature` for the classification task:
  input → `NormalizedRequest` + `context` + `policy_excerpt`
  output → `label`, `action`, `reason`, `confidence`
- Optimize the prompt using DSPy's `BootstrapFewShot` or `MIPROv2` optimizer
  against the labeled examples from Step 4

### 5. Plan the Distillation Pipeline
- **Student model architecture (locked):** **Small generative model, 1–3B parameters,
  4-bit quantized** (~1–2.5 GB RAM). Reason text and predicted outcome are core
  requirements; an encoder classifier cannot produce either.
  - Fallback: encoder classifier with templated reasons, if the generative model fails
    the latency/RAM budget on target machines.
  - `reason` and `predicted_outcome` are **optional** fields in the `Verdict` schema so
    either architecture fits without a schema change.
  - The 100 MB figure previously stated was for the RAG embedding model only, not the
    generative student. Do not conflate them.
- Plan the fine-tuning job structure on watsonx.ai
- Define evaluation thresholds (accuracy, precision, recall) for the pass/fail gate
  in the Evaluation Harness
- Define `TestCase` schema and write the first set of test cases from hard-rail examples
  (these are deterministic — ground truth is known)

### 6. Model & Index Registry Design
- Decide the distribution mechanism: how does the Registry push a new student model
  + index to developer machines?
- Options: **Supervisor service checks at startup** if a newer version exists in the Registry,
  or out-of-band update script
- Define `ModelVersion` and `IndexVersion` version tags and compatibility contract
  with the **Supervisor service** (the Hook Client never touches the model or index)

---

## Open Items (To Resolve Before Step 5)

| Item | Waiting On |
|---|---|
| **Confirm `PreToolUse` fires before Bob's own approval prompt** | ⚠️ Corrected (2026-11-10): `PreToolUse` fires reliably on every tool call (266 events logged), but in Step 4 testing Christina observed Bob's own approval prompt appears *before* the supervisor's escalation — so `PreToolUse` fires after Bob's prompt, not before it. Requires further investigation. |
| ~~**Confirm stderr reason actually reaches the model**~~ | ✅ Resolved (2026-11-10): confirmed in Step 4 testing. After a deny, Bob's next reply repeated the supervisor's reason text. |
| ~~Confirm hook can hold open > 10s without Bob misbehaving~~ | ✅ Resolved (2026-11-10): 51 `Stop` events and 7 `SessionStart` events with no Bob misbehaviour reported. Normal usage confirmed. Long-hold (>10s) test still pending — do once before wiring the Escalation Manager. |
| **Confirm `PreToolUse` fires for MCP tool calls** | ⏳ Still open (corrected 2026-11-10): no MCP tools were used during testing, so 0 MCP calls in the probe log does not prove `PreToolUse` skips them. Needs a deliberate MCP tool call test. Gateway remains required until confirmed. |
| **Confirm Bob accepts custom VS Code extensions** for Escalation Manager UI — fallbacks: OS notification or local web page | Research / Step 1 |
| ~~Final PermissionRequest field list~~ | ✅ Resolved (2026-11-10): All `tool_input` shapes confirmed from 266 real events. See `tool_input field shapes` in `summarize.py` output. Normalizer already handles all observed shapes. |
| ~~Student model architecture choice~~ | ✅ Resolved: small generative 1–3B, 4-bit quantized; encoder fallback |
| ~~IBM Cloud provisioning~~ | ✅ Resolved: watsonx.ai only (no separate IBM Cloud account); COS comes with project |
| Evaluation thresholds (accuracy/precision/recall) | Teacher pipeline experiments |
| Current watsonx.ai model catalog (verify teacher model availability) | watsonx.ai catalog research |
| Threat model | After rules-only runtime is stable |
| Model I/O spec | After DSPy prompt is validated |

---

## File Layout (Target)

```
Project/
├── hook_client/
│   └── main.py                  # Thin hook client — exits 0 or 2 only
├── supervisor/
│   ├── app.py                   # Supervisor service (FastAPI)
│   ├── audit.py                 # Append-only hash-chained audit log
│   ├── normalizer.py            # Bob payload → PermissionRequest
│   ├── rule_engine.py           # PolicyRuleset matcher
│   ├── policy_engine.py         # Verdict + audit write
│   ├── escalation.py            # Approval prompt + timeout
│   └── context_store.py        # Session prompt context
├── schemas/
│   ├── permission_request.py    # Pydantic: PermissionRequest
│   └── verdict.py               # Pydantic: Verdict
├── policy/
│   ├── policy.yaml              # Decision table + hard rails
│   └── taxonomy.yaml            # Category definitions
├── tests/
│   ├── test_hook_client.py      # Fail-closed cases
│   ├── test_audit.py            # Hash-chain tamper detection
│   ├── test_normalizer.py       # Step 1 log replay
│   ├── test_rule_engine.py      # Hard rail + allow + escalate
│   └── test_escalation.py      # Timeout → deny
└── ai-permission-supervisor-plan.md
```

---

## Diagram Inventory

| Diagram | File | Status |
|---|---|---|
| Bob Workflow Diagram (v3) | AI_Permission_Supervisor_System_Bob_workflow.pdf | ✅ Done |
| Bob Components Diagram | AI_Permission_Supervisor_System_Bob_Components.pdf | ✅ Done |
| ERD | AI_Permission_Supervisor_System_Bob_ERD.pdf | ✅ Done (4 fixes applied 2026-11-10) |
| Data Dictionary / Schema | AI_Permission_Supervisor_System_Bob_DataSchema.pdf | ✅ Done (4 fixes applied 2026-11-10) |
| System Sequence Diagram | Export pending | ✅ Done (in this plan session) |
| Activity Diagram — Runtime | Export pending | ✅ Done (in this plan session) |
| Activity Diagram — Build-time | AI_Permission_Supervisor_System_Bob_BuildTime_Activity.pdf | ✅ Done (2026-11-10, export from artifact) |

### ERD Fixes Required (apply in source tool, re-export PDF)

| # | Entity | Change |
|---|---|---|
| 1 | ERD — new subject area | Add **Build-time subject area** swim lane: `TrainingRecord`, `TeacherLabel`, `DSPyRun`, `ModelVersion`, `IndexVersion`, `TestCase`, `EvaluationRun`. Connect `AuditLogEntry` → `TrainingRecord` (1:N, via COS sync). Connect `ModelVersion` → `Decision.model_version_id` (FK). |
| 2 | `AuditLogEntry` | Add `prev_entry_hash: VARCHAR(64)` and `entry_hash: VARCHAR(64)`. Add note: *entry_hash = sha256(canonical_json(entry_id + written_at + prev_entry_hash + snapshot)) — sorted keys, no whitespace*. |
| 3 | `HumanFeedback` | Add `sent_to_pipeline: BOOLEAN NOT NULL DEFAULT FALSE` and `for_review_only: BOOLEAN NOT NULL DEFAULT FALSE`. Constraint: *for_review_only=TRUE implies sent_to_pipeline=FALSE*. |
| 4 | `Decision` | Add `model_version_id: FK → ModelVersion` (nullable), `index_version_id: FK → IndexVersion` (nullable), `confidence: FLOAT` (nullable, 0.0–1.0). |

### Data Schema Fixes Required (same 4 items, apply in source tool, re-export PDF)

| # | Section | Change |
|---|---|---|
| 1 | New section | Add **Build-Time Entities** section (lives on IBM Cloud): `TrainingRecord`, `TeacherLabel`, `ModelVersion`, `IndexVersion`, `EvaluationRun` with full data dictionaries. |
| 2 | `AuditLogEntry` | Add rows for `prev_entry_hash` and `entry_hash` (VARCHAR 64, SHA-256 hex). Add serialization note: canonical JSON, keys sorted, no whitespace, covers entry_id + written_at + prev_entry_hash + snapshot. |
| 3 | `HumanFeedback` | Add rows for `sent_to_pipeline` (BOOLEAN, default FALSE) and `for_review_only` (BOOLEAN, default FALSE). Add business rule: for_review_only=TRUE implies sent_to_pipeline=FALSE. |
| 4 | `Decision` | Add rows for `model_version_id` (FK, nullable), `index_version_id` (FK, nullable), `confidence` (FLOAT 0.0–1.0, nullable — null for rule-engine decisions). |
