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
  "weather-comp":         ["cloud",         "terminal.ansiBrightBlue"],
  "blending-valve-rl-controller": ["flame", "terminal.ansiRed"],
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
  // faculty — fireblade windows cockpit
  "wispr-flow-alternative": ["mic",          "terminal.ansiBrightMagenta"],
  "pc-cleanup":             ["trash",        "terminal.ansiYellow"],
  "pc-upgrade":             ["arrow-up",     "terminal.ansiGreen"],
  "rclone-onedrive":        ["cloud-upload", "terminal.ansiBlue"],
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
        // field 4 = launch args. Carried so the Launch settings menu can show
        // only the action that would CHANGE something, instead of offering
        // both and inviting no-op clicks.
        args: (f[3] || "").trim(),
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

// Auto-start guards, shared between the focus handler and the manual
// commands so the two paths are mutually aware (2026-07-25, FB gap B):
//   inflight      — terminal -> last prompt/start ts. Stamped by BOTH the
//                   focus toast and squad.startAttach, so a manual start
//                   suppresses the focus toast for that terminal (and vice
//                   versa) instead of double-prompting.
//   pendingToasts — terminal -> armed setTimeout handle. The focus toast is
//                   DELAYED ~600ms and any squad.* command cancels all armed
//                   toasts: a right-click exists only to reach the context
//                   menu, and VSCode gives no mouse-button signal, so
//                   "command ran promptly after focus" is the only observable
//                   proxy for right-click intent. Right-click + dismiss with
//                   no pick still toasts after the delay — undetectable.
const inflight = new Map();
const pendingToasts = new Map();
function cancelPendingToasts() {
  for (const timer of pendingToasts.values()) clearTimeout(timer);
  pendingToasts.clear();
}

