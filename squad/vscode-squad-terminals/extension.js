// Squad Terminals — one PLAIN terminal per roster agent, auto-opened.
//
// Why not tasks: task terminals pin their tab name/description to the task
// system and never render the live ${sequence} title (the agent's claude
// status that tmux pushes). Plain terminals render it. This extension gives
// plain terminals the two things only tasks used to provide: auto-open on
// window load, and per-agent icon+color.
//
// Reads the roster from ~/.config/squad/squad.conf — add/rm agents with
// `squad add|rm` and the terminal list follows on next window load.
const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");

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

function activate() {
  // Only act in the squad workspace — this extension is machine-local but
  // the window might be any project.
  const wf = vscode.workspace.workspaceFile;
  if (!wf || !wf.path.endsWith("squad.code-workspace")) return;

  const existing = new Set(vscode.window.terminals.map((t) => t.name));
  for (const agent of rosterAgents()) {
    const label = shortLabel(agent);
    if (existing.has(label)) continue; // don't duplicate on window reloads
    const [icon, color] = THEME[label] || FALLBACK;
    vscode.window.createTerminal({
      name: label,
      iconPath: new vscode.ThemeIcon(icon),
      color: new vscode.ThemeColor(color),
      shellPath: path.join(os.homedir(), ".local", "bin", "squad"),
      shellArgs: ["attach", agent],
    });
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
