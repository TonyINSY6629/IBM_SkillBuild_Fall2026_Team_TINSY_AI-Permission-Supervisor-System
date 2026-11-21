# AI Permission Supervisor

A local guardrail for AI coding agents such as IBM Bob. The supervisor checks
every tool call the agent makes **before it runs**: routine actions are allowed
instantly, dangerous ones are blocked by hard rails, and unclear ones are sent
to you with a risk label and a reason. Every decision is written to a
hash-chained audit log.

This extension makes the supervisor easy to set up and monitor. It does not make
decisions itself: the checks run in a separate local service, so the agent
cannot switch them off from inside the editor.

## Requirements

- Python 3.10 or newer (on Windows, the `py` launcher)
- IBM Bob

## Getting started

1. Install this extension.
2. When asked "Set it up now?", click **Set up** (or run
   **AI Permission Supervisor: Set up** from the Command Palette).
3. The terminal installs the Python package, adds the hooks to Bob and starts
   the service.
4. Restart Bob. The status bar shows **APS: running**.

## Commands

| Command | What it does |
|---|---|
| AI Permission Supervisor: Set up | pip install, add the Bob hooks, start the service |
| AI Permission Supervisor: Start / Stop | Start or stop the service |
| AI Permission Supervisor: Check status | Run the full health check |
| AI Permission Supervisor: Open audit log | Open the decision log |
| AI Permission Supervisor: Uninstall from Bob | Stop the service and remove the hooks |

## More information

Source code, documentation and the policy rules:
https://github.com/TonyINSY6629/IBM_SkillBuild_Fall2026_Team_TINSY_AI-Permission-Supervisor-System

Built by Team TINSY (Tony Wang, Christina Lopez) for IBM SkillsBuild, Fall 2026.