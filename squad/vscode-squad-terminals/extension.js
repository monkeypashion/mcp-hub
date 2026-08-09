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
// Same directory as squad, installed as a link by the same step. Named
// explicitly rather than trusting PATH because a VSCode extension host does not
// inherit an interactive shell's PATH.
const MCP_HUB = path.join(os.homedir(), ".local", "bin", "mcp-hub");
// Guarded: QuickPickItemKind arrived in VSCode 1.64. Falling back to undefined
// degrades a separator into an ordinary unselectable-looking row rather than
// throwing while building the list, which would take the whole panel with it.
const SEPARATOR =
  (vscode.QuickPickItemKind && vscode.QuickPickItemKind.Separator) || undefined;

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
// The panel is revealed once, at startup. See buildCockpit's tail.
let revealedOnce = false;
let purgedOldViews = false;

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

// Icon+colour for an agent. Keyed on the REPO, found by longest-prefix match on
// the agent name — NOT on shortLabel, which only strips "-<hostname>".
//
// A transported agent carries an identity suffix (mcp-hub-dev-vm-1-general), so
// shortLabel returns the whole name, THEME misses, and the tab falls back to a
// grey terminal icon. The operator spotted it as "transport doesn't take the icon
// across" — but the icon was never data that travels; it's derived, and the
// derivation wasn't suffix-aware. Fixing the lookup fixes every suffixed identity
// at once, including ones nobody has created yet.
//
// LONGEST match, and only on a "-" boundary: shortest-first would let a key that
// prefixes another win wrongly, and an unanchored match would make "sam" claim
// "samba-...".
function themeFor(agent) {
  let best = null;
  for (const key of Object.keys(THEME)) {
    if (agent === key || agent.startsWith(key + "-")) {
      if (!best || key.length > best.length) best = key;
    }
  }
  return best ? THEME[best] : FALLBACK;
}

// An operator-editable list: one entry per line, "#" comments and blanks
// dropped. Read on every use, never cached — an edit takes effect at the next
// menu open, with no reload and no version bump.
//
// ONE function for both lists (~/.config/squad/prompts.txt and slash.txt) so
// "same rules as prompts.txt" is structurally true rather than a claim in a
// comment that can quietly stop being true.
const operatorListPath = (name) =>
  path.join(os.homedir(), ".config", "squad", name);

function readOperatorList(name) {
  try {
    return fs
      .readFileSync(operatorListPath(name), "utf8")
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s && !s.startsWith("#"));
  } catch {
    return [];
  }
}

// Create the list file with a worked example and open it for editing.
//
// "Stock prompt…" used to dead-end in a warning naming a path — a menu entry
// whose only possible outcome was to tell you it could do nothing, on any
// machine that had never made the file. Nothing created it, so nothing ever
// would. The seed is entirely commented out, so the first click still shows an
// empty list rather than silently installing prompts nobody chose.
//
// NEVER overwrites: this is the operator's file, and the only reason to be here
// is that it was missing.
async function openOperatorList(name, seed) {
  const p = operatorListPath(name);
  try {
    if (!fs.existsSync(p)) {
      fs.mkdirSync(path.dirname(p), { recursive: true });
      fs.writeFileSync(p, seed, { flag: "wx" });
    }
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(p));
    await vscode.window.showTextDocument(doc);
  } catch (e) {
    vscode.window.showErrorMessage(`Squad: could not open ${p}: ${e.message}`);
  }
}

const PROMPTS_SEED = `# Stock prompts — one per line, sent to the agent(s) you right-clicked.
# Lines starting with # are ignored, so delete a # to enable one.
#
# status please
# commit what you have, then tell me what is left
# write up where you got to in memory before you run out of context
`;

// short label: strip the derived "-<hostname>" (sanitized like cli.py) — from
// the MIDDLE as well as the end.
//
// A transported or duplicated agent is <repo>-<host>-<suffix>, so an
// end-anchored strip returned the whole raw name: the original rendered as
// "mcp-hub" while its copy rendered as "mcp-hub-fireblade-wsl-windows", side by
// side in one panel. Exactly the bug themeFor above was fixed for and this was
// not — the derivation has to be suffix-aware everywhere or nowhere.
//
// Mirrored in squad's short_label() — CHANGE BOTH OR NEITHER. A test runs a
// table of names through both implementations and fails if they disagree.
function shortLabel(agent) {
  const host = os.hostname().toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  if (agent.endsWith("-" + host)) return agent.slice(0, -(host.length + 1));
  const mid = agent.indexOf("-" + host + "-");
  if (mid !== -1) return agent.slice(0, mid) + agent.slice(mid + host.length + 1);
  return agent;
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
    // TWO PATHS, and they are not interchangeable — picked by whether a viewer
    // is ATTACHED, not by whether the agent is running.
    //
    // Typing is right for a SHELL tab: attaching is a property of THIS
    // terminal, and a background exec would leave the tab a bare shell (the
    // 2026-07-26 "Start & attach only started" regression, which is why the
    // typed form exists at all).
    //
    // Typing is WRONG for an attached tab: the pane is Claude, so the command
    // lands in the agent's own prompt. The operator hit this restarting a live
    // seat — "it just keeps putting the command into the chat box". For that
    // case `squad restart` must run in the BACKGROUND: it respawns the pane in
    // place and attached viewers keep watching, so nothing needs typing.
    //
    // Fails toward TYPING: if the probe errors we take the shell path, because
    // a stray command line in a tab is visible and recoverable, whereas a
    // background restart on a shell tab leaves the operator staring at a
    // prompt wondering why the button did nothing.
    cp.execFile(SQUAD, ["attached", a], { timeout: 10000 }, (err) => {
      const isAttached = !err;
      if (isAttached) {
        squadExec(["restart", a, mode], a);
      } else {
        t.sendText(
          `clear; squad restart ${a} ${mode} >/dev/null 2>&1 && squad attach ${a}; clear`
        );
      }
    });
  }
  if (!any) vscode.window.showWarningMessage("Squad: no squad agent in the selection.");
}

function squadExec(args, agent) {
  cp.execFile(SQUAD, args, { timeout: 30000 }, (err, _out, stderr) => {
    if (err) vscode.window.showErrorMessage(`squad ${args[0]} ${agent}: ${stderr || err.message}`);
  });
}

