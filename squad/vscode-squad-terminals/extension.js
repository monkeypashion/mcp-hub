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
let buildCockpitRef = null;   // set at activation so commands can refresh tabs

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

// Start an agent with an explicit resume/fresh mode AND attach this terminal to
// it. Typed into the tab rather than exec'd in the background, because attaching
// is a property of THIS terminal — and because a background exec leaves the tab a
// bare shell, which is how "Start & attach" came to only start (2026-07-26).
// `clear` runs after attach returns (i.e. on detach) so the tab never shows the
// typed command or the launch chatter.
function startWithMode(args, mode) {
  const list = Array.isArray(args[1]) && args[1].length ? args[1] : [args[0]];
  let any = false;
  for (const t of list) {
    const a = resolveAgent(t);
    if (!a || !t || typeof t.sendText !== "function") continue;
    any = true;
    inflight.set(t, Date.now());          // suppress the focus toast for this tab
    t.show(false);
    t.sendText(`clear; squad restart ${a} ${mode} >/dev/null 2>&1 && squad attach ${a}; clear`);
  }
  if (!any) vscode.window.showWarningMessage("Squad: no squad agent in the selection.");
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
    // Start is a CHOICE, not a readout. It was one entry whose meaning depended
    // on the roster's --continue, so it could silently resume megabytes of prior
    // conversation — and wanting the other behaviour meant leaving the menu,
    // changing a setting under Launch settings, and coming back. Restart had it
    // right all along by offering both; Start now mirrors it.
    //
    // `restart --resume|--fresh` is a one-off override that never touches the
    // roster, and on a DOWN agent it routes to up_one with those args — so the
    // same two actions work whether the agent is up or down, and the old
    // has-session branch (and its "already up" dead end) disappears.
    //
    // Found by the operator using it, after I "fixed" the label and left the
    // trap underneath: naming a consequence is not the same as offering it.
    // ...and it must ATTACH, which is the whole second half of its name. The
    // first cut of this ran `restart` via execFile — which starts the agent in
    // its own tmux session and leaves the cockpit tab sitting as a bare shell,
    // so "Start & attach" only started. Worse, the operator then SEES the
    // command and its output in the tab, because nothing ever took the screen
    // over. `squad restart <a> --resume|--fresh && squad attach <a>` gets both:
    // the one-off mode override, then this terminal attaches; `clear` wipes the
    // typed line and the launch chatter so the tab shows the agent, not a
    // transcript of how it got there.
    vscode.commands.registerCommand("squad.startAttach", (...args) =>
      startWithMode(args, "--resume")
    ),
    vscode.commands.registerCommand("squad.startAttachFresh", (...args) =>
      startWithMode(args, "--fresh")
    ),
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
  // Shared by squad.transport and squad.transportAll: ask which machine, then
  // which workspace on it. Returns {host, file} or null if cancelled.
  const pickTarget = async (title) => {
        const here = vscode.workspace.workspaceFile;
        // Keep stdout even on a non-zero exit. `ls` over a glob that matches
        // nothing exits non-zero while still listing what DID match, and
        // discarding that made remote workspace discovery silently return
        // nothing (2026-07-26).
        const sh = (cmd) => {
          try {
            return cp.execSync(cmd, { timeout: 15000, stdio: ["ignore", "pipe", "ignore"] })
              .toString();
          } catch (e) {
            return e && e.stdout ? e.stdout.toString() : "";
          }
        };

        // ---- step 1: this machine, or another one? ----
        // Remote hosts come from the tailnet rather than a config file: the
        // tailnet IS the list of machines reachable without key management,
        // and a second registry would only drift from it.
        const hosts = sh("tailscale status")
          .split("\n")
          .map((l) => l.trim().split(/\s+/))
          .filter((f) => f.length >= 5 && f[0].startsWith("100."))
          .filter((f) => f[3] === "linux")            // needs git/python3/mcp-hub
          .filter((f) => !f.slice(4).join(" ").includes("offline"))
          .map((f) => f[1])
          .filter((h) => h && h !== os.hostname());

        const wsOn = (host) => {
          // Enumerate .code-workspace candidates — locally with fs, remotely
          // over ssh. Same two roots either way.
          const roots = ["~/Projects", "~"];
          if (!host) {
            const out = new Set();
            for (const dir of [path.join(os.homedir(), "Projects"), os.homedir()]) {
              try {
                for (const f of fs.readdirSync(dir)) {
                  if (f.endsWith(".code-workspace")) out.add(path.join(dir, f));
                }
              } catch { /* absent — fine */ }
            }
            return [...out];
          }
          const globs = roots.map((r) => `${r}/*.code-workspace`).join(" ");
          return sh(`tailscale ssh ${host} 'ls -1 ${globs} 2>/dev/null'`)
            .split("\n").map((s) => s.trim()).filter(Boolean);
        };

        let host = "";
        if (hosts.length) {
          const where = await vscode.window.showQuickPick(
            [
              { label: "This machine", description: os.hostname(), host: "" },
              ...hosts.map((h) => ({ label: h, description: "over the tailnet", host: h })),
            ],
            { title: `${title} — to which machine?`,
              placeHolder: "Agents are CLONED; the sources keep running" }
          );
          if (!where) return null;
          host = where.host;
        }

        // ---- step 2: which workspace on that machine? ----
        const files = wsOn(host).filter(
          (f) => host || !here || canon(f) !== canon(here.fsPath)   // never offer where it already lives
        );
        if (!files.length) {
          vscode.window.showWarningMessage(
            `Squad: no .code-workspace files found${host ? ` on ${host}` : " in ~/Projects or ~"}.`
          );
          return null;
        }
        const pick = await vscode.window.showQuickPick(
          files.map((f) => ({ label: path.basename(f, ".code-workspace"), description: f })),
          {
            title: `${title} — which workspace${host ? ` on ${host}` : ""}?`,
            placeHolder: "Refuses any repo that is dirty or unpushed",
          }
        );
        if (!pick) return null;
        return { host, file: pick.description, label: pick.label, sh };
  };

  // Runs in a visible terminal rather than fire-and-forget: transport REFUSES
  // on a dirty or unpushed tree, and that refusal is something the operator
  // must read, not a silently-swallowed exit code.
  const runTransport = (label, cmd) => {
    const t = vscode.window.createTerminal({
      name: `transport → ${label}`,
      iconPath: new vscode.ThemeIcon("arrow-right"),
      color: new vscode.ThemeColor("terminal.ansiCyan"),
    });
    t.show(true);
    sendWhenReady(t, cmd);
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("squad.transport", (...args) =>
      withAgents(args, async (agents) => {
        if (agents.length !== 1) {
          vscode.window.showWarningMessage("Squad: transport one agent at a time.");
          return;
        }
        const agent = agents[0];
        const target = await pickTarget(`Transport ${shortLabel(agent)}`);
        if (!target) return;
        runTransport(
          `${target.host ? target.host + ":" : ""}${target.label}`,
          `${SQUAD} transport ${agent} --to ${JSON.stringify(target.file)}` +
            (target.host ? ` --host ${target.host}` : "")
        );
      })
    )
  );

  // Squad-level clone. A bulk clone is expensive and partly irreversible, so
  // the DRY RUN is shown first and the operator confirms against the real
  // eligibility list — ineligible agents are named, never silently dropped.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.transportAll", async () => {
      const target = await pickTarget("Transport ALL agents");
      if (!target) return;
      const base =
        `${SQUAD} transport all --to ${JSON.stringify(target.file)}` +
        (target.host ? ` --host ${target.host}` : "");
      const preview = target.sh(`${base} --dry-run 2>&1`) || "(no output)";
      const go = await vscode.window.showWarningMessage(
        `Clone the squad into ${target.label}${target.host ? ` on ${target.host}` : ""}?`,
        { modal: true, detail: preview },
        "Transport"
      );
      if (go !== "Transport") return;
      runTransport(`${target.host ? target.host + ":" : ""}${target.label} (all)`, base);
    })
  );

  // ---- add an existing folder to this workspace as an agent ----
  // The PULL to transport's push: nothing is cloned, copied or re-keyed. You
  // point at a folder that already exists and it becomes a cockpit tab.
  //
  // Deliberately incurious about the folder: no git required, no prior Claude
  // history required. This is how the operator's scratch agents were made by
  // hand (wispr-flow-alternative, pc-cleanup, pc-upgrade — non-git, no args,
  // faculty), so the feature is that same act without editing a config file.
  //
  // If the folder DOES happen to be a git repo with an origin, we opt it into
  // the hub so it gets comms — a bonus when available, never a requirement.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.addFolder", async () => {
      const wf = vscode.workspace.workspaceFile;
      if (!wf) {
        vscode.window.showWarningMessage(
          "Squad: open a .code-workspace first — agent tabs only appear in one."
        );
        return;
      }
      const picked = await vscode.window.showOpenDialog({
        canSelectFolders: true,
        canSelectFiles: false,
        canSelectMany: false,
        openLabel: "Add as agent",
        title: "Add an existing folder to this workspace as an agent",
        defaultUri: vscode.Uri.file(
          fs.existsSync(path.join(os.homedir(), "Projects"))
            ? path.join(os.homedir(), "Projects")
            : os.homedir()
        ),
      });
      if (!picked || !picked.length) return;
      const dir = picked[0].fsPath;

      // Already an agent? Then this is a no-op worth naming, not a duplicate.
      const existing = rosterRows().find((r) => canon(r.worktree) === canon(dir));
      if (existing) {
        const inWs = (vscode.workspace.workspaceFolders || []).some(
          (f) => canon(f.uri.fsPath) === canon(dir)
        );
        vscode.window.showInformationMessage(
          `Squad: ${shortLabel(existing.agent)} already covers that folder` +
            (inWs ? "." : " — adding it to this workspace.")
        );
        if (inWs) return;
      }

      const out = cp.spawnSync(SQUAD, ["add-folder", dir], {
        timeout: 60000, encoding: "utf8",
      });
      const said = ((out.stdout || "") + (out.stderr || "")).trim();
      if (out.status !== 0) {
        vscode.window.showErrorMessage(`Squad: ${said || "add-folder failed"}`);
        return;
      }

      // Add the folder via the API rather than editing the workspace file:
      // VSCode owns the formatting, and this fires onDidChangeWorkspaceFolders,
      // which the cockpit builder already listens for — so the tab appears
      // without a reload.
      const already = (vscode.workspace.workspaceFolders || []).some(
        (f) => canon(f.uri.fsPath) === canon(dir)
      );
      if (!already) {
        vscode.workspace.updateWorkspaceFolders(
          (vscode.workspace.workspaceFolders || []).length, 0,
          { uri: vscode.Uri.file(dir), name: path.basename(dir) }
        );
      }
      vscode.window.showInformationMessage(
        `Squad: ${said || path.basename(dir)} — right-click its tab → Start & attach.`
      );
    })
  );

  // ---- workspace-scoped actions ----
  // The workspace is the unit the operator works in, and folder membership is
  // already how the cockpit decides which tabs to show — so these reuse that
  // one rule rather than inventing a second notion of "which agents".
  //
  // `Transport THIS workspace` exists because `all` is MACHINE-scoped, which was
  // wrong for both real cases: standing up a second squad for a side project, and
  // retiring a box by migrating one workspace. Machine scope would drag in every
  // unrelated faculty agent.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.transportWorkspace", async () => {
      const here = vscode.workspace.workspaceFile;
      if (!here) {
        vscode.window.showWarningMessage("Squad: open a .code-workspace first.");
        return;
      }
      const target = await pickTarget("Transport THIS workspace");
      if (!target) return;
      const base =
        `${SQUAD} transport workspace ${JSON.stringify(here.fsPath)}` +
        ` --to ${JSON.stringify(target.file)}` +
        (target.host ? ` --host ${target.host}` : "");
      // Dry run FIRST, confirmed against the real eligibility list: a bulk clone
      // is expensive and partly irreversible, and ineligible agents must be read
      // rather than discovered afterwards.
      const preview = target.sh(`${base} --dry-run 2>&1`) || "(no output)";
      const go = await vscode.window.showWarningMessage(
        `Clone this workspace's agents into ${target.label}` +
          `${target.host ? ` on ${target.host}` : ""}?`,
        { modal: true, detail: preview },
        "Transport"
      );
      if (go !== "Transport") return;
      runTransport(`${target.host ? target.host + ":" : ""}${target.label} (workspace)`, base);
    })
  );

  // Remove from THIS workspace vs retire everywhere: two different consequences,
  // so two entries with names that say which. Conflating them is the mistake the
  // Start label taught us.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.wsRemove", (...args) =>
      withAgents(args, async (agents) => {
        const here = vscode.workspace.workspaceFile;
        if (!here) {
          vscode.window.showWarningMessage("Squad: no .code-workspace open.");
          return;
        }
        const ok = await vscode.window.showWarningMessage(
          `Remove ${labels(agents)} from this workspace?`,
          {
            modal: true,
            detail:
              "The tab disappears from THIS workspace only. The agent stays " +
              "enrolled, keeps its hub identity and worktree, and still appears " +
              "in any other workspace that lists it.",
          },
          "Remove from workspace"
        );
        if (ok !== "Remove from workspace") return;
        agents.forEach((a) => squadExec(["ws-remove", a, "--from", here.fsPath], a));
        setTimeout(() => buildCockpitRef && buildCockpitRef(), 800);
      })
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("squad.retire", (...args) =>
      withAgents(args, async (agents) => {
        const ok = await vscode.window.showWarningMessage(
          `Retire ${labels(agents)} completely?`,
          {
            modal: true,
            detail:
              "Removes the roster row, opts the repo out of the hub and retires " +
              "the heartbeat daemon, on every workspace. The worktree and its " +
              "files are left on disk.",
          },
          "Retire agent"
        );
        if (ok !== "Retire agent") return;
        agents.forEach((a) => squadExec(["rm", a], a));
      })
    )
  );

  // Clone from GitHub with no local folder: org_alias picks the ssh alias (i.e.
  // WHICH GitHub account) and pull_local picks the path, so the operator only
  // supplies <org>/<repo>. Then it is listed here so a tab appears.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.addFromGitHub", async () => {
      const here = vscode.workspace.workspaceFile;
      const spec = await vscode.window.showInputBox({
        title: "Clone from GitHub and add as agent",
        prompt: "<org>/<repo> — cloned to ~/Projects/code/<org>/<repo> using that org's GitHub identity",
        placeHolder: "monkeypashion/some-repo",
        validateInput: (v) =>
          /^[^/\s]+\/[^/\s]+$/.test((v || "").trim()) ? null : "Expected <org>/<repo>",
      });
      if (!spec) return;
      const t = vscode.window.createTerminal({
        name: `clone ${spec.trim()}`,
        iconPath: new vscode.ThemeIcon("cloud-download"),
        color: new vscode.ThemeColor("terminal.ansiGreen"),
      });
      t.show(true);
      // Visible terminal, not fire-and-forget: an unknown org, a missing ssh
      // alias or a clone failure are all things the operator must read.
      sendWhenReady(
        t,
        `${SQUAD} add ${spec.trim()}` +
          (here ? ` --to ${JSON.stringify(here.fsPath)}` : "")
      );
    })
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
    // `&& clear`, not `; clear`. On a live agent, clear wipes the dead
    // session's scrollback after a detach — that's what it's for. But
    // `attach --no-start` on a DOWN agent prints the only affordance that tab
    // will ever show ("start it with… / right-click → Start & attach"), and an
    // unconditional clear wiped it, leaving a blank pane. Clicking it then did
    // nothing, because the start toast rides on onDidChangeActiveTerminal,
    // which never fires when the terminal you click is already active
    // (2026-07-26). attach exits 3 when down, so `&&` keeps the hint.
    sendWhenReady(t, `squad attach --no-start ${agent} && clear`);
  }

  // hideOnStartup keeps VSCode from spawning a filler terminal into a
  // restored-open empty panel (the recurring "bash <first-folder>" ghost
  // tab) — which means revealing the panel is OUR job now: once the cockpit
  // is built, show it with the board on top, without stealing focus.
  const boardTerm = [...vscode.window.terminals].find((t) => t.name === BOARD);
  if (boardTerm) boardTerm.show(true);
  };

  buildCockpitRef = buildCockpit;
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
                `Squad: ${shortLabel(a)} is down — start it?`,
                "Resume conversation",
                "Fresh conversation"
              )
              .then((pick) => {
                // The same two outcomes as the menu, worded the same. The old
                // toast offered "Start & attach" and then silently followed the
                // roster — a third vocabulary for one decision, able to
                // contradict the menu label sitting right beside it.
                if (!pick) return;
                // Route through the SAME helper the menu uses, so the two can
                // never diverge in what they actually DO. The first cut had the
                // toast starting without attaching, which left the operator
                // staring at command output in a bare shell.
                startWithMode(
                  [t],
                  pick === "Resume conversation" ? "--resume" : "--fresh"
                );
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
