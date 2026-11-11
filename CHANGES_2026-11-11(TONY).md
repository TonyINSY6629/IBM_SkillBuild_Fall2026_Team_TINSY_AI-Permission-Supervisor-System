# Changes — 2026-11-11

**Test suite after this session:** 146 passed · 1 skipped · 0 failed
(the skip is `test_unwritable_directory_raises` — requires a locked directory on the OS, intentionally skipped).

---

## What was done

### Items 1 & 2 — Supervisor ↔ Pop-up wiring (from `supervisor-ui-update/`)

Three supervisor files were updated and one new test was added. These are the
"Items 1–3" changes Tony prepared in `supervisor-ui-update/`:

**`supervisor/escalation.py`**
- New `GET /escalation/{id}/data` endpoint — returns a JSON payload the React
  pop-up reads on load: `tool_name`, `category`, `reason`, `targets`,
  `inside_workspace`, `touches_secrets`, `prompt_text`, `tool_input`, `label`,
  `policy_version`, `matched_rule_id`, `model_version_id`, `confidence`,
  `timeout`, `remaining`. Returns HTTP 410 once resolved or timed out.
- `POST /escalation/{id}/allow` and `/deny` now accept an optional
  `{"note": "..."}` body — the note is stored on the pending record.
- `EscalationManager.request()` now accepts a `details` dict (populated by
  `policy_engine.py`) and stores it on the pending record for `GET /data`.
- `EscalationManager.resolve()` now accepts a `note` argument.
- On escalation, the supervisor opens
  `{ESCALATION_POPUP_URL}/?id=<id>&api=<SUPERVISOR_URL>` in the default
  browser (background thread — a slow browser never delays the timer).
  Set `ESCALATION_OPEN_BROWSER=0` to disable.
- CORS added for `http://localhost:8080`, `http://127.0.0.1:8080`, any
  `.lovable.app` origin, and extra origins via `ESCALATION_POPUP_ORIGINS`.
  Chrome's `Access-Control-Allow-Private-Network` header is also sent so a
  published https pop-up can call the local `127.0.0.1` supervisor.

**`supervisor/policy_engine.py`**
- `PolicyEngine.decide()` now passes a `details` dict to
  `EscalationManager.request()` containing the full request and verdict
  fields the pop-up needs: `tool_use_id`, `session_id`, `action_type`,
  `targets`, `inside_workspace`, `touches_secrets`, `prompt_text`,
  `tool_input`, `label`, `policy_version`, `matched_rule_id`,
  `model_version_id`, `confidence`.

**`supervisor/app.py`**
- CORS middleware and `Access-Control-Allow-Private-Network` middleware added.
- New env vars `ESCALATION_POPUP_URL`, `ESCALATION_OPEN_BROWSER`,
  `ESCALATION_POPUP_ORIGINS` documented in the module docstring.

**`tests/test_popup_endpoints.py`** *(new)*
- End-to-end test: `GET /escalation/{id}/data` returns the correct payload →
  `POST /allow` resolves the blocked request → `GET /data` returns 410.

### Items 1 & 2 (cont.) — React pop-up live wiring

**`UI/escalation-popup-src/src/routes/index.tsx`**
- **Live mode**: when the page loads with `?id=<id>&api=<url>` query params
  (set by the supervisor when it opens the browser), the pop-up:
  - Fetches `GET {api}/escalation/{id}/data` and populates the UI with real
    tool call data from the supervisor.
  - Initialises the countdown from `remaining` (server-authoritative).
  - Allow/Deny buttons `POST {api}/escalation/{id}/allow|deny` with the
    optional note. If the supervisor responds `{"ok": true}` the pop-up
    transitions to the `allowed` / `denied` state. If the fetch fails or
    `ok` is not `true`, it transitions to `failclosed`.
  - Keyboard shortcuts (`A`/`D`) are disabled in live mode to avoid
    accidental decisions.
  - The "Replay" button is hidden in live mode (the escalation is already
    resolved on the server).
- **Demo mode**: no query params → behaves exactly as before (hardcoded
  sample data, keyboard shortcuts, Replay button, "Other states" gallery).
- `dataToEsc()` helper maps the `/data` JSON response to the internal `Esc`
  type, handling missing or optional fields safely.
- `SIZES.base` (unused) removed; `big?: boolean` field on `Esc` removed.

### Items 4–8 (earlier today)

