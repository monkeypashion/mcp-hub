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
  "factory-fleet":      ["organization", "terminal.ansiBrightMagenta"],
  "spike":              ["beaker",    "terminal.ansiBrightGreen"],
  "pm":                 ["checklist", "terminal.ansiBrightCyan"],
  // faculty (general workspace) — same visual weight as the squad
  "homeassistant":        ["home",          "terminal.ansiGreen"],
  "weathercomp":          ["cloud",         "terminal.ansiBrightBlue"],
  "blendingvalverl":      ["flame",         "terminal.ansiRed"],
  "mindconnect-iot2050":  ["circuit-board", "terminal.ansiCyan"],
  "node-red-mvp":         ["credit-card",   "terminal.ansiBrightRed"],
  "dt-vm-spec":           ["vm",            "terminal.ansiMagenta"],
  "unifrog":              ["mortar-board",  "terminal.ansiBrightGreen"],
  "sam":                  ["graph",         "terminal.ansiYellow"],
  "health":               ["heart",         "terminal.ansiBrightMagenta"],
  "financial-planning":   ["graph-line",    "terminal.ansiBrightCyan"],
  "money-talks":          ["megaphone",     "terminal.ansiBrightYellow"],
  "subscriptions":        ["sync",          "terminal.ansiBlue"],
  "transport":            ["compass",       "terminal.ansiWhite"],
  "get-my-shit-in-order": ["tasklist",      "terminal.ansiBrightWhite"],
};
const FALLBACK = ["terminal", "terminal.ansiBrightBlack"];

// Terminal -> agent name, for context-menu target resolution
const agentOf = new Map();

function rosterRows() {
  const conf = path.join(os.homedir(), ".config", "squad", "squad.conf");
  try {
    return fs
      .readFileSync(conf, "utf8")
      .split("\n")
      .filter((l) => l.trim() && !l.trim().startsWith("#"))
      .map((l) => l.split("|"))
      .filter((f) => f[0] && f[0].trim() && f[1] && f[1].trim())
      .map((f) => ({
        agent: f[0].trim(),
        worktree: f[1].trim().replace(/^~(?=$|[/\\])/, os.homedir()),
      }));
  } catch {
    return [];
  }
}

function rosterAgents() {
  return rosterRows().map((r) => r.agent);
}

// Canonical form for path equality: realpath (symlink-proof — the whole tree
// moved once already), falling back to a plain resolve for paths that don't
// exist yet.
function canon(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return path.resolve(p);
  }
}

