# AI Permission Supervisor

A local guardrail for AI coding agents. It intercepts every tool call the
IBM Bob agent attempts (file reads and writes, shell commands, and so on),
classifies it, and then **allows** it, **denies** it with a reason the agent
can read, or **escalates** it to you for approval, before the tool runs.

Built by Team TINSY for IBM SkillsBuild, Fall 2026.

> **Status: v0.1 (beta), rules only.** Decisions come from the policy in
> `policy/policy.yaml`. Anything the rules do not cover is escalated to you.
> The distilled student model that will judge ambiguous cases automatically
> is planned for a later release (see `ai-permission-supervisor-plan.md`).

## How it works

```
Bob agent ──tool call──> PreToolUse hook ──> Hook Client ──> Supervisor service (local)
                                               │               ├─ Normalizer
                         exit 0 = allow  <─────┤               ├─ Rule Engine (policy.yaml)
                         exit 2 = deny   <─────┘               ├─ Escalation Manager ──> you
                                                               └─ Audit Log (hash-chained)
```

- **Fail-closed.** If the Supervisor is not running, errors, or the approval
  times out, the tool call is denied. Nothing runs unrecorded.
- **Hard rails** (always denied): writes outside the workspace, writes to
  secret files such as `.env` or keys, any access to the Supervisor's own files,
  Bob's settings, or the Supervisor's HTTP API.
- **Auto-allowed:** reading and writing ordinary files inside the workspace.
- **Escalated to you:** everything else, such as shell commands and network
  access. An approval page opens in your browser; no answer within 120 s
  means deny.
- **Audit log:** every decision is written to `~/supervisor_audit/audit_log.jsonl`
  before the verdict is returned, as an append-only hash chain.

## Requirements

- IBM Bob (IDE or Bob Shell)
- Python 3.10 or newer
- Windows, macOS, or Linux

## Install

```bash
git clone https://github.com/TonyINSY6629/IBM_SkillBuild_Fall2026_Team_TINSY_AI-Permission-Supervisor-System.git
cd IBM_SkillBuild_Fall2026_Team_TINSY_AI-Permission-Supervisor-System
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e .

aps install              # register the hooks in Bob's global settings
aps autostart enable     # start the Supervisor at every login (recommended)
aps start                # start it now
aps doctor               # check that everything is set up correctly
```

Then restart Bob.

`aps install` adds its hooks to `~/.bob/settings/settings.json` without
touching your other settings, and saves a backup of the previous file in
`~/.ai_permission_supervisor/backups/`. The hooks are registered globally on
purpose: workspace settings live inside the agent's workspace, where the agent
could edit them.

> **Important:** once the hooks are installed, Bob's tool calls are denied
> whenever the Supervisor is not running. Keep it running (`aps autostart enable`)
> or remove the hooks with `aps uninstall`.

## Commands

| Command | What it does |
|---|---|
| `aps install [--dry-run] [--remove-probe]` | Add the Supervisor hooks to Bob's global settings |
| `aps uninstall [--remove-probe]` | Remove them; other settings stay as they are |
| `aps start` / `aps stop` | Start or stop the service in the background |
| `aps status` | Is the service running, are the hooks installed |
| `aps doctor` | Check packages, policy, hooks, timeout ordering, and service health |
| `aps autostart enable` / `disable` | Start at login: Windows Startup folder, macOS LaunchAgent, or systemd user service |

The service log is `~/.ai_permission_supervisor/supervisor.log`.

## Uninstall

```bash
aps autostart disable
aps stop
aps uninstall
pip uninstall ai-permission-supervisor
```

Restart Bob afterwards.

## Configuration

Set these as environment variables, or in a `.env` file in the repository
root (read by the Supervisor service, not by the Hook Client).

| Variable | Default | Meaning |
|---|---|---|
| `SUPERVISOR_HOST` / `SUPERVISOR_PORT` | `127.0.0.1` / `9001` | Where the service listens |
| `ESCALATION_TIMEOUT_SECONDS` | `120` | How long to wait for your approval |
| `HOOK_DEADLINE_SECONDS` | `130` | Hook Client deadline (environment only) |
| `ESCALATION_OPEN_BROWSER` | `1` | Open the approval page automatically |
| `ESCALATION_POPUP_URL` | built-in page | URL of the React pop-up (`UI/escalation-popup-src`), for development |
| `AUDIT_LOG_PATH` | `~/supervisor_audit/audit_log.jsonl` | Audit log location |
| `POLICY_DIR` | `policy/` | Folder with `policy.yaml` and `taxonomy.yaml` |
| `COS_API_KEY`, `COS_INSTANCE_ID`, `COS_AUDIT_BUCKET` | unset | Optional audit-log sync to IBM Cloud Object Storage (`pip install -e ".[cloud]"`) |

The timeouts must keep this order, or a slow approval would let the tool run:
**escalation timeout < Hook Client deadline < Bob hook timeout (150 s)**.
`aps doctor` checks it.

To change what is allowed or denied, edit `policy/policy.yaml`. The service
reloads it every 5 minutes.

## Known limitations

- **Double prompts.** In testing, Bob's own approval prompt appeared before the
  Supervisor's. To use the Supervisor as the single gate, turn on Bob's
  auto-approve for the tool groups you want it to handle.
- **Same user account.** The Supervisor runs as your user, like the agent. The
  hard rails block the direct routes to disable it, but this is not isolation
  in the operating-system sense.
- **Hook Client crash fails open.** If the Hook Client process itself crashes
  (not just errors), Bob runs the tool. It is kept small, standard-library only,
  and catches every exception to make this unlikely.
- **MCP tools.** It is not yet confirmed that Bob's `PreToolUse` hook fires for
  MCP tool calls.
- **Linux autostart** runs as a systemd user service, which may not be able to
  open your browser. Use the approval URL from the log if no page appears.

## Development

```bash
pip install -e ".[dev]"            # tests
pip install -e ".[teacher,cloud]"  # teacher pipeline (DSPy + watsonx.ai)
python -m pytest
```

Design documents: `ai-permission-supervisor-plan.md` and `diagrams/`.

## License

Not yet chosen. Until a license is added, all rights are reserved by the authors.