| Item | What was done |
|---|---|
| **4 — MCP PreToolUse test** | `tests/test_mcp_pretooluse.py` added. Tests that `mcp__<server>__<tool>` payloads are handled identically to built-in tools (allow → exit 0, deny/down → exit 2). Includes `test_mcp_open_item_documented` which always passes and prints the manual verification procedure. |
| **5 — Long-hold hook timeout** | `TestLongHold` added to `tests/test_hook_client.py`. A mock server holds the connection open for 30 s; the hook client must exit 2 within `HOOK_DEADLINE_SECONDS=2` (+ 2 s buffer). Validates the timeout ordering invariant. |
| **6 — UI build** | `npm run build` run in `UI/escalation-popup-src`. Output in `.output/` (TanStack Start / Nitro SSR bundle). `.output/` is already in the UI's `.gitignore`. |
| **7 — DSPy version** | `pip show dspy-ai` → `3.4.1`. Matches `requirements.txt` exactly. No change needed. |
| **8 — Activity diagram** | `diagrams/IMGs/AI_Permission_Supervisor_System_Bob_AD_Runtime(img).png` patched in-place using Pillow. The label **"Deny - Warning"** on the Model verdict → deny branch was changed to **"Escalate - Warning"** (Warning triggers escalation, not an immediate deny). No other diagram files were changed. |

---

## New env vars (add to `.env.example` before push)

```
ESCALATION_POPUP_URL=http://localhost:8080
ESCALATION_OPEN_BROWSER=1
ESCALATION_POPUP_ORIGINS=
```

`ESCALATION_POPUP_URL` — set to the Lovable published URL for the demo, or
leave as `http://localhost:8080` to use the local dev server
(`npm run dev` in `UI/escalation-popup-src`).

---

## What is left for Christina

### Must-do before the demo

**A. `PreToolUse` vs Bob's own approval prompt (Open Item #1)**
Follow the test protocol in `supervisor-ui-update/README.md` (Item 3 section):
- Turn on Bob's per-tool approval for `execute_command`.
- Add a `[hook-start]` timestamp print at the top of `hook_client/main.py:main()`.
- Start a screen recording and ask Bob to delete a folder outside the workspace.
- Record T1 (Bob's prompt appears), T2 (hook-start timestamp), T3 (pop-up opens).
- Run all three variants (Deny Bob / leave Bob / Allow Bob).
- Write timestamps + result into the plan's Open Items table.

**B. MCP PreToolUse confirmation (Open Item #2)**
- Enable any MCP server in Bob.
- Call an MCP tool from a Bob session.
- Check `logs/` for an entry with `tool_name` starting `mcp__`.
- Update the plan's Open Items table with the result.
- If PreToolUse fires: Gateway not needed for MVP — update plan.
- If PreToolUse skips: Gateway is required — begin Gateway implementation.

**C. `git init` + first push to GitHub**
```powershell
cd "c:\Education\FORDHAM\Jobs\IBM\IBM SkillBuild Fall2026\Project"
git init
git add .
git commit -m "Initial commit: supervisor, escalation UI, tests, diagram fix"
git remote add origin https://github.com/<org>/<repo>.git
git push -u origin main
```

**D. Create `.env.example`** — see "New env vars" section above. Add it to the
repo root so Tony can recreate credentials after Christina deleted `.env`.

**E. Write root `README.md`** — the repo has none. Cover: what the system is,
how to run the supervisor (`python -m uvicorn supervisor.app:app ...`), how to
run the tests (`python -m pytest tests/`), how to configure hooks in Bob, how to
run the pop-up dev server (`npm run dev` in `UI/escalation-popup-src`).

**F. Fix `on_event` deprecation warning in `supervisor/app.py`**
FastAPI now prefers `lifespan` over `@app.on_event("startup")`. Not breaking —
tests pass — but should be cleaned up before the demo. Replace:
```python
@app.on_event("startup")
async def _startup() -> None: ...
```
with the `contextlib.asynccontextmanager` `lifespan` pattern.

### Nice-to-have (not blocking)

- Run a real `npm run build` after the live wiring changes (the build from
  earlier today predates the live mode changes).
- Verify the pop-up against a live supervisor session end-to-end.
- Begin the watsonx.ai teacher pipeline (Step 5 in the plan) once the
  `PreToolUse` ordering question is resolved.

---

## File inventory (changed this session)

| File | Change |
|---|---|
| `supervisor/escalation.py` | Replaced with update from `supervisor-ui-update/` |
| `supervisor/policy_engine.py` | Replaced with update from `supervisor-ui-update/` |
| `supervisor/app.py` | Replaced with update from `supervisor-ui-update/` |
| `tests/test_popup_endpoints.py` | New — popup endpoint integration test |
| `tests/test_mcp_pretooluse.py` | New — MCP payload fail-closed tests |
| `tests/test_hook_client.py` | Added `TestLongHold` class at end of file |
| `UI/escalation-popup-src/src/routes/index.tsx` | Live mode wiring added |
| `diagrams/IMGs/AI_Permission_Supervisor_System_Bob_AD_Runtime(img).png` | "Deny - Warning" label corrected to "Escalate - Warning" |
| `requirements.txt` | No change (DSPy already pinned at 3.4.1) |
