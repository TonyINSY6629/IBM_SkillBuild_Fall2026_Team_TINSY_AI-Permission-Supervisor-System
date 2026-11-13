# Bug fixes — 2026-11-12 (Christina)

Bugs found by two separate reviews (Claude + Bob), checked against the code, fixed one per commit.
Every code change is marked with `NEW` comments (`# NEW` in Python, `// NEW` in TypeScript,
`<!-- NEW -->` in Markdown; JSON has no comments, so Bug 6 is noted in its `_comment` list).

**Test results after all fixes:** `py -3.13 -m pytest tests -q` → 144 passed, 3 skipped.
The pop-up builds (`npm run build`) and was checked in the browser (size, new screen, no console errors).

| Bug | File(s) | What was wrong | Fix |
|---|---|---|---|
| 1 | `UI/escalation-popup-src/src/routes/index.tsx` | A request that already ended (even a timeout = deny) showed **"Allowed"** with fake sample data | New "Already decided" screen; shows the real escalation id; also added to the demo gallery |
| 2 | `index.tsx` | Pop-up was ~61×34 px instead of 607×341 (extra `/ 10`) | Removed the extra `/ 10` |
| 3 | `supervisor/escalation.py`, `supervisor/policy_engine.py` | The human's note from the pop-up was never saved | `EscalationResult` now carries `note`; it is written to the audit log |
| 4 | `supervisor/escalation.py` | Two answers to one escalation both got "ok"; the last one won | Check + save inside the lock; only the first answer counts; supervisor reads the result under the same lock |
| 5 | `index.tsx` | Server rendered the demo page, browser rendered the live page (hydration error); `useState` after an early return | `ssr: false` on the route; `useState` moved above the return |
| 6 | `hook_client/supervisor_settings.json` | `python3` may not exist on Windows; a hook that fails to start does not block | Commands use `py -3` |
| 7 | `supervisor/escalation.py` | Backup HTML page inserted text without escaping | `html.escape()` on tool name, category, reason, id |
| 8 | `supervisor/escalation.py` | If the React pop-up isn't running, there was no way to approve | Terminal message also prints the built-in backup page link |
| 9 | `supervisor/app.py` | CORS trusted every `*.lovable.app` site | Wildcard removed. **Tony: set `ESCALATION_POPUP_ORIGINS` to the published pop-up address if you use it** |
       | 9b | `supervisor/app.py` | Pop-up blocked if `npm run dev` uses a port other than 8080 | CORS accepts any `localhost` / `127.0.0.1` port |
| 11 | `supervisor/escalation.py` | Backup page countdown used the global timeout | Uses the request's own `pending.timeout` |
| 12 | `supervisor/app.py` | After a policy reload, the engine was rebuilt on every request | Rebuilt engine is saved (`global _policy_engine`) |
| 13 | `supervisor-ui-update/` | Duplicate copy of 3 supervisor files + 1 test | Folder removed; its README kept as `UI/POPUP_SETUP_NOTES.md` |
| 14 | `tests/test_mcp_pretooluse.py` | Open-item test was `assert True` (always "passed") | Now `pytest.skip(...)` |
| 15 | `ai-permission-supervisor-plan.md` | Line 146 still said the stderr reason was "unconfirmed" | Says "confirmed in Step 4 testing" |
| 17 | `tests/conftest.py` (new) | Running the tests opened Chrome tabs | Sets `ESCALATION_OPEN_BROWSER=0` before any test |
| 18 | `requirements.txt` | Newer Starlette's test client needs `httpx2` | Added `httpx2` |

   ## Decisions (Nov 13, with Tony)
   - **Bug 10:** keep the repo public, including `logs/audit_log.jsonl` (no keys in it).
   - **Bug 9:** the pop-up runs locally only (`npm run dev`). CORS allows any `localhost` / `127.0.0.1` port; the published Lovable site is not used.
   - **Bug 16:** skipped — the manual browser check is enough for the demo.
   - Still open (not bugs): does `PreToolUse` fire before Bob's own approval prompt, and for MCP tools? Steps in `UI/POPUP_SETUP_NOTES.md`.

## Notes for running on Windows
- Run the tests with `py -3.13 -m pytest tests -q` (needs `pip install -r requirements.txt`).
- `npm install` / `npm run build` change `package-lock.json` and `src/routeTree.gen.ts`; don't commit those (`git restore` them).