// Same shape for the hub CLI. Separate binary, so a caller cannot accidentally
// pass a hub verb to `squad` and get a confusing "unknown command" toast.
function hubExec(args, agent) {
  cp.execFile(MCP_HUB, args, { timeout: 30000 }, (err, _out, stderr) => {
    if (err) {
      vscode.window.showErrorMessage(
        `mcp-hub ${args[0]} ${agent}: ${stderr || err.message}`
      );
    }
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

// For actions that are SINGULAR BY NATURE — one courier, one seat to read, one
// thing to copy. The clicked tab wins.
//
// ⚠️ WHY THIS EXISTS RATHER THAN A `when` CLAUSE HIDING THE ITEM. VSCode
// exposes no terminal-SELECTION api and no selection-change event: a `when`
// clause can only read a context key, and a key can only be refreshed from an
// event, so any "is multi-selected" key would be stale at the moment the menu
// is drawn. The selection is knowable ONLY here, at invocation (args[1]) —
// which is why every menu item has to be safe under multi-select rather than
// filtered out of it, and why singular actions must say what they did with
// the rest of the selection instead of silently picking one.
function withOneAgent(args, what, fn) {
  cancelPendingToasts();
  const agents = resolveAgents(args);
  if (!agents.length) {
    vscode.window.showWarningMessage("Squad: no squad agent in the selection.");
    return;
  }
  const chosen = resolveAgent(args[0]) || agents[0];
  if (agents.length > 1) {
    // Named, not counted, and stated BEFORE the action: silently using one of
    // several highlighted tabs is the kind of thing an operator only notices
    // when the wrong agent has already spoken.
    const others = agents.filter((a) => a !== chosen);
    vscode.window.showInformationMessage(
      `${what}: using ${shortLabel(chosen)} only — ${others.length} other ` +
      `selected tab(s) ignored (${others.map(shortLabel).join(", ")}). ` +
      "This action has a single subject by nature."
    );
  }
  fn(chosen);
}

const labels = (agents) => agents.map(shortLabel).join(", ");

// The model from `mcp-hub settings --json`, as quick-pick rows.
//
// A quick pick rather than a webview because a webview is an EDITOR TAB — it
// sits in the file row and is closed, not dismissed, which is not what a
// settings view should feel like (operator, 2026-07-28: "it opened it as a file
// rather than a popup"). This appears over the centre, Escape closes it, and
// typing filters.
//
// label = the setting, description = its value, detail = its SOURCE. The source
// is a whole line of its own rather than a parenthetical because it is the point
// of the panel: these settings differ in scope, so a value alone cannot say
// whether changing it affects this agent or every agent on the machine.
// matchOnDetail makes those sources searchable too — "no workspace declares it"
// finds every hand-set value in one keystroke.
// ---- the settings view: a real panel, docked beside the terminals ----
//
// A webview in the EDITOR area is a file tab ("it opened it as a file rather
// than a popup"); a quick pick is a filter list that cannot lay anything out
// ("ok but very basic"); and the PANEL area shows one tab at a time, so a view
// there competes with the Terminal instead of sitting beside it. VSCode has no
// modal webview and no way to stack a contributed view with the terminal, so
// this lives in its own view container — visible at the same time as the panel,
// and draggable to the secondary sidebar, which VSCode then remembers.
//
// Nothing here names a location. `<viewId>.focus` reveals the view wherever it
// has been parked, which is what makes the position the operator's to choose
// rather than a thing to re-ship. The view ID is therefore load-bearing and
// pinned by a test.
//
// Values are rendered as SELECTS for editable rows and as plain text for the
// rest, which makes editability visible in the control itself rather than in a
// marker beside it. Sources get their own column: they are the point of the
// panel, and a value alone cannot say whether changing it affects this agent or
// every agent on the machine.

const esc = (s) =>
  String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

function settingsHtml(model, nonce) {
  const sections = (model.sections || [])
    .map((sec, si) => {
      const rows = (sec.rows || [])
        .map((r, ri) => {
          const control = r.edit
            ? `<select data-row="${si}.${ri}">` +
              (r.edit.choices.includes(r.value)
                ? ""
                : `<option selected>${esc(r.value)}</option>`) +
              r.edit.choices
                .map(
                  (c) =>
                    `<option${c === r.value ? " selected" : ""}>${esc(c)}</option>`
                )
                .join("") +
              `</select>`
            : `<div class="ro">${esc(r.value)}</div>`;
          const when = r.edit
            ? `<span class="when">applies ${esc(r.edit.applies)}</span>`
            : "";
          return `<div class="row${r.edit ? " editable" : ""}">
              <div class="k">${esc(r.label)}</div>
              <div class="v">${control}</div>
              <div class="src">${esc(r.source)}${when}</div>
            </div>`;
        })
        .join("");
      const note = sec.note ? `<div class="note">${esc(sec.note)}</div>` : "";
      return `<section><h2>${esc(sec.title)}</h2>${note}${rows}</section>`;
    })
    .join("");
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
 style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
 /* Every rule here is width-tolerant on purpose. This view is dockable —
    activity bar, secondary sidebar, panel, editor — and the operator moves it
    freely, so a layout that only works at one width is a layout that is broken
    most of the time. It was a three-column TABLE first, which needed width it
    never has in a sidebar: "formatting and ergonomics is terrible", correctly.
    Stacked is the base case; columns are the enhancement, not the assumption. */
 *{box-sizing:border-box}
 body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);
      font-size:var(--vscode-font-size);padding:.6rem .8rem 2rem;margin:0;
      line-height:1.45}
 h1{font-size:1.05em;font-weight:600;margin:0 0 .1rem;
    overflow-wrap:anywhere}
 .sub{opacity:.55;font-size:.85em;margin:0 0 .4rem}
 section{margin-top:1.15rem}
 h2{font-size:.7em;letter-spacing:.11em;font-weight:700;opacity:.65;margin:0;
    padding-bottom:.25rem;border-bottom:1px solid
    var(--vscode-widget-border,rgba(128,128,128,.25))}
 .note{opacity:.55;font-size:.83em;margin:.3rem 0 .1rem}
 .row{padding:.5rem 0;border-bottom:1px solid
      var(--vscode-widget-border,rgba(128,128,128,.10))}
 .row:last-child{border-bottom:0}
 .k{font-weight:500;overflow-wrap:anywhere}
 .v{margin:.22rem 0 .1rem}
 .ro{font-family:var(--vscode-editor-font-family);font-size:.92em;opacity:.85;
     overflow-wrap:anywhere}
 .src{opacity:.55;font-size:.82em;overflow-wrap:anywhere}
 .when{opacity:.9;margin-left:.4rem;font-style:italic}
 select{width:100%;background:var(--vscode-dropdown-background);
        color:var(--vscode-dropdown-foreground);
        border:1px solid var(--vscode-dropdown-border,
        var(--vscode-contrastBorder,rgba(128,128,128,.35)));
        border-radius:3px;padding:.28rem .4rem;font-family:inherit;
        font-size:inherit;cursor:pointer}
 select:hover{background:var(--vscode-dropdown-listBackground,
              var(--vscode-dropdown-background))}
 select:focus{outline:1px solid var(--vscode-focusBorder);outline-offset:-1px}
 .empty{opacity:.6;padding:1.2rem .2rem;line-height:1.6}
 /* Wide enough for two columns — editor tab, or a sidebar dragged out. The key
    and the value/source share a row instead of stacking, which is what makes
    the same view readable full-width without a second template. */
 @media (min-width:460px){
   body{padding:.8rem 1.2rem 2rem}
   .row{display:grid;grid-template-columns:minmax(7rem,14rem) 1fr;
        column-gap:1.2rem;align-items:baseline;padding:.42rem 0}
   .k{grid-row:1;grid-column:1}
   .v{grid-row:1;grid-column:2;margin:0}
   .src{grid-row:2;grid-column:2;margin-top:.15rem}
   select{width:auto;min-width:11rem;max-width:100%}
 }
</style></head><body>
<h1>${esc(model.agent)}</h1>
<div class="sub">read-only values show where they came from</div>
${sections}
<script nonce="${nonce}">
 const vs = acquireVsCodeApi();
 for (const el of document.querySelectorAll("select")) {
   el.addEventListener("change", () =>
     vs.postMessage({ type: "set", row: el.dataset.row, value: el.value }));
 }
</script>
</body></html>`;
}


// ONE editor tab, retargeted — not a tab per click.
//
// An editor tab is the only surface in VSCode that can render this well: full
// width, aligned columns, real dropdowns, proper typography. It is not a popup,
// and there is no modal webview to make it one — a native modal gives plain
// text in a proportional font with about three buttons, which cannot show a
// settings sheet and cannot edit one. Docked views were tried and are worse
// again: the panel area shows one tab at a time so it hides the terminals, and
// a sidebar is too narrow for anything but a stacked list.
//
// Retargeting rather than spawning is what stops "Settings…" turning into a row
// of near-identical tabs across a session with ten agents.
class SettingsPanel {
  constructor() {
    this.panel = undefined;
    this.agent = undefined;
    this.worktree = undefined;
    this.model = undefined;
    this.seq = 0;
  }

  show(agent, worktree) {
    this.agent = agent;
    this.worktree = worktree;
    if (this.panel) {
      // reveal() without taking focus away from wherever the operator is
      // typing would be wrong here: they asked for this panel, so it should
      // come forward.
      this.panel.reveal(undefined, false);
    } else {
      this.panel = vscode.window.createWebviewPanel(
        "squadSettings",
        "Squad settings",
        vscode.ViewColumn.Active,
        { enableScripts: true, retainContextWhenHidden: true }
      );
      this.panel.onDidDispose(() => {
        this.panel = undefined;
        this.model = undefined;
      });
      this.panel.webview.onDidReceiveMessage((msg) => this.onMessage(msg));
    }
    this.panel.title = `Settings — ${shortLabel(agent)}`;
    this.render();
  }

  render() {
    if (!this.panel) return;
    // Guards against an out-of-order reply retargeting the tab: two quick
    // clicks on different agents race, and the SLOWER cli call would otherwise
    // win and render the agent you didn't ask for last.
    const mine = ++this.seq;
    cp.execFile(
      MCP_HUB,
      ["settings", "--cwd", this.worktree, "--json"],
      { timeout: 15000 },
      (err, out, stderr) => {
        if (mine !== this.seq || !this.panel) return;
        if (err) {
          vscode.window.showErrorMessage(
            `Squad settings: ${(stderr || err.message || "").trim()}`
          );
          return;
        }
        try {
          this.model = JSON.parse(out);
        } catch {
          vscode.window.showErrorMessage("Squad settings: unreadable output.");
          return;
        }
        // A fresh nonce per render: reused across renders it is a nonce in
        // name only.
        this.panel.webview.html = settingsHtml(this.model, `n${mine}s${this.seq}`);
      }
    );
  }

  onMessage(msg) {
    if (!msg || msg.type !== "set" || !this.model) return;
    const [si, ri] = String(msg.row).split(".").map(Number);
    const section = (this.model.sections || [])[si];
    const row = section && (section.rows || [])[ri];
    // The row index is a CLAIM from the page, so it is resolved against the
    // model actually rendered and refused when it names a row that is not
    // editable — rather than trusted to address a command.
    if (!row || !row.edit) return;
    if (msg.value === row.value) return;
    const argv = row.edit.argv.map((a) => (a === "{}" ? msg.value : a));
    const bin = row.edit.bin === "mcp-hub" ? MCP_HUB : SQUAD;
    cp.execFile(bin, argv, { timeout: 20000 }, (err, _o, stderr) => {
      if (err) {
        vscode.window.showErrorMessage(
          `Squad: ${row.label} unchanged — ${(stderr || err.message || "").trim()}`
        );
      } else {
        vscode.window.showInformationMessage(
          `Squad: ${shortLabel(this.agent)} ${row.label} → ${msg.value} (applies ${row.edit.applies}).`
        );
      }
      // Re-read either way. On success the page must show the state after the
      // write rather than the value sent; on failure it must snap the control
      // BACK, because the browser already moved it and the page would otherwise
      // display a setting that was never applied.
      this.render();
    });
  }
}

const settingsView = new SettingsPanel();

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
        const prompts = readOperatorList("prompts.txt");
        if (!prompts.length) {
          const go = await vscode.window.showWarningMessage(
            "No stock prompts yet. They live in ~/.config/squad/prompts.txt — one prompt per line.",
            "Create and open it"
          );
          if (go) await openOperatorList("prompts.txt", PROMPTS_SEED);
          return;
        }
        const pick = await vscode.window.showQuickPick(prompts, {
          placeHolder: `Stock prompt for ${labels(agents)}`,
        });
        if (pick) agents.forEach((a) => squadExec(["cmd", a, pick], a));
      })
    ),
    vscode.commands.registerCommand("squad.broadcast", (...args) =>
      // 🔴 ONE COURIER, and it must stay that way. This used to fan out over
      // the whole selection, so three highlighted tabs sent the SAME broadcast
      // three times — and if they share a squad, that squad received three
      // copies of one message. A broadcast has exactly one sender by nature;
      // fanning it out is not "doing it for each agent", it is duplicating it.
      //
      // The clicked tab is the courier (args[0]), matching dmVia. Anything
      // else in the selection is ignored DELIBERATELY, and the prompt says
      // whose voice it goes out in so the choice is visible before sending.
      withOneAgent(args, "Broadcast", async (sender) => {
        const prefix = "Please broadcast a message to the team saying: ";
        const text = await vscode.window.showInputBox({
          prompt: `Broadcast via ${shortLabel(sender)} (one sender)`,
          value: prefix,
          valueSelection: [prefix.length, prefix.length],
        });
        if (text && text.trim() !== prefix.trim())
          squadExec(["cmd", sender, text], sender);
      })
    ),
    vscode.commands.registerCommand("squad.dmVia", (...args) =>
      // One courier, as before — but the ignored tabs are now NAMED rather
      // than silently dropped. Recipients are chosen in the pick list below,
      // so a multi-selection here is almost certainly the operator meaning
      // "these are the recipients", and saying nothing let that misread
      // stand.
      withOneAgent(args, "Message via", (sender) => {
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
      })
    ),
    // ---- settings: READ-ONLY, and deliberately so ----
    // Nothing in this panel DOES anything: no restart, no transport, no retire.
    // A settings panel that can destroy an agent is one you open carefully, and
    // it should be one you can open freely — its whole job is answering "what is
    // this agent actually set to", which nothing else does.
    //
    // The model comes from `mcp-hub settings --json`, not from reading files
    // here. Provenance is the hard part (a squad usually comes from a workspace,
    // comms is per agent, the hub URL is per machine) and duplicating that logic
    // in the extension is how the future web UI would come to disagree with the
    // cockpit about what an agent is set to.
    vscode.commands.registerCommand("squad.settings", (...args) =>
      withAgents(args, (agents) => {
        const agent = agents[0];
        const row = rosterRows().find((r) => r.agent === agent);
        if (!row) {
          vscode.window.showWarningMessage(`Squad: ${agent} has no roster row.`);
          return;
        }
        if (agents.length > 1) {
          vscode.window.showInformationMessage(
            `Squad: settings are per agent — showing ${shortLabel(agent)}.`
          );
        }
        settingsView.show(agent, row.worktree);
      })
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
    // These two ARE the restart pair. There used to be four commands here:
    // squad.restartResume/Fresh ran `squad restart` in the background, and
    // startAttach did the same thing plus an attach — the same action under two
    // names in two different submenus, which is most of why the menu needed
    // finding rather than reading (2026-07-28 flatten). startWithMode is the
    // superset: on an attached tab it takes the identical background path, and
    // on a bare shell it also attaches, which the background-only pair never
    // did. So the pair went and this one kept BOTH names' behaviour.
    //
    // The confirm came from the deleted squad.restartFresh and had to be
    // carried across deliberately: of the two commands that were merged only
    // one asked, and collapsing to the more permissive of a safe/unsafe pair is
    // how a guard disappears in a refactor that looks like pure tidying.
    vscode.commands.registerCommand("squad.startAttach", (...args) =>
      startWithMode(args, "--resume")
    ),
    vscode.commands.registerCommand("squad.startAttachFresh", async (...args) => {
      cancelPendingToasts();
      const agents = resolveAgents(args);
      if (agents.length) {
        const ok = await vscode.window.showWarningMessage(
          `Restart ${labels(agents)} with a BLANK conversation? The current conversation is kept on disk but not resumed.`,
          { modal: true },
          "Fresh restart"
        );
        if (!ok) return;
      }
      // Falls through with no agents so the "no squad agent" warning still
      // comes from one place.
      startWithMode(args, "--fresh");
    }),
    vscode.commands.registerCommand("squad.stop", (...args) =>
      withAgents(args, (agents) => agents.forEach((a) => squadExec(["stop", a], a)))
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

  // ---- focus (do not disturb) ----
  // Talks to the HUB, not the roster: focus is per-agent attention state held
  // server-side, so unlike model/effort there is nothing local to write and
  // nothing to apply on next launch — it takes effect immediately.
  //
  // Durations rather than a toggle because focus is bounded BY DESIGN: the
  // hub stores an expiry, not a flag, so "on" without a length is not a state
  // it can represent. Offering a bare toggle here would invite exactly the
  // forever-silenced agent the expiry exists to prevent.
  for (const mins of [30, 60, 120]) {
    context.subscriptions.push(
      vscode.commands.registerCommand(`squad.focus.${mins}`, (...args) =>
        withAgents(args, (agents) => {
          agents.forEach((a) => hubExec(["focus", String(mins), "--agent", a], a));
          vscode.window.showInformationMessage(
            `Squad: focus ${mins}m for ${agents.map(shortLabel).join(", ")} — ` +
            "normal messages queue, urgent still gets through, expires on its own."
          );
        })
      )
    );
  }
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.focus.off", (...args) =>
      withAgents(args, (agents) => {
        agents.forEach((a) => hubExec(["focus", "--off", "--agent", a], a));
        vscode.window.showInformationMessage(
          `Squad: focus off for ${agents.map(shortLabel).join(", ")} — wakes resume now.`
        );
      })
    )
  );

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
    // 🔴 `squad.hasComms` / `squad.hasResume` USED TO LIVE HERE and gated the
    // launch toggles, so only the state-changing half of each pair was shown.
    // That is correct for one tab and WRONG for a selection: the keys are read
    // from the ACTIVE terminal, so selecting agent A (comms on) and agent B
    // (comms off) offered whichever half suited A — hiding the very action B
    // needed, while the visible one fanned out to both.
    //
    // It cannot be fixed by computing the key across the selection, because
    // VSCode exposes no terminal-selection api and no selection-change event
    // (see withOneAgent). A key that cannot be refreshed when the selection
    // changes is a key that lies.
    //
    // So both halves are always shown. `squad comms on/off` is idempotent and
    // toasts what it did — a menu that occasionally offers a no-op is strictly
    // better than one that hides the action you came for.
    //
    // Only `isAgent` survives, and only because it is a property of the tab
    // itself rather than of the selection: without it the toggles would appear
    // on the operator's own board and shell tabs.
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

        // ---- step 1b: CAN that machine host an agent? ----
        // Asked here, before a workspace is even chosen, because the answer
        // changes what the operator is agreeing to. A brand-new server is
        // offered by the list above (any online Linux peer), and without this
        // the repo and memory ship before anything notices it cannot host them.
        let needsBootstrap = false;
        if (host) {
          const report = sh(`${SQUAD} preflight --host ${host} 2>&1`);
          if (!/READY/.test(report) || /NOT READY/.test(report)) {
            const go = await vscode.window.showWarningMessage(
              `${host} isn't set up to host agents yet. Set it up now?`,
              {
                modal: true,
                detail:
                  report +
                  "\nThis installs mcp-hub and squad for your user on that machine, " +
                  "adds the hub hooks, and leaves anything already there alone. " +
                  "It never installs system packages.",
              },
              "Set it up, then transport"
            );
            if (go !== "Set it up, then transport") return null;
            needsBootstrap = true;
          }
        }

        // ---- step 2: which workspace on that machine? ----
        const files = wsOn(host).filter(
          (f) => host || !here || canon(f) !== canon(here.fsPath)   // never offer where it already lives
        );
        // "Create it empty, then transport into it" is the operator's own
        // opening move, so a target that doesn't exist yet is the NORMAL case —
        // not an error. It must also be offered when the list is empty, which
        // used to dead-end in a warning: the one moment you most need to make a
        // workspace is when there isn't one.
        const NEW = "➕ New workspace…";
        const pick = await vscode.window.showQuickPick(
          [
            { label: NEW, description: host ? `on ${host}` : "in ~/Projects", isNew: true },
            ...files.map((f) => ({ label: path.basename(f, ".code-workspace"), description: f })),
          ],
          {
            title: `${title} — which workspace${host ? ` on ${host}` : ""}?`,
            placeHolder: files.length
              ? "Refuses any repo that is dirty or unpushed"
              : `No workspaces found${host ? ` on ${host}` : " in ~/Projects or ~"} — make one`,
          }
        );
        if (!pick) return null;
        if (!pick.isNew) return { host, file: pick.description, label: pick.label, sh, needsBootstrap };

        const name = await vscode.window.showInputBox({
          title: `${title} — name the new workspace`,
          placeHolder: "e.g. side-project",
          validateInput: (v) =>
            /^[A-Za-z0-9._-]+$/.test(v || "")
              ? null
              : "letters, digits, dot, dash and underscore only",
        });
        if (!name) return null;
        // Resolve the DESTINATION's home rather than assuming it matches this
        // box's. Quoting $HOME through ssh has already produced a directory
        // literally named "$HOME" once (2026-07-26); asking is the only way that
        // is right on both machines.
        const home = host
          ? sh(`tailscale ssh ${host} 'printf %s "$HOME"'`).trim()
          : os.homedir();
        if (!home) {
          vscode.window.showWarningMessage(`Squad: could not resolve $HOME on ${host}.`);
          return null;
        }
        // The file itself is created by transport at the far end, so there is
        // nothing to clean up if the operator cancels or the gate refuses.
        return { host, file: `${home}/Projects/${name}.code-workspace`, label: name, sh, needsBootstrap };
  };

  // Runs in a visible terminal rather than fire-and-forget: transport REFUSES
  // on a dirty or unpushed tree, and that refusal is something the operator
  // must read, not a silently-swallowed exit code.
  // Destinations this window has asked for and not yet adopted as folders.
  // Populated by Duplicate, drained by the roster watcher below.
  const pendingFolderAdds = new Set();

  // Add any pending destination whose roster row has now appeared. Going through
  // updateWorkspaceFolders is the whole point: it applies live and lets VSCode
  // own the file, where an external write to an OPEN .code-workspace makes VSCode
  // reload the window on its own heuristic and bin the terminal panel.
  //
  // Gated on the ROSTER ROW, not on a timer and not on the directory existing:
  // transport-recv writes the row last and only after every other step
  // succeeded, so the row is the success signal. A duplicate that fails leaves
  // nothing behind.
  const adoptPendingFolders = () => {
    if (!pendingFolderAdds.size) return;
    const rows = rosterRows();
    const folders = vscode.workspace.workspaceFolders || [];
    const have = new Set(folders.map((f) => canon(f.uri.fsPath)));
    for (const dest of [...pendingFolderAdds]) {
      if (!rows.some((r) => canon(r.worktree) === canon(dest))) continue;
      pendingFolderAdds.delete(dest);
      if (have.has(canon(dest))) continue;
      vscode.workspace.updateWorkspaceFolders(folders.length, 0, {
        uri: vscode.Uri.file(dest),
        name: path.basename(dest),
      });
      // No buildCockpit() here: adding a folder fires
      // onDidChangeWorkspaceFolders, which already rebuilds.
    }
  };

  // ---- the seat, as opposed to the session ----
  //
  // Everything above acts on the RUNNING claude in this tab. These three act
  // on the SEAT DECLARATION behind it — what the hub says should exist. The
  // split is why they are their own submenu rather than sprinkled among the
  // session actions: re-briefing a seat changes nothing in the tab you are
  // looking at until it is re-placed, and saying otherwise would be a lie the
  // menu tells by omission.
  //
  // All three are SINGULAR BY NATURE. `seats logs` reads one seat's output;
  // a re-brief needs one brief; a clone needs one source and one new name.
  // They use withOneAgent, which names the tabs it is ignoring — the only
  // honest option, since the menu cannot be filtered on selection size.
  const runInTerminal = (name, icon, cmd) => {
    const t = vscode.window.createTerminal({
      name,
      iconPath: new vscode.ThemeIcon(icon),
      color: new vscode.ThemeColor("terminal.ansiCyan"),
    });
    t.show(true);
    sendWhenReady(t, cmd);
  };

  context.subscriptions.push(
    // Read what a seat produced. A headless errand's whole point is that
    // nobody watched it, so this is the only way to see what it did — and
    // after reclaim it comes off the memory volume rather than docker logs.
    vscode.commands.registerCommand("squad.seatLogs", (...args) =>
      withOneAgent(args, "Show result", (agent) =>
        runInTerminal(`logs · ${shortLabel(agent)}`, "output",
          `${MCP_HUB} seats logs ${agent} --tail 200`)
      )
    ),
    // Re-brief: edit the DECLARATION. The toast is not decoration — a brief
    // changed while a container is running has no effect until it is
    // re-placed, and an operator who does not know that waits for a change
    // that never arrives.
    vscode.commands.registerCommand("squad.seatRebrief", (...args) =>
      withOneAgent(args, "Re-brief", async (agent) => {
        const text = await vscode.window.showInputBox({
          prompt: `New brief for ${shortLabel(agent)} — what this seat is FOR`,
          placeHolder: "or paste a path beginning with @ to read a file",
        });
        if (!text) return;
        runInTerminal(`re-brief · ${shortLabel(agent)}`, "edit",
          `${MCP_HUB} seats update ${agent} --brief ${JSON.stringify(text)}`);
        vscode.window.showInformationMessage(
          `Squad: ${shortLabel(agent)} re-briefed — a RUNNING container keeps ` +
          "the old brief until it is reclaimed and re-placed."
        );
      })
    ),
    // Clone the seat, not the tab: a second declaration from this one, spec
    // and all, under its own identity. The suffix is required by the hub for
    // the reason every clone path here learns — two seats sharing an identity
    // is the collapse the runtime exists to prevent.
    vscode.commands.registerCommand("squad.seatClone", (...args) =>
      withOneAgent(args, "Clone seat", async (agent) => {
        const suffix = await vscode.window.showInputBox({
          prompt: `Clone ${shortLabel(agent)} as ${shortLabel(agent)}-…`,
          placeHolder: "a short label, e.g. takeb",
          validateInput: (v) =>
            !v || !v.trim() ? "a label is required — the clone needs its own identity"
            : /[.:]/.test(v) ? "no dots or colons: tmux reads them as separators and the agent would be unaddressable"
            : null,
        });
        if (!suffix) return;
        runInTerminal(`clone · ${shortLabel(agent)}`, "files",
          `${MCP_HUB} seats clone ${agent} --as ${JSON.stringify(suffix.trim())}`);
      })
    )
  );

  // ---- squad management: FLEET-level, deliberately not a per-tab menu ----
  //
  // create/fork/merge/members, machine retirement and capsule placement are
  // about the TEAM, not about the tab that was right-clicked. Putting them in
  // the agent context menu would have made that menu longer and less true —
  // every entry there should be an answer to "do this to this agent".
  //
  // A quick-pick instead: one entry, reachable from the palette too, and the
  // one surface where multi-select is irrelevant by construction because
  // there is no selection involved.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.manage", async () => {
      cancelPendingToasts();
      const items = [
        { label: "$(organization) Squads — list", cmd: "squads list" },
        { label: "$(person-add) Squad members — list", cmd: "squads members", needs: "squad" },
        { label: "$(git-branch) Fork a squad…", cmd: "fork" },
        { label: "$(git-merge) Merge a squad…", cmd: "merge" },
        { label: "$(server) Seats — list", cmd: "seats list" },
        { label: "$(rocket) Placements — list", cmd: "placements list" },
        { label: "$(package) Capsules — list", cmd: "capsules list" },
        { label: "$(vm) Machines — list", cmd: "machines list" },
      ];
      const pick = await vscode.window.showQuickPick(items, {
        placeHolder: "Squad management — fleet-level, not this tab",
      });
      if (!pick) return;
      if (pick.cmd === "fork" || pick.cmd === "merge") {
        const from = await vscode.window.showInputBox({
          prompt: `${pick.cmd === "fork" ? "Fork" : "Merge"} which squad?`,
        });
        if (!from) return;
        const to = await vscode.window.showInputBox({
          prompt: pick.cmd === "fork"
            ? "Into which NEW squad? (the source keeps every member — a fork COPIES)"
            : "Into which squad? (it survives; the source is archived)",
        });
        if (!to) return;
        const members = pick.cmd === "fork"
          ? await vscode.window.showInputBox({
              prompt: "Which members? comma-separated, or empty for all",
              placeHolder: "alice,bob   (empty = the whole squad)",
            })
          : "";
        // --members, not positionals: argparse cannot bind a trailing
        // positional list after an option, so `fork dt --to x alice bob`
        // fails outright. Measured 2026-08-08.
        const flag = pick.cmd === "fork"
          ? `--to ${JSON.stringify(to)}` + (members && members.trim()
              ? ` --members ${JSON.stringify(members.trim())}` : "")
          : `--into ${JSON.stringify(to)}`;
        runInTerminal(`squads ${pick.cmd}`, "organization",
          `${MCP_HUB} squads ${pick.cmd} ${JSON.stringify(from)} ${flag}`);
        return;
      }
      if (pick.needs === "squad") {
        const squad = await vscode.window.showInputBox({ prompt: "Which squad?" });
        if (!squad) return;
        runInTerminal("squad members", "organization",
          `${MCP_HUB} ${pick.cmd} ${JSON.stringify(squad)}`);
        return;
      }
      runInTerminal(pick.cmd, "list-unordered", `${MCP_HUB} ${pick.cmd}`);
    })
  );

  const runTransport = (label, cmd) => {
    const t = vscode.window.createTerminal({
      name: `transport → ${label}`,
      iconPath: new vscode.ThemeIcon("arrow-right"),
      color: new vscode.ThemeColor("terminal.ansiCyan"),
    });
    t.show(true);
    sendWhenReady(t, cmd);
  };

  // Duplicate: a second seat on one repo, landing in the workspace you are
  // already looking at. No target picker, because there is no target to pick —
  // that is the whole point of it being a separate entry from Transport. The
  // occurrence number is worked out by `squad duplicate`, which is the one thing
  // neither the operator nor this side can know: it depends on what the
  // workspace already holds.
  //
  // Runs in its own terminal like the other transport-family actions rather than
  // typing into the agent's tab — that tab may hold a LIVE claude, where a typed
  // shell command becomes a prompt.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.duplicate", (...args) =>
      withAgents(args, async (agents) => {
        if (agents.length !== 1) {
          vscode.window.showWarningMessage("Squad: duplicate one agent at a time.");
          return;
        }
        const ws = vscode.workspace.workspaceFile;
        if (!ws) {
          vscode.window.showWarningMessage(
            "Squad: open a .code-workspace to duplicate into — a duplicate joins " +
              "the workspace it is made in, and there isn't one here."
          );
          return;
        }
        const agent = agents[0];
        // Ask where it WILL land before starting, so this side can add the
        // folder itself. The dry run is also the gate check, so a refusal is
        // reported here instead of scrolling past in a terminal.
        let dest;
        try {
          const out = cp.execFileSync(
            SQUAD,
            ["duplicate", agent, "--to", ws.fsPath, "--dry-run"],
            { encoding: "utf8", timeout: 30000 }
          );
          dest = (out.match(/^\s*dest\s*:\s*(.+)$/m) || [])[1];
          if (dest) dest = dest.trim();
        } catch (e) {
          const why = String((e && (e.stdout || e.stderr || e.message)) || e).trim();
          vscode.window.showWarningMessage(
            `Squad: cannot duplicate ${shortLabel(agent)} — ${why.split("\n").pop()}`
          );
          return;
        }
        if (!dest) {
          vscode.window.showWarningMessage(
            "Squad: could not work out where the duplicate would land."
          );
          return;
        }
        // Adopt it only once the ROSTER ROW appears, which transport-recv writes
        // last and only on success — so a failed duplicate never leaves a
        // phantom folder pointing at nothing.
        pendingFolderAdds.add(dest);
        runTransport(
          `duplicate ${shortLabel(agent)}`,
          `${SQUAD} duplicate ${agent} --to ${JSON.stringify(ws.fsPath)} --no-folder-entry`
        );
      })
    )
  );

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
            (target.host ? ` --host ${target.host}` : "") +
        (target.needsBootstrap ? " --bootstrap" : "")
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
        (target.host ? ` --host ${target.host}` : "") +
        (target.needsBootstrap ? " --bootstrap" : "");
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

  // ---- attach a placed capsule ----
  // `capsules place` writes desired state and the edge makes containers exist;
  // NEITHER gives the operator a way in. That gap was closed in the CLI and
  // nowhere else, so standing up a container squad meant a terminal — which by
  // the standing rule makes the whole capsule half-delivered.
  //
  // Two things this deliberately cannot do, both because they are destination
  // facts (the same reason `transport-recv` exists rather than the source
  // doing the wiring):
  //
  //   - It only ever enrols seats belonging to THIS machine. Under Remote-SSH
  //     "this machine" is the remote host, which is exactly the case that
  //     matters; a capsule spanning two boxes is attached once per box.
  //   - It cannot run before the edge has realized the containers. The work
  //     folders must already exist, and the CLI REFUSES the whole capsule when
  //     one is missing rather than letting docker create it as root under a
  //     seat that runs as uid 1000.
  //
  // The plan is read as JSON, never parsed from the rendered lines: a UI
  // reading a human format is how the board came to attribute agents by repo
  // basename, and every later wording change becomes a silent behaviour change.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.capsuleAttach", async () => {
      const wf = vscode.workspace.workspaceFile;
      if (!wf) {
        vscode.window.showWarningMessage(
          "Squad: open a .code-workspace first — agent tabs only appear in one."
        );
        return;
      }

      const listed = cp.spawnSync(MCP_HUB, ["capsules", "list", "--json"], {
        timeout: 30000, encoding: "utf8",
      });
      if (listed.status !== 0) {
        vscode.window.showErrorMessage(
          `Squad: capsules list — ${(listed.stderr || "failed").trim()}`
        );
        return;
      }
      let capsules = [];
      try {
        capsules = JSON.parse(listed.stdout || "[]");
      } catch (e) {
        vscode.window.showErrorMessage(`Squad: unreadable capsule list — ${e}`);
        return;
      }
      if (!capsules.length) {
        vscode.window.showInformationMessage(
          "Squad: no capsules — freeze one with `mcp-hub capsules compose`, " +
            "then place it on a machine."
        );
        return;
      }

      const pick = await vscode.window.showQuickPick(
        capsules.map((c) => ({
          label: c.squad || c.id,
          description: c.id,
          // `created` is an epoch FLOAT (seconds). Rendering it raw puts
          // "composed 1785934605.87" in front of the operator, which is a
          // number pretending to be information.
          detail: c.created
            ? `composed ${new Date(c.created * 1000).toISOString().slice(0, 16).replace("T", " ")}` +
              ` · ${((c.manifest || {}).seats || []).length} seat(s)`
            : undefined,
          id: c.id,
        })),
        { title: "Attach which capsule?", placeHolder: "Its seats gain a tab here" }
      );
      if (!pick) return;

      // Dry run FIRST, always. The confirmation names what will happen to each
      // seat, so a refusal is read before anything is written rather than
      // discovered halfway through.
      const dry = cp.spawnSync(
        MCP_HUB, ["capsules", "attach", pick.id, "--dry-run", "--json"],
        { timeout: 60000, encoding: "utf8" }
      );
      let plan;
      try {
        plan = JSON.parse(dry.stdout || "{}");
      } catch (e) {
        vscode.window.showErrorMessage(
          `Squad: unreadable attach plan — ${(dry.stderr || e).toString().trim()}`
        );
        return;
      }
      if ((plan.refused || []).length) {
        vscode.window.showErrorMessage(
          `Squad: ${plan.refused.length} seat(s) refused, nothing written — ` +
            plan.refused.map((r) => `${r.identity}: ${r.reason}`).join("; ") +
            ". Has the edge realized this capsule on this machine yet?"
        );
        return;
      }
      const enrol = plan.enrol || [];
      if (!enrol.length) {
        // Distinguish the two empty cases — "already done" is success and
        // "nothing here belongs to this box" is a different machine's job.
        const mine = (plan.plan || []).filter((p) => p.action !== "skip");
        vscode.window.showInformationMessage(
          mine.length
            ? "Squad: every seat in that capsule is already enrolled here."
            : `Squad: none of that capsule's seats belong to ${plan.machine} — ` +
                "attach it on the machine hosting them."
        );
        return;
      }

      const go = await vscode.window.showInformationMessage(
        `Attach ${enrol.length} seat(s) to this workspace?`,
        { modal: true, detail: enrol.map((s) => `${s.identity}\n  ${s.folder}`).join("\n") },
        "Attach"
      );
      if (go !== "Attach") return;

      const out = cp.spawnSync(MCP_HUB, ["capsules", "attach", pick.id, "--json"], {
        timeout: 120000, encoding: "utf8",
      });
      if (out.status !== 0) {
        vscode.window.showErrorMessage(
          `Squad: attach failed — ${(out.stderr || "").trim() || "see terminal"}`
        );
        return;
      }

      // Folders go in via the API, not by writing the workspace file: VSCode
      // owns the formatting and this fires onDidChangeWorkspaceFolders, which
      // the cockpit builder already listens for — so tabs appear without a
      // reload. (`--workspace` is deliberately NOT passed for the same reason:
      // two writers of one hand-formatted JSONC file is how comments die.)
      const existing = vscode.workspace.workspaceFolders || [];
      const fresh = enrol.filter(
        (s) => !existing.some((f) => canon(f.uri.fsPath) === canon(s.folder))
      );
      if (fresh.length) {
        vscode.workspace.updateWorkspaceFolders(
          existing.length, 0,
          ...fresh.map((s) => ({
            uri: vscode.Uri.file(s.folder), name: shortLabel(s.identity),
          }))
        );
      }
      vscode.window.showInformationMessage(
        `Squad: ${enrol.length} seat(s) attached — each tab runs ` +
          "`docker exec` into its container. Right-click → Start & attach."
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
        (target.host ? ` --host ${target.host}` : "") +
        (target.needsBootstrap ? " --bootstrap" : "");
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

  // ---- tear this workspace's squad down ----
  // The closing move for the ephemeral case: spin a parallel squad up for a side
  // project, then close it down. Without this the clones just accumulate.
  //
  // Code is KEPT unless explicitly asked for, and even then squad only removes
  // TRANSPORTED CLONES — deletion is gated on a transport-registered identity
  // suffix, so pointing this at the main workspace cannot delete an original.
  // The two menu entries name their consequence rather than their mechanism.
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.teardownWorkspace", async () => {
      const here = vscode.workspace.workspaceFile;
      if (!here) {
        vscode.window.showWarningMessage("Squad: open a .code-workspace first.");
        return;
      }
      const mode = await vscode.window.showQuickPick(
        [
          {
            label: "Close down, keep every folder on disk",
            detail: "Removes the tabs, roster rows and daemons. No code is deleted.",
            del: false,
          },
          {
            label: "Close down and delete the transported clones",
            detail:
              "Also removes the worktrees + Claude state of agents that were " +
              "transported here. Originals, added folders, and any clone with " +
              "unpushed work are kept and named.",
            del: true,
          },
        ],
        {
          title: `Tear down ${path.basename(here.fsPath, ".code-workspace")}`,
          placeHolder: "This workspace's agents",
        }
      );
      if (!mode) return;

      const base =
        `${SQUAD} teardown workspace ${JSON.stringify(here.fsPath)}` +
        (mode.del ? " --delete-worktrees" : "");
      // Confirm against the REAL plan, per agent, not a generic warning. The
      // dry run is what distinguishes "delete two clones" from "delete the
      // repos you work in" — so it must be read before anything is removed.
      let preview;
      try {
        preview = cp
          .execSync(`${base} --dry-run 2>&1`, { timeout: 20000, stdio: ["ignore", "pipe", "pipe"] })
          .toString();
      } catch (e) {
        preview = (e && e.stdout ? e.stdout.toString() : "") || "(no output)";
      }
      const go = await vscode.window.showWarningMessage(
        `Tear down ${path.basename(here.fsPath, ".code-workspace")}?`,
        { modal: true, detail: preview },
        mode.del ? "Tear down and delete" : "Tear down"
      );
      if (!go) return;

      const t = vscode.window.createTerminal({
        name: `teardown → ${path.basename(here.fsPath, ".code-workspace")}`,
        iconPath: new vscode.ThemeIcon("trash"),
        color: new vscode.ThemeColor("terminal.ansiRed"),
      });
      t.show(true);
      sendWhenReady(t, `${base} --yes`);
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
        // Remove via the API, NOT by shelling out to `squad ws-remove`.
        // ws-remove edits the .code-workspace file directly, and VSCode reacts to
        // an external change to that file by RELOADING THE WINDOW on its own
        // heuristic — the operator saw it reload on one removal and not the next,
        // which is the same inconsistency from two different mechanisms.
        // updateWorkspaceFolders applies live, never reloads, and lets VSCode own
        // the file's formatting. Same reasoning as the add path.
        //
        // The CLI verb stays: it is the only way to edit a workspace that isn't
        // currently open.
        const folders = vscode.workspace.workspaceFolders || [];
        const targets = new Set(
          agents
            .map((a) => (rosterRows().find((r) => r.agent === a) || {}).worktree)
            .filter(Boolean)
            .map(canon)
        );
        // Remove highest index first: each removal reindexes the ones after it.
        const idx = folders
          .map((f, i) => [i, canon(f.uri.fsPath)])
          .filter(([, p]) => targets.has(p))
          .map(([i]) => i)
          .sort((x, y) => y - x);
        if (!idx.length) {
          vscode.window.showInformationMessage(
            "Squad: this workspace doesn't list that folder."
          );
          return;
        }
        for (const i of idx) vscode.workspace.updateWorkspaceFolders(i, 1);
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
  // Each of these had its OWN menu entry until the 2026-07-28 flatten: eleven
  // rows you navigated by remembering their order. One searchable list is
  // shorter to read and faster to use — you type "cost" quicker than you find
  // it — and it collapses the last real difference between a built-in and an
  // operator-saved command, which were two rules for one menu.
  //
  // The list is therefore shown ALWAYS, including with no slash.txt. That
  // reverses the older rule ("no file ⇒ no quick pick, straight to the input
  // box") and had to: with the individual entries gone, this picker is the only
  // way left to reach /context, so skipping it would remove the commands rather
  // than move them.
  const BUILTIN_SLASH = [
    "/context", "/cost", "/status", "/todos", "/mcp", "/doctor", "/help",
    "/compact", "/model", "/memory", "/clear",
  ];
  const DESTRUCTIVE = new Set(["/clear"]);
  context.subscriptions.push(
    vscode.commands.registerCommand("squad.slash.custom", (...args) =>
      withAgents(args, async (agents) => {
        const TYPE = "✎ Type one…";
        // A line missing its leading "/" is a typo, not a different intent —
        // normalise it, and show the normalised form so the list says exactly
        // what will be sent.
        const saved = readOperatorList("slash.txt").map((s) =>
          s.startsWith("/") ? s : "/" + s
        );
        // Saved first: they are the ones this operator curated, and putting the
        // eleven built-ins above them would bury a short personal list under a
        // fixed one. Deduped, so a saved "/status" replaces the built-in in
        // place rather than appearing twice.
        const items = [TYPE, ...new Set([...saved, ...BUILTIN_SLASH])];
        const pick = await vscode.window.showQuickPick(items, {
          placeHolder: `Slash command for ${labels(agents)}`,
        });
        if (!pick) return;
        let cmd = pick === TYPE ? undefined : pick;
        if (!cmd) {
          cmd = await vscode.window.showInputBox({
            prompt: `Slash command for ${labels(agents)}`,
            placeHolder: "/memory-sync, /review, …",
            validateInput: (v) => (v.startsWith("/") ? undefined : "must start with /"),
          });
        }
        if (!cmd) return;
        // /clear had a modal confirm when it was its own menu entry. Gating on
        // the COMMAND rather than on the entry it arrived through keeps that
        // guard whichever route reaches it — picked from the list, or typed.
        if (DESTRUCTIVE.has(cmd.trim())) {
          const ok = await vscode.window.showWarningMessage(
            `${cmd.trim()} wipes the conversation(s) of: ${labels(agents)}. Sure?`,
            { modal: true },
            "Clear"
          );
          if (!ok) return;
        }
        agents.forEach((a) => squadExec(["cmd", a, cmd], a));
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
  // ONE operator view, not two. The text board (`squad board -w`) and the
  // settings panel were separate tabs until 2026-07-29, when the settings
  // panel absorbed the board (operator: "consolidate the two items into
  // one") — the Textual app now shows the live fleet AND the settings sheet,
  // so a second tab would be the same data rendered worse. The tab keeps the
  // board's NAME and icon because the board is what it is; the settings
  // sheet is one section of it. `squad board` itself remains a CLI command —
  // and this panel's data source.
  //
  // A TERMINAL, after four attempts at VSCode-native surfaces that each failed
  // for a structural reason: a webview in the editor is a file tab, a quick
  // pick cannot lay anything out, a panel view HIDES the terminals it sits
  // beside, and a sidebar is too narrow. This asks nothing of VSCode's UI —
  // it is a tab in the panel like every agent, so it can stay open, be clicked
  // back to, and look however we render it.
  //
  // Scoped to THIS workspace by the same folder-membership rule the tabs use,
  // so the panel lists exactly the agents whose tabs are beside it.
  const BOARD = "squad-board";
  // Retired tab names from the two-tab era. A restored "squad-settings"
  // terminal would sit beside the new board as a duplicate view; a restored
  // "squad-board" is the OLD text-board watch loop wearing the new tab's
  // name, which would satisfy the dedup below and keep the old view forever.
  // Purged ONCE per activation — within a session the dedup owns the name.
  if (!purgedOldViews) {
    purgedOldViews = true;
    for (const t of [...vscode.window.terminals]) {
      if (t.name === "squad-settings" || t.name === BOARD) t.dispose();
    }
  }
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
    const ws = vscode.workspace.workspaceFile;
    sendWhenReady(
      b,
      `${MCP_HUB} board` +
        (ws ? ` --workspace ${JSON.stringify(ws.fsPath)}` : "")
    );
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
    const [icon, color] = themeFor(agent);
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
    // LEADING clear as well as trailing. A shell echoes what is typed into it,
    // so a down agent's pane read as a raw command line followed by a status
    // report — which looks like a command that failed, not like a tab waiting to
    // be started. Clearing FIRST wipes the echo, so what remains is only the
    // hint. The trailing clear still does its own job (wiping a dead session's
    // scrollback after a detach) and is still `&&`, so a down agent — where
    // attach exits 3 — keeps the hint instead of being blanked.
    sendWhenReady(t, `clear && squad attach --no-start ${agent} && clear`);
  }

  // hideOnStartup keeps VSCode from spawning a filler terminal into a
  // restored-open empty panel (the recurring "bash <first-folder>" ghost
  // tab) — which means revealing the panel is OUR job now: once the cockpit
  // is built, show it with the board on top, without stealing focus.
  //
  // ONCE. buildCockpit() also runs from the roster watcher, and every launch
  // setting written by `squad comms/resume/launch` changes squad.conf — so
  // this line yanked the panel to the board every time the operator changed a
  // value in the settings tab ("I change any of the launch settings and it
  // takes me out of the settings and the squad-board loads instead",
  // 2026-07-28). Revealing the panel is a STARTUP concern; a rebuild triggered
  // by someone editing a setting must leave the view where they put it.
  if (!revealedOnce) {
    const boardTerm = [...vscode.window.terminals].find((t) => t.name === BOARD);
    if (boardTerm) {
      boardTerm.show(true);
      revealedOnce = true;
    }
  }
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
    confWatcher.onDidChange(() => {
      adoptPendingFolders();
      buildCockpit();
    });
    confWatcher.onDidCreate(() => {
      adoptPendingFolders();
      buildCockpit();
    });
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

module.exports = { activate, deactivate, shortLabel };
