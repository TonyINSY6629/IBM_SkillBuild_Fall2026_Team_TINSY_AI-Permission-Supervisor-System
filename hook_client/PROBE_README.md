# Step 1 — Probe Hook: Install & Run

## What this does

Three files in `hook_client/`:

| File | Purpose |
|---|---|
| `probe.py` | Hook script — logs every payload to `~/supervisor_probe/probe_log.jsonl`, exits 0 |
| `probe_settings.json` | Hook configuration to merge into `~/.bob/settings/settings.json` |
| `summarize.py` | Reads the log and prints a summary of what was observed |

---

## Install (takes ~5 minutes)

### 1. Copy the probe script to your home directory

```powershell
# Windows (PowerShell)
New-Item -ItemType Directory -Force "$env:USERPROFILE\supervisor_probe"
Copy-Item "hook_client\probe.py" "$env:USERPROFILE\supervisor_probe\probe.py"
Copy-Item "hook_client\summarize.py" "$env:USERPROFILE\supervisor_probe\summarize.py"
```

```bash
# macOS / Linux
mkdir -p ~/supervisor_probe
cp hook_client/probe.py ~/supervisor_probe/probe.py
cp hook_client/summarize.py ~/supervisor_probe/summarize.py
```

### 2. Merge hooks into Bob's global settings

Open `~/.bob/settings/settings.json` (create it if it doesn't exist).

If the file is empty or does not exist, paste the entire contents of
`hook_client/probe_settings.json` as the file content.

If it already has content, merge the `"hooks"` key from `probe_settings.json`
into the existing file. Do **not** overwrite existing hooks — add to them.

> **Important:** Use the **global** settings file `~/.bob/settings/settings.json`,
> not a workspace `.bob/settings.json`. Global hooks cannot be removed by the agent.

**Windows path:** `%USERPROFILE%\.bob\settings\settings.json`
**macOS/Linux path:** `~/.bob/settings/settings.json`

### 3. Verify Python is available

```bash
python3 --version   # must be 3.8+
```

If `python3` is not on PATH on Windows, replace `python3` with the full path
to your Python executable in `probe_settings.json` (e.g. `C:/Python311/python.exe`).

---

## Run

Use Bob normally for **at least one full work session** (ideally one day).
The probe logs silently in the background.

The log is written to:
- **Windows:** `%USERPROFILE%\supervisor_probe\probe_log.jsonl`
- **macOS/Linux:** `~/supervisor_probe/probe_log.jsonl`

---

## Summarize results

```bash
python3 ~/supervisor_probe/summarize.py
```

This prints:
- How many times each hook event fired
- Every `tool_name` seen in `PreToolUse`
- Tools that appeared in `PreToolUse` but not `PostToolUse` (may be blocked/cancelled)
- `tool_input` field shapes per tool (grounds the Normalizer)
- Possible MCP tool calls (tool names not in the standard Bob tool list)

---

## What to check manually in probe_log.jsonl

After running `summarize.py`, open `probe_log.jsonl` and check:

1. **Does `PreToolUse` fire before Bob's own approval prompt?**
   Look at timestamps — does `PreToolUse` always precede user interaction?

2. **Does the hook hold open > 10s without Bob misbehaving?**
   Edit `probe_settings.json`, set `"timeout": 15` on `PreToolUse`, restart Bob,
   and add a `time.sleep(12)` to `probe.py` temporarily. Does Bob wait, or does it
   proceed without the hook result?

3. **Does `exit 2` stderr reason reach the model?**
   Temporarily change `probe.py` to `sys.exit(2)` with a print to stderr.
   Check whether the model's next message references the block reason.

4. **Does `PreToolUse` fire for MCP tool calls?**
   Install any MCP server, trigger it, and check whether a `PreToolUse` entry
   appears in the log with that tool's name.

---

## Remove the probe

Once data is collected, remove the `"hooks"` entries from `~/.bob/settings/settings.json`
and delete `~/supervisor_probe/probe.py`.

Keep `probe_log.jsonl` — it is the first dataset for the Normalizer and Supervisor.
