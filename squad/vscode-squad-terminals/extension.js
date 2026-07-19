// Squad Terminals — one PLAIN terminal per roster agent, auto-opened, plus
// per-agent squad actions on the terminal tab context menu.
//
// Why not tasks / names / shellPath (all learned the hard way 2026-07-19):
//   - task terminals pin their tab identity and never render live titles
//   - terminals created with a fixed `name:` never rename from OSC titles
//   - `shellPath:` spawns skip VSCode's shell-integration bootstrap and
//     their tabs don't track titles either
// Working formula: unnamed default-profile bash + sendText the attach.
// Identity/status live in the title itself, pushed by `squad who --watch`:
//   "⚡ dt · ✳ <status>"  (🔴 after the glyph when waiting on the operator)
//
// Roster: ~/.config/squad/squad.conf — `squad add|rm` and the terminal list
// follows on next window load.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const SQUAD = path.join(os.homedir(), ".local", "bin", "squad");

// repo-key -> [codicon, terminal color id]; fallback below for unknown repos
const THEME = {
  "dreamteam":          ["rocket",    "terminal.ansiGreen"],
  "mcp-hub":            ["broadcast", "terminal.ansiCyan"],
  "vps-hetzner":        ["server",    "terminal.ansiRed"],
  "features-json":      ["json",      "terminal.ansiYellow"],
  "reliable-ai":        ["shield",    "terminal.ansiBlue"],
  "factory-data-model": ["database",  "terminal.ansiMagenta"],
  "factory-operations": ["gear",      "terminal.ansiWhite"],
};
const FALLBACK = ["terminal", "terminal.ansiBrightBlack"];

// Terminal -> agent name, for context-menu target resolution
const agentOf = new Map();

function rosterAgents() {
  const conf = path.join(os.homedir(), ".config", "squad", "squad.conf");
  try {
    return fs
      .readFileSync(conf, "utf8")
      .split("\n")
      .filter((l) => l.trim() && !l.trim().startsWith("#"))
      .map((l) => l.split("|")[0].trim())
      .filter(Boolean);
  } catch {
    return [];
  }
}

// short label: strip the derived "-<hostname>" suffix (sanitized like cli.py)
function shortLabel(agent) {
  const host = os.hostname().toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  return agent.endsWith("-" + host) ? agent.slice(0, -(host.length + 1)) : agent;
}

// Resolve which agent a context-menu invocation targets. Prefer our creation
// map; fall back to matching the agent label inside the live tab name.
function resolveAgent(terminal) {
  const t = terminal || vscode.window.activeTerminal;
  if (!t) return undefined;
  if (agentOf.has(t)) return agentOf.get(t);
  const title = t.name || "";
  for (const agent of rosterAgents()) {
    const label = shortLabel(agent);
    if (title.includes(` ${label} · `) || title.includes(`${label} · `)) return agent;
  }
  return undefined;
}

function squadExec(args, agent) {
  cp.execFile(SQUAD, args, { timeout: 30000 }, (err, _out, stderr) => {
    if (err) vscode.window.showErrorMessage(`squad ${args[0]} ${agent}: ${stderr || err.message}`);
  });
}

function withAgent(terminal, fn) {
  const agent = resolveAgent(terminal);
  if (!agent) {
    vscode.window.showWarningMessage("Squad: this terminal isn't a squad agent.");
    return;
  }
  fn(agent);
}

function activate(context) {
  // ---- context-menu commands (registered in every window; they no-op
  // politely on non-agent terminals) ----
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.sendPrompt", (t) =>
      withAgent(t, async (agent) => {
        const text = await vscode.window.showInputBox({
          prompt: `Prompt to ${shortLabel(agent)}`,
          placeHolder: "typed into that agent's claude input, Enter included",
        });
        if (text) squadExec(["cmd", agent, text], agent);
      })
    ),
    vscode.commands.registerCommand("squad.compact", (t) =>
      withAgent(t, (agent) => squadExec(["cmd", agent, "/compact"], agent))
    ),
    vscode.commands.registerCommand("squad.interrupt", (t) =>
      withAgent(t, (agent) => squadExec(["key", agent, "Escape"], agent))
    ),
    vscode.commands.registerCommand("squad.restartResume", (t) =>
      withAgent(t, (agent) => squadExec(["restart", agent, "--resume"], agent))
    ),
    vscode.commands.registerCommand("squad.restartFresh", (t) =>
      withAgent(t, async (agent) => {
        const ok = await vscode.window.showWarningMessage(
          `Restart ${shortLabel(agent)} with a BLANK conversation?`,
          { modal: true },
          "Fresh restart"
        );
        if (ok) squadExec(["restart", agent, "--fresh"], agent);
      })
    )
  );

  // ---- standard claude slash commands (typed into the agent's pane) ----
  // /clear is destructive (wipes the conversation) -> modal confirm.
  for (const slash of ["context", "cost", "status", "doctor", "mcp", "model", "memory", "todos", "help"]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.slash.${slash}`, (t) =>
        withAgent(t, (agent) => squadExec(["cmd", agent, `/${slash}`], agent))
      )
    );
  }
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.slash.clear", (t) =>
      withAgent(t, async (agent) => {
        const ok = await vscode.window.showWarningMessage(
          `/clear wipes ${shortLabel(agent)}'s conversation. Sure?`,
          { modal: true },
          "Clear it"
        );
        if (ok) squadExec(["cmd", agent, "/clear"], agent);
      })
    ),
    vscode.commands.registerCommand("squad.slash.custom", (t) =>
      withAgent(t, async (agent) => {
        const cmd = await vscode.window.showInputBox({
          prompt: `Slash command for ${shortLabel(agent)}`,
          placeHolder: "/memory-sync, /review, …",
          validateInput: (v) => (v.startsWith("/") ? undefined : "must start with /"),
        });
        if (cmd) squadExec(["cmd", agent, cmd], agent);
      })
    )
  );

  // ---- cockpit terminals: only in the squad workspace ----
  const wf = vscode.workspace.workspaceFile;
  if (!wf || !wf.path.endsWith("squad.code-workspace")) return;

  // who + dash monitors: NAMED terminals (pinned tab names are a feature
  // here — no live titles wanted).
  const existingNames = new Set(vscode.window.terminals.map((t) => t.name));
  for (const [name, icon, color, cmd] of [
    ["who",  "eye",    "terminal.ansiBrightRed",   "squad who --watch"],
    ["dash", "layout", "terminal.ansiBrightGreen", "squad dash"],
  ]) {
    if (existingNames.has(name)) continue;
    const t = vscode.window.createTerminal({
      name,
      iconPath: new vscode.ThemeIcon(icon),
      color: new vscode.ThemeColor(color),
    });
    t.sendText(cmd);
  }

  // agent terminals: UNNAMED default-profile bash + typed attach
  for (const agent of rosterAgents()) {
    if ([...agentOf.values()].includes(agent)) continue;
    const label = shortLabel(agent);
    const [icon, color] = THEME[label] || FALLBACK;
    const t = vscode.window.createTerminal({
      iconPath: new vscode.ThemeIcon(icon),
      color: new vscode.ThemeColor(color),
    });
    agentOf.set(t, agent);
    t.sendText(`squad attach ${agent}`);
  }

  // keep the map tidy as terminals close
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((t) => agentOf.delete(t))
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
