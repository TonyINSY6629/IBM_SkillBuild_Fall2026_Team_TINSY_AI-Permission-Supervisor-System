# Supervisor ⇄ pop-up wiring (drop-in update)

Copy `supervisor/*.py` and `tests/test_popup_endpoints.py` over the same files in your project.

## What changed
- `GET /escalation/{id}/data` — JSON payload for the pop-up (tool, category, reason,
  targets, workspace/secrets flags, prompt, tool_input, label, policy/rule/model ids,
  `timeout`, `remaining`). Returns 410 once resolved or timed out.
- `POST /escalation/{id}/allow|deny` accept an optional `{"note": "..."}` body.
- On escalation the supervisor opens `ESCALATION_POPUP_URL/?id=<id>&api=<SUPERVISOR_URL>`
  in the default browser (fails safe: if the browser can't open, the timer still auto-denies).
- CORS for http://localhost:8080 and *.lovable.app (+ `ESCALATION_POPUP_ORIGINS`),
  including Chrome's private-network permission so a published https pop-up can reach 127.0.0.1.
- `policy_engine.py` passes the full request + verdict details to the pop-up.
- The old HTML page at `GET /escalation/{id}` is kept as a fallback (set `ESCALATION_POPUP_URL=""`).

## Env vars
| var | default |
|---|---|
| ESCALATION_POPUP_URL | http://localhost:8080 (run `bun run dev` in UI/escalation-popup-src) — or your published Lovable URL |
| ESCALATION_OPEN_BROWSER | 1 |
| ESCALATION_POPUP_ORIGINS | (comma list of extra allowed origins) |

## Fail-closed behaviour in the pop-up
If the pop-up can't load the request, or the supervisor doesn't confirm an Allow/Deny
(`ok: true`), it shows "Blocked for safety". The supervisor itself still denies on timeout.

---

# Item 3 — PreToolUse vs Bob's own approval prompt: test protocol

Goal: prove whether the supervisor decides **before** or **after** Bob shows its own
"Allow this tool?" prompt. This can only be run on a machine with Bob IDE.

1. Turn on Bob's per-tool approval for `execute_command` (default setting) and
   start the supervisor with `ESCALATION_TIMEOUT_SECONDS=120`.
2. Add a timestamp line to `hook_client/main.py` at the very top of `main()`:
   `print(f"[hook-start] {time.time():.3f}", file=sys.stderr)` (also log it to a file,
   e.g. `logs/hook_timing.log`, because Bob may hide stderr).
3. In Bob, ask: *"Delete the folder ../shared/cache"* (escalates: outside workspace).
4. Record, with a screen recording running:
   - T1 = when Bob's own approval prompt appears
   - T2 = `[hook-start]` timestamp
   - T3 = when the supervisor pop-up opens
5. Run three variants: (a) answer Bob's prompt **Deny** — does the hook ever run?
   (b) leave Bob's prompt unanswered for 30 s — does the hook run meanwhile?
   (c) answer **Allow** — does the hook run only after that?

Interpretation
- Hook fires only after Bob's prompt is answered (T2 > T1, never runs on Deny):
  Bob's prompt is a first gate; the supervisor is a second gate. Safety still holds
  (the supervisor can still block), but the user sees two prompts. Fix for the demo:
  set Bob's auto-approve for tools so the supervisor is the single gate, and document it.
- Hook fires before/while Bob's prompt shows (T2 < T1): ordering is fine; Bob's
  prompt is just displayed early. Document as "cosmetic".
- Hook does **not** fire at all for some tools: real gap — those tools bypass the
  supervisor. Must be listed in the open items before the demo.

Write the result (timestamps + recording) into the CHANGES file and the plan's Open Items.