function withAgents(args, fn) {
  cancelPendingToasts();
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
      cancelPendingToasts();
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
      cancelPendingToasts();
      const list = Array.isArray(args[1]) && args[1].length ? args[1] : [args[0]];
      let any = false;
      for (const t of list) {
        const a = resolveAgent(t);
        if (!a || !t || typeof t.sendText !== "function") continue;
        any = true;
        cp.execFile("tmux", ["-L", "squad", "has-session", "-t", "=" + a], (err) => {
          if (err) {
            inflight.set(t, Date.now()); // manual start arms the focus-path throttle too
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
  // The command id can't be the /model token verbatim — `opus[1m]` and full
  // ids like `claude-opus-4-8` aren't legal id suffixes — so each entry is
  // [id suffix, token claude actually accepts]. Bare aliases (opus, fable,
  // sonnet) always mean the LATEST model of that line, so an alias entry
  // needs no edit when a new model ships; pinning an older one takes its
  // full id.
  const MODELS = [
    ["default", "default"],
    ["opus", "opus"],
    ["opus1m", "opus[1m]"],
    ["opus48", "claude-opus-4-8"],
    ["fable", "fable"],
    ["sonnet", "sonnet"],
    ["haiku", "haiku"],
  ];
  for (const [id, token] of MODELS) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.model.${id}`, (...args) =>
        withAgents(args, (agents) => agents.forEach((a) => squadExec(["model", a, token], a)))
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

  // Launch-settings state -> context keys, so the menu can show ONLY the action
  // that would change something. Without this the submenu offers on AND off
  // regardless of the current value, which tells the operator nothing about
  // what's set and invites applying a setting that's already applied.
  //
  // Reads the roster directly (same file `squad` writes) rather than shelling
  // out per menu-open — a context refresh runs on every terminal focus change,
  // so a subprocess there would be wasteful and racy.
  //
  // Keyed on the ACTIVE terminal only. With a mixed multi-selection the keys
  // describe the focused agent; the underlying verbs are idempotent, so the
  // worst case is one no-op for the others rather than anything incorrect.
  function launchStateOf(agent) {
    const row = rosterRows().find((r) => r.agent === agent);
    const args = row ? row.args : "";
    return {
      comms: /(^|\s)--(dangerously-load-development-)?channels(\s|$)/.test(args)
        ? /hub/.test(args)
        : false,
      resume: /(^|\s)--continue(\s|$)/.test(args),
    };
  }
  function refreshLaunchContext() {
    const t = vscode.window.activeTerminal;
    const a = t ? agentOf.get(t) : null;
    const s = a ? launchStateOf(a) : { comms: false, resume: false };
    vscode.commands.executeCommand("setContext", "squad.hasComms", s.comms);
    vscode.commands.executeCommand("setContext", "squad.hasResume", s.resume);
    // Only meaningful for a roster agent; without this both "turn on" entries
    // would show on the operator's own board/shell tabs.
    vscode.commands.executeCommand("setContext", "squad.isAgent", !!a);
  }
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTerminal(() => refreshLaunchContext())
  );
  refreshLaunchContext();

  // ---- launch settings: roster edits, NOT slash commands ----
  // Everything above types into the RUNNING claude and takes effect at once.
  // These two rewrite the agent's roster args and land on its NEXT launch, which
  // is why they sit beside the lifecycle actions rather than under Slash
  // commands. Toasted on success because a settings change with no visible
  // effect on the current session otherwise looks like a no-op click.
  //   comms  — hub channel-push wake (the --dangerously-load-development-channels
  //            flag). Normally automatic: any hub-opted-in agent is armed at
  //            launch. This is the manual override.
  //   resume — whether a relaunch keeps the conversation (--continue). Also read
  //            by `squad heal`, which REFUSES to auto-restart a deaf agent
  //            without it rather than destroy the conversation — so an agent
  //            with comms on and resume off can be detected-deaf and still
  //            unrecoverable without a human.
  for (const [verb, opt] of [
    ["comms", "on"], ["comms", "off"],
    ["resume", "on"], ["resume", "off"],
  ]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.${verb}.${opt}`, (...args) =>
        withAgents(args, (agents) => {
          agents.forEach((a) => squadExec([verb, opt, a], a));
          vscode.window.showInformationMessage(
            `Squad: ${verb} ${opt} for ${agents.map(shortLabel).join(", ")} — applies on next launch.`
          );
          // squadExec is fire-and-forget, so the roster write lands slightly
          // after this returns. Re-read once it has, or the menu would still
          // offer the action just taken.
          setTimeout(refreshLaunchContext, 800);
        })
      )
    );
  }

  // ---- transport: clone this agent into another VSCode workspace ----
  // The operator's model: pick the agent's tab, pick a TARGET WORKSPACE, and
  // the whole agent (code, memory, conversation, launch args, its own hub
  // identity) appears there ready to start. Workspaces are discovered rather
  // than configured — a .code-workspace file IS the target, so there is no
  // second registry to keep in sync with reality.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.transport", (...args) =>
      withAgents(args, async (agents) => {
        if (agents.length !== 1) {
          vscode.window.showWarningMessage("Squad: transport one agent at a time.");
          return;
        }
        const agent = agents[0];
        const found = new Set();
        for (const dir of [path.join(os.homedir(), "Projects"), os.homedir()]) {
          try {
            for (const f of fs.readdirSync(dir)) {
              if (f.endsWith(".code-workspace")) found.add(path.join(dir, f));
            }
          } catch { /* dir absent — fine */ }
        }
        // Never offer the workspace this agent already lives in.
        const here = vscode.workspace.workspaceFile;
        const picks = [...found]
          .filter((f) => !here || canon(f) !== canon(here.fsPath))
          .map((f) => ({ label: path.basename(f, ".code-workspace"), description: f }));
        if (!picks.length) {
          vscode.window.showWarningMessage(
            "Squad: no other .code-workspace files found in ~/Projects or ~."
          );
          return;
        }
        const pick = await vscode.window.showQuickPick(picks, {
          title: `Transport ${shortLabel(agent)} to which workspace?`,
          placeHolder: "The agent is CLONED — the source keeps running",
        });
        if (!pick) return;
        // Runs in a visible terminal rather than fire-and-forget: transport
        // REFUSES on a dirty or unpushed tree, and that refusal is something
        // the operator must read, not a silently-swallowed exit code.
        const t = vscode.window.createTerminal({
          name: `transport → ${pick.label}`,
          iconPath: new vscode.ThemeIcon("arrow-right"),
          color: new vscode.ThemeColor("terminal.ansiCyan"),
        });
        t.show(true);
        sendWhenReady(t, `${SQUAD} transport ${agent} --to ${JSON.stringify(pick.description)}`);
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

  // ---- cockpit terminals: any workspace containing roster worktrees ----
  // Gate on FOLDER MEMBERSHIP, not the workspace filename (pre-0.9 this was
  // squad.code-workspace only): a terminal opens for each roster agent whose
  // worktree IS one of the current workspace's folders. One roster, and each
  // workspace (squad / general / windows) shows exactly its own agents.
  // Plain folder windows (no .code-workspace file) get no auto-terminals —
  // the context-menu commands above still work everywhere.
  // Re-runnable: the roster and the workspace's folder list both change
  // UNDER a live window (transport adds a folder entry and a roster row to a
  // workspace that is already open), and terminal creation is idempotent —
  // every branch below skips what already exists. Called once at activation
  // and again from the watchers wired at the end of activate().
  const buildCockpit = () => {
  const wf = vscode.workspace.workspaceFile;
  if (!wf) return;
  const wsDirs = new Set(
    (vscode.workspace.workspaceFolders || []).map((f) => canon(f.uri.fsPath))
  );
  const mine = rosterRows().filter((r) => wsDirs.has(canon(r.worktree)));
  if (!mine.length) return;

  // The whole cockpit look rides on pushed titles, and that needs the
  // workspace to render sequences: with VSCode's DEFAULT tabs.title
  // (${process}) every glyph/model/ctx% the painter pushes is silently
  // ignored and tabs read "tmux homeassistant" (2026-07-24: the general
  // workspace was created without the settings squad always had — took a
  // day of ghost-chasing to spot). Warn once instead of letting the next
  // workspace repeat it.
  const tabsCfg = vscode.workspace.getConfiguration("terminal.integrated.tabs");
  if (tabsCfg.get("title") !== "${sequence}") {
    vscode.window.showWarningMessage(
      'Squad: this workspace does not set terminal.integrated.tabs.title = "${sequence}" — ' +
        "agent tabs will show process names instead of live status. Add it (and " +
        'tabs.description = "${progress}") to the workspace settings.'
    );
  }

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
      // already be standing in the right repo anyway. Guarded: a renamed/
      // moved folder must degrade to the default cwd, not kill the terminal
      // ("Starting directory does not exist", 2026-07-24 WeatherComp).
      cwd: fs.existsSync(worktree) ? worktree : undefined,
    });
    agentOf.set(t, agent);
    // `; clear` — the operator should never study a shell transcript in a
    // cockpit tab: for a down agent this leaves a titled tab with a bare
    // prompt (the OSC title survives clear), and after a later detach/stop
    // it wipes the dead session's scrollback the same way restart does.
    sendWhenReady(t, `squad attach --no-start ${agent}; clear`);
  }

  // hideOnStartup keeps VSCode from spawning a filler terminal into a
  // restored-open empty panel (the recurring "bash <first-folder>" ghost
  // tab) — which means revealing the panel is OUR job now: once the cockpit
  // is built, show it with the board on top, without stealing focus.
  const boardTerm = [...vscode.window.terminals].find((t) => t.name === BOARD);
  if (boardTerm) boardTerm.show(true);
  };

  buildCockpit();

  // Roster + folder watchers. Registered UNCONDITIONALLY — and deliberately
  // after buildCockpit()'s first call rather than behind it, because the case
  // that needs them most is a workspace with NO matching rows yet: transport
  // writes the folder entry and the roster row into an already-open window,
  // and without these the agent only appears after a manual reload. The old
  // code returned early when there was nothing to build, which is exactly
  // when a watcher matters.
  context.subscriptions.push(
    vscode.workspace.onDidChangeWorkspaceFolders(() => buildCockpit())
  );
  try {
    const confDir = path.join(os.homedir(), ".config", "squad");
    const confWatcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(vscode.Uri.file(confDir), "squad.conf")
    );
    // change AND create: `squad transport` appends to the roster, but a fresh
    // machine may not have the file at all when the window opens.
    confWatcher.onDidChange(() => buildCockpit());
    confWatcher.onDidCreate(() => buildCockpit());
    context.subscriptions.push(confWatcher);
  } catch {
    /* watcher unavailable (remote/virtual FS) — reload still works */
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
  // Guards: an arming delay swallows the window-restore burst, the shared
  // inflight window stops a double-send while tmux is still booting (a second
  // attach line after the first takes over would land in claude's input), the
  // tmux down-check means an up agent is never touched — and the prompt is
  // DEFERRED ~600ms via pendingToasts so a right-click that only wanted the
  // context menu never pops it (any squad.* command cancels armed toasts;
  // see the guard block above withAgents).
  const armedAt = Date.now() + 5000;
  const TOAST_DELAY_MS = 600;
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTerminal((t) => {
      const mode = vscode.workspace
        .getConfiguration("squadTerminals")
        .get("autoStart", "confirm");
      if (mode === "off" || !t || Date.now() < armedAt) return;
      const a = agentOf.get(t);
      if (!a) return;
      if (inflight.get(t) && Date.now() - inflight.get(t) < 15000) return;
      const prev = pendingToasts.get(t);
      if (prev) clearTimeout(prev);
      pendingToasts.set(
        t,
        setTimeout(() => {
          pendingToasts.delete(t);
          // Focus moved on during the delay (tab-walking) — stale, drop it.
          if (vscode.window.activeTerminal !== t) return;
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
        }, TOAST_DELAY_MS)
      );
    })
  );

  // keep the maps tidy as terminals close
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((t) => {
      agentOf.delete(t);
      inflight.delete(t);
      const timer = pendingToasts.get(t);
      if (timer) {
        clearTimeout(timer);
        pendingToasts.delete(t);
      }
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
