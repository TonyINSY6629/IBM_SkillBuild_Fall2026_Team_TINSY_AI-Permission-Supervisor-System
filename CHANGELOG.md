# Changelog

## 0.1.0 (beta)

First installable release of the AI Permission Supervisor for IBM Bob.

- One-command install: `pip install ai-permission-supervisor`, then `aps install`.
- `aps` command: install and uninstall the Bob hooks, start, stop, status,
  doctor, autostart at login (Windows, macOS, Linux), and an editable policy.
- `aps install --bob-auto-approve` makes the Supervisor the single approval
  gate by adding everyday command prefixes (`git`, `python`, `npm`, …) to
  Bob's allowed list, since Bob matches by prefix and ignores `*`;
  `aps uninstall` reverts exactly what it added.
- CLI hints show a command that works on your machine (`python -m
  ai_permission_supervisor.cli …`) when `aps` is not on your PATH.
- Rules-only decisions: hard rails deny, allow rules pass silently, everything
  else is escalated to a human through an approval page.
- Read-only shell commands (`git status`, `git diff`, `python --version`,
  `dir`, `ls`, …) are allowed without a prompt (policy rule al-003). Commands
  that chain, redirect, or substitute are never auto-allowed.
- The Supervisor protects itself: its installed files, the active policy, its
  state folder, Bob's settings, and its local HTTP API are off-limits to the
  agent.
- Fail-closed throughout, with a hash-chained audit log written before every
  decision and optional sync to IBM Cloud Object Storage.
- Fixed: on Windows the approval page could open the React pop-up's address
  even when it was not running.