// Type into a terminal only once its shell is actually reading. sendText at
// creation races bash init: the tty echoes the raw line before the prompt,
// then readline echoes it again — every cockpit tab opened with a doubled
// command line at the top (2026-07-24). Shell-integration readiness is the
// real signal; the timeout is the fallback for shells where integration is
// off (then we're no worse than the old immediate send).
function sendWhenReady(t, text) {
  if (t.shellIntegration) {
    t.sendText(text);
    return;
  }
  let done = false;
  const fire = () => {
    if (done) return;
    done = true;
    sub && sub.dispose();
    t.sendText(text);
  };
  let sub;
  if (typeof vscode.window.onDidChangeTerminalShellIntegration === "function") {
    sub = vscode.window.onDidChangeTerminalShellIntegration((e) => {
      if (e.terminal === t) fire();
    });
  }
  setTimeout(fire, 4000);
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
  // one-click answers to the approval dialog — `squad answer` parses the
  // dialog's visible options and presses the matching digit (fail-closed,
  // so a stale 🔴 or a mismatched intent errors instead of guessing)
  for (const [cmd, intent] of [
    ["squad.answerYes", "yes"],
    ["squad.answerAlways", "always"],
    ["squad.answerNo", "no"],
  ]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(cmd, (...args) =>
        withAgents(args, (agents) =>
          agents.forEach((a) => squadExec(["answer", a, intent], a))
        )
      )
    );
  }

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
    vscode.commands.registerCommand("squad.stockPrompt", (...args) =>
      withAgents(args, async (agents) => {
        const file = path.join(os.homedir(), ".config", "squad", "prompts.txt");
        let prompts = [];
        try {
          prompts = fs
            .readFileSync(file, "utf8")
            .split("\n")
            .map((s) => s.trim())
            .filter((s) => s && !s.startsWith("#"));
        } catch {}
        if (!prompts.length) {
          vscode.window.showWarningMessage("No stock prompts — add lines to ~/.config/squad/prompts.txt");
          return;
        }
        const pick = await vscode.window.showQuickPick(prompts, {
          placeHolder: `Stock prompt for ${labels(agents)}`,
        });
        if (pick) agents.forEach((a) => squadExec(["cmd", a, pick], a));
      })
    ),
    vscode.commands.registerCommand("squad.broadcast", (...args) =>
      withAgents(args, async (agents) => {
        const prefix = "Please broadcast a message to the team saying: ";
        const text = await vscode.window.showInputBox({
          prompt: `Broadcast via ${labels(agents)}`,
          value: prefix,
          valueSelection: [prefix.length, prefix.length],
        });
        if (text && text.trim() !== prefix.trim())
          agents.forEach((a) => squadExec(["cmd", a, text], a));
      })
    ),
    vscode.commands.registerCommand("squad.dmVia", (...args) => {
      // sender = the clicked tab (multi-select doesn't apply: one courier)
      const sender = resolveAgent(args[0]);
      if (!sender) {
        vscode.window.showWarningMessage("Squad: this terminal isn't a squad agent.");
        return;
      }
      (async () => {
        const others = rosterAgents()
          .filter((a) => a !== sender)
          .map((a) => ({ label: shortLabel(a), agent: a }));
        const picks = await vscode.window.showQuickPick(others, {
          canPickMany: true,
          placeHolder: `Recipients — message goes via ${shortLabel(sender)} over the hub`,
        });
        if (!picks || !picks.length) return;
        const msg = await vscode.window.showInputBox({
          prompt: `From ${shortLabel(sender)} to ${picks.map((p) => p.label).join(", ")}`,
          placeHolder: "the message to deliver",
        });
        if (!msg) return;
        const names = picks.map((p) => p.agent).join(", ");
        squadExec(
          ["cmd", sender, `Please send this message via the hub to ${names}: ${msg}`],
          sender
        );
      })();
    }),
    vscode.commands.registerCommand("squad.compact", (...args) =>
      withAgents(args, (agents) => agents.forEach((a) => squadExec(["cmd", a, "/compact"], a)))
    ),
    vscode.commands.registerCommand("squad.interrupt", (...args) =>
      withAgents(args, (agents) => agents.forEach((a) => squadExec(["key", a, "Escape"], a)))
    ),
    // Start & attach: the faculty lifecycle action — brings a down agent up
    // AND attaches THIS terminal to it in one click. The attach line must be
    // typed into the terminal (attaching is a property of the terminal, not
    // of the agent), but sendText into a terminal that's ALREADY inside tmux
    // would land in the agent's claude input as a junk prompt — so gate on
    // the session actually being down (checked via tmux directly; if it's
    // up, this terminal either is attached already or the user detached on
    // purpose — say so instead of typing).
    vscode.commands.registerCommand("squad.startAttach", (...args) => {
      const list = Array.isArray(args[1]) && args[1].length ? args[1] : [args[0]];
      let any = false;
      for (const t of list) {
        const a = resolveAgent(t);
        if (!a || !t || typeof t.sendText !== "function") continue;
        any = true;
        cp.execFile("tmux", ["-L", "squad", "has-session", "-t", "=" + a], (err) => {
          if (err) {
            t.show(false);
            t.sendText(`squad attach ${a}`); // down: start-if-down attach — an explicit click, not window-open
          } else {
            vscode.window.showInformationMessage(
              `Squad: ${shortLabel(a)} is already up — this tab is attached, or reattach with: squad attach ${a}`
            );
          }
        });
      }
      if (!any) vscode.window.showWarningMessage("Squad: no squad agent in the selection.");
    }),
    vscode.commands.registerCommand("squad.stop", (...args) =>
      withAgents(args, (agents) => agents.forEach((a) => squadExec(["stop", a], a)))
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

  // ---- slash commands that take an OPTION ----
  // Set the same model/effort/voice mode across a multi-selection in two
  // clicks, instead of opening claude's picker in each agent by hand.
  // /model goes through `squad model` because an actual switch raises a
  // cache-invalidation confirm dialog that has to be answered; /effort and
  // /voice apply straight from the argument with no prompt.
  for (const m of ["default", "opus", "fable", "sonnet", "haiku"]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.model.${m}`, (...args) =>
        withAgents(args, (agents) => agents.forEach((a) => squadExec(["model", a, m], a)))
      )
    );
  }
  for (const e of ["low", "medium", "high", "xhigh", "max"]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.effort.${e}`, (...args) =>
        withAgents(args, (agents) => agents.forEach((a) => squadExec(["cmd", a, `/effort ${e}`], a)))
      )
    );
  }
  for (const v of ["hold", "tap", "off"]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.voice.${v}`, (...args) =>
        withAgents(args, (agents) => agents.forEach((a) => squadExec(["cmd", a, `/voice ${v}`], a)))
      )
    );
  }

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

  // ---- cockpit terminals: any workspace containing roster worktrees ----
  // Gate on FOLDER MEMBERSHIP, not the workspace filename (pre-0.9 this was
  // squad.code-workspace only): a terminal opens for each roster agent whose
  // worktree IS one of the current workspace's folders. One roster, and each
  // workspace (squad / general / windows) shows exactly its own agents.
  // Plain folder windows (no .code-workspace file) get no auto-terminals —
  // the context-menu commands above still work everywhere.
  const wf = vscode.workspace.workspaceFile;
  if (!wf) return;
  const wsDirs = new Set(
    (vscode.workspace.workspaceFolders || []).map((f) => canon(f.uri.fsPath))
  );
  const mine = rosterRows().filter((r) => wsDirs.has(canon(r.worktree)));
  if (!mine.length) return;

  // The OPERATOR's own terminal, first in the list: what's blocking you,
  // what's running in parallel, what's idle. Created here rather than as a
  // workspace task — an auto-run task sits behind VSCode's "allow automatic
  // tasks in this folder" permission and silently doesn't start, which is
  // exactly how it failed to appear on first try. The extension already
  // creates the agent terminals reliably; use the same door.
  // Named for the command that fills it (`squad board`), not just "board":
  // every other tab in the list is a bare agent name, so an unqualified
  // "board" reads like one more agent rather than the operator's own view.
  const BOARD = "squad-board";
  if (![...vscode.window.terminals].some((t) => t.name === BOARD)) {
    const b = vscode.window.createTerminal({
      name: BOARD,
      iconPath: new vscode.ThemeIcon("dashboard"),
      color: new vscode.ThemeColor("terminal.ansiYellow"),
      // neutral cwd — otherwise the tab's description names whatever the
      // workspace's first folder happens to be, which reads as an agent.
      // Guarded: ~/Projects is a Linux-box convention (fireblade's tree is
      // D:\Projects, not under the profile) — a missing cwd must fall back
      // to VSCode's default, not break the board terminal.
      cwd: fs.existsSync(path.join(os.homedir(), "Projects"))
        ? path.join(os.homedir(), "Projects")
        : undefined,
    });
    sendWhenReady(b, `${SQUAD} board -w`);
  }

  // The who engine runs headless as squad-who.service and its signal lives in
  // the tab titles; `squad dash` (tiled wall) remains one command away.

  // agent terminals: UNNAMED default-profile bash + typed attach.
  // --no-start is load-bearing: these attaches fire at window-open, and
  // start-if-down semantics here would mass-launch every rostered agent in
  // the workspace (14 for general) and re-run the 2026-07-19 up_one race.
  // A down agent's terminal shows how to start it; nothing launches itself.
  for (const { agent, worktree } of mine) {
    if ([...agentOf.values()].includes(agent)) continue;
    const label = shortLabel(agent);
    const [icon, color] = THEME[label] || FALLBACK;
    const t = vscode.window.createTerminal({
      iconPath: new vscode.ThemeIcon(icon),
      color: new vscode.ThemeColor(color),
      // The agent's OWN worktree, not VSCode's default (the workspace's
      // first folder): the tab's dimmed description shows the cwd folder,
      // so without this every tab in the panel described the same project
      // (2026-07-24) — and a shell left after a --no-start attach should
      // already be standing in the right repo anyway.
      cwd: worktree,
    });
    agentOf.set(t, agent);
    // `; clear` — the operator should never study a shell transcript in a
    // cockpit tab: for a down agent this leaves a titled tab with a bare
    // prompt (the OSC title survives clear), and after a later detach/stop
    // it wipes the dead session's scrollback the same way restart does.
    sendWhenReady(t, `squad attach --no-start ${agent}; clear`);
  }

  // Focusing a DOWN agent's terminal means "I want claude HERE" — offer (or
  // perform) the start right then, instead of leaving the operator at a bare
  // shell with instructions. Modes (squadTerminals.autoStart):
  //   confirm (default) — one-click toast. Operator's pick (2026-07-24,
  //                       once the tabs opened clean): a glance must never
  //                       start an agent, a click on the toast is cheap.
  //   focus             — starts immediately on focus; zero-click, but
  //                       window-restore/panel-reveal focus events count.
  //   off               — context menu only.
  // Guards: an arming delay swallows the window-restore burst, an inflight
  // window stops a double-send while tmux is still booting (a second attach
  // line after the first takes over would land in claude's input), and the
  // tmux down-check means an up agent is never touched.
  const armedAt = Date.now() + 5000;
  const inflight = new Map(); // terminal -> last prompt/start ts
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTerminal((t) => {
      const mode = vscode.workspace
        .getConfiguration("squadTerminals")
        .get("autoStart", "confirm");
      if (mode === "off" || !t || Date.now() < armedAt) return;
      const a = agentOf.get(t);
      if (!a) return;
      if (inflight.get(t) && Date.now() - inflight.get(t) < 15000) return;
      cp.execFile("tmux", ["-L", "squad", "has-session", "-t", "=" + a], (err) => {
        if (!err) return; // up — already attached, or detached on purpose
        const go = () => {
          inflight.set(t, Date.now());
          t.show(false);
          t.sendText(`squad attach ${a}`);
        };
        if (mode === "focus") {
          go();
          return;
        }
        inflight.set(t, Date.now()); // also throttles repeat toasts
        vscode.window
          .showInformationMessage(
            `Squad: ${shortLabel(a)} is down — start claude and attach?`,
            "Start & attach"
          )
          .then((pick) => {
            if (pick === "Start & attach") go();
          });
      });
    })
  );

  // keep the map tidy as terminals close
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((t) => agentOf.delete(t))
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
