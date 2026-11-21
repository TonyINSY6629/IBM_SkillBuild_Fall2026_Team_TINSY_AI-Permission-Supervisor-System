// AI Permission Supervisor - Bob extension.
//
// This extension does NOT make any allow/deny decisions. It only installs,
// starts, stops and shows the status of the separate supervisor service.
// The real checks stay in that service (reached through Bob's PreToolUse
// hook), so the agent cannot switch them off from inside the editor.

const vscode = require("vscode");
const http = require("http");
const os = require("os");
const path = require("path");

const PACKAGE_NAME = "ai-permission-supervisor"; // name on PyPI
const HEALTH_URL = "http://127.0.0.1:9001/health";
const CHECK_EVERY_MS = 15000;

let statusItem;
let timer;

function python() {
  // Uses the "Python command" setting if set; otherwise the "py" launcher on
  // Windows and python3 on macOS/Linux.
  const custom = vscode.workspace
    .getConfiguration("aiPermissionSupervisor")
    .get("pythonCommand", "")
    .trim();
  if (custom) {
    return custom;
  }
  return process.platform === "win32" ? "py -3" : "python3";
}

function cli(args) {
  return `${python()} -m ai_permission_supervisor.cli ${args}`;
}

// Commands run in a visible terminal so the user sees every step and any error.
function runInTerminal(name, lines) {
  const terminal = vscode.window.createTerminal({ name });
  terminal.show();
  for (const line of lines) {
    terminal.sendText(line);
  }
}

// Any HTTP answer means the service is up; a connection error means it is not.
function isRunning() {
  return new Promise((resolve) => {
    const req = http.get(HEALTH_URL, { timeout: 3000 }, (res) => {
      res.resume();
      resolve(true);
    });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(false));
  });
}

async function refreshStatus() {
  const running = await isRunning();
  statusItem.text = running ? "$(shield) APS: running" : "$(shield) APS: stopped";
  statusItem.tooltip = running
    ? "AI Permission Supervisor is checking every tool call. Click for details."
    : "AI Permission Supervisor is not running. Click to check status.";
  statusItem.backgroundColor = running
    ? undefined
    : new vscode.ThemeColor("statusBarItem.warningBackground");
  return running;
}

async function setup() {
  const choice = await vscode.window.showQuickPick(
    [
      {
        label: "Yes, also turn on Bob's auto-approve (recommended)",
        description: "You see one prompt (the supervisor's) instead of two",
        flag: " --bob-auto-approve",
      },
      { label: "No, keep Bob's own prompts", description: "", flag: "" },
    ],
    { placeHolder: "Turn on Bob's auto-approve so the supervisor is the only checkpoint?" }
  );
  if (!choice) {
    return;
  }
  runInTerminal("AI Permission Supervisor: Set up", [
    `${python()} -m pip install --upgrade ${PACKAGE_NAME}`,
    cli(`install${choice.flag}`),
    cli("start"),
    cli("status"),
  ]);
  vscode.window.showInformationMessage(
    "Setting up AI Permission Supervisor in the terminal. Restart Bob when it finishes so the new hooks load."
  );
  setTimeout(refreshStatus, 20000);
}

async function uninstall() {
  const answer = await vscode.window.showWarningMessage(
    "Remove the AI Permission Supervisor hooks from Bob and stop the service? Your audit log is kept.",
    { modal: true },
    "Uninstall"
  );
  if (answer !== "Uninstall") {
    return;
  }
  runInTerminal("AI Permission Supervisor: Uninstall", [cli("stop"), cli("uninstall")]);
  setTimeout(refreshStatus, 5000);
}

async function openAuditLog() {
  const file = path.join(os.homedir(), "supervisor_audit", "audit_log.jsonl");
  try {
    const doc = await vscode.workspace.openTextDocument(file);
    await vscode.window.showTextDocument(doc, { preview: true });
  } catch (err) {
    vscode.window.showWarningMessage(`No audit log found yet at ${file}`);
  }
}

async function activate(context) {
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusItem.command = "aps.status";
  statusItem.show();

  context.subscriptions.push(
    statusItem,
    vscode.commands.registerCommand("aps.setup", setup),
    vscode.commands.registerCommand("aps.start", () => {
      runInTerminal("AI Permission Supervisor", [cli("start")]);
      setTimeout(refreshStatus, 5000);
    }),
    vscode.commands.registerCommand("aps.stop", () => {
      runInTerminal("AI Permission Supervisor", [cli("stop")]);
      setTimeout(refreshStatus, 3000);
    }),
    vscode.commands.registerCommand("aps.status", () => {
      runInTerminal("AI Permission Supervisor", [cli("doctor")]);
      refreshStatus();
    }),
    vscode.commands.registerCommand("aps.auditLog", openAuditLog),
    vscode.commands.registerCommand("aps.uninstall", uninstall)
  );

  timer = setInterval(refreshStatus, CHECK_EVERY_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  const running = await refreshStatus();
  const askedBefore = context.globalState.get("aps.askedSetup", false);
  if (!running && !askedBefore) {
    const pick = await vscode.window.showInformationMessage(
      "AI Permission Supervisor is not running. Set it up now?",
      "Set up",
      "Not now"
    );
    await context.globalState.update("aps.askedSetup", true);
    if (pick === "Set up") {
      await setup();
    }
  }
}

function deactivate() {
  if (timer) {
    clearInterval(timer);
  }
}

module.exports = { activate, deactivate };