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

// Context-menu commands may be invoked as (clickedTerminal) or — when tabs
// are multi-selected — as (clickedTerminal, selectedTerminals[]). Resolve
// every selected agent so actions apply to the whole selection.
function resolveAgents(args) {
  const list = Array.isArray(args[1]) && args[1].length ? args[1] : [args[0]];
  const agents = [];
  for (const t of list) {
    const a = resolveAgent(t);
    if (a) agents.push(a);
  }
  return [...new Set(agents)];
}

function withAgents(args, fn) {
  const agents = resolveAgents(args);
  if (!agents.length) {
    vscode.window.showWarningMessage("Squad: no squad agent in the selection.");
    return;
  }
  fn(agents);
}

const labels = (agents) => agents.map(shortLabel).join(", ");

function activate(context) {
  // ---- context-menu commands (registered in every window; they no-op
  // politely on non-agent terminals) ----
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.sendPrompt", (...args) =>
      withAgents(args, async (agents) => {
        const text = await vscode.window.showInputBox({
          prompt: `Prompt to ${labels(agents)}`,
          placeHolder: "typed into each agent's claude input, Enter included",
        });
        if (text) agents.forEach((a) => squadExec(["cmd", a, text], a));
      })
    ),
    vscode.commands.registerCommand("squad.compact", (...args) =>
      withAgents(args, (agents) => agents.forEach((a) => squadExec(["cmd", a, "/compact"], a)))
    ),
    vscode.commands.registerCommand("squad.interrupt", (...args) =>
      withAgents(args, (agents) => agents.forEach((a) => squadExec(["key", a, "Escape"], a)))
    ),
    vscode.commands.registerCommand("squad.restartResume", (...args) =>
      withAgents(args, (agents) => agents.forEach((a) => squadExec(["restart", a, "--resume"], a)))
    ),
    vscode.commands.registerCommand("squad.restartFresh", (...args) =>
      withAgents(args, async (agents) => {
        const ok = await vscode.window.showWarningMessage(
          `Restart ${labels(agents)} with BLANK conversation(s)?`,
          { modal: true },
          "Fresh restart"
        );
        if (ok) agents.forEach((a) => squadExec(["restart", a, "--fresh"], a));
      })
    )
  );

  // ---- standard claude slash commands (typed into the agent's pane) ----
  // /clear is destructive (wipes the conversation) -> modal confirm.
  for (const slash of ["context", "cost", "status", "doctor", "mcp", "model", "memory", "todos", "help"]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.slash.${slash}`, (...args) =>
        withAgents(args, (agents) => agents.forEach((a) => squadExec(["cmd", a, `/${slash}`], a)))
      )
    );
  }
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.slash.clear", (...args) =>
      withAgents(args, async (agents) => {
        const ok = await vscode.window.showWarningMessage(
          `/clear wipes the conversation(s) of: ${labels(agents)}. Sure?`,
          { modal: true },
          "Clear"
        );
        if (ok) agents.forEach((a) => squadExec(["cmd", a, "/clear"], a));
      })
    ),
    vscode.commands.registerCommand("squad.slash.custom", (...args) =>
      withAgents(args, async (agents) => {
        const cmd = await vscode.window.showInputBox({
          prompt: `Slash command for ${labels(agents)}`,
          placeHolder: "/memory-sync, /review, …",
          validateInput: (v) => (v.startsWith("/") ? undefined : "must start with /"),
        });
        if (cmd) agents.forEach((a) => squadExec(["cmd", a, cmd], a));
      })
    )
  );

  // ---- cockpit terminals: only in the squad workspace ----
  const wf = vscode.workspace.workspaceFile;
  if (!wf || !wf.path.endsWith("squad.code-workspace")) return;

  // No monitor terminals: the who engine runs headless as squad-who.service
  // and its signal lives in the tab titles; `squad who` (board) and
  // `squad dash` (tiled wall) remain one command away in any shell.

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
