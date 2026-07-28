#!/usr/bin/env node
// Drives the cockpit extension against a STUBBED VSCode API.
//
// Why this exists: everything about transport and teardown had been exercised
// from the command line, and the menu path — the only way the operator actually
// uses any of it — had never run once. `node --check` proves the file parses,
// which is not the same as proving a menu entry reaches a command, or that the
// command builds the shell line you think it does.
//
// Modes:
//   commands            print every command id the extension really registers
//   run <commandId>     invoke one command, print what it sent to a terminal
//
// Scripted UI answers come in via HARNESS_ANSWERS (JSON array). A quick-pick
// answer is matched as a substring of an item's label; input boxes and warning
// dialogs take the value verbatim.
const Module = require("module");
const path = require("path");

const mode = process.argv[2];
const target = process.argv[3];
const answers = JSON.parse(process.env.HARNESS_ANSWERS || "[]");
let ai = 0;
const nextAnswer = () => (ai < answers.length ? answers[ai++] : undefined);

const sent = [];
const registered = [];
const shown = [];
const D = { dispose() {} };
const ev = () => D;

const vscode = {
  commands: {
    registerCommand(id, fn) {
      registered.push(id);
      vscode._handlers[id] = fn;
      return D;
    },
    executeCommand(id) {
      // Recorded: revealing the panel is the difference between "Settings…"
      // working and appearing to do nothing whenever the Squad tab is not the
      // visible one — which is the normal case.
      executed.push(id);
      return Promise.resolve();
    },
  },
  _handlers: {},
  window: {
    terminals: [],
    activeTerminal: undefined,
    createTerminal(opts) {
      const t = {
        name: opts && opts.name,
        shellIntegration: true, // so sendWhenReady fires synchronously
        show() {},
        sendText(x) {
          sent.push(x);
        },
        dispose() {},
      };
      vscode.window.terminals.push(t);
      return t;
    },
    async showQuickPick(items) {
      const want = nextAnswer();
      const list = await items;
      // Record what was OFFERED, not just what was chosen. A picker's contents
      // are otherwise unobservable: `find` returns the first match, so a list
      // holding an entry twice is indistinguishable from one holding it once,
      // and a list that lost entries is indistinguishable from a test that
      // didn't ask for them. The 2026-07-28 flatten turned eleven menu entries
      // into rows of one list — "they are all still there" is the claim, and
      // this is the only thing that can check it.
      offered.push(list.map((i) => String((i && i.label) || i)));
      // Full items too. A settings row is label + description + SOURCE, and
      // flattening to labels throws away the two parts the panel exists to
      // show — "every value names where it came from" is unassertable against
      // a list of labels.
      picks.push(
        list.map((i) =>
          i && typeof i === "object"
            ? { label: String(i.label || ""), description: String(i.description || ""),
                detail: String(i.detail || ""), separator: i.kind === 999 }
            : { label: String(i), description: "", detail: "", separator: false }
        )
      );
      if (want === undefined) return undefined;
      return list.find((i) => String(i.label || i).includes(want));
    },
    async showInputBox() {
      return nextAnswer();
    },
    async showWarningMessage(msg, opts, ...actions) {
      shown.push(String(msg));
      const want = nextAnswer();
      if (want === undefined) return undefined;
      return actions.find((a) => String(a).includes(want)) || want;
    },
    async showInformationMessage(msg) {
      shown.push(String(msg));
      return undefined;
    },
    async showErrorMessage(msg) {
      shown.push(String(msg));
      return undefined;
    },
    async showOpenDialog() {
      return undefined;
    },
    // Recorded because "offers to CREATE the missing file" is only
    // distinguishable from the old dead-end warning by what it opens.
    async showTextDocument(doc) {
      opened.push((doc && doc.fsPath) || String(doc));
      return {};
    },
    // The settings page is a webview PANEL. Both halves are observable on
    // purpose: the rendered html IS the behaviour, and the message handler is
    // the only route by which an edit can be triggered, since the page can act
    // solely through postMessage.
    createWebviewPanel(id, title) {
      const panel = {
        title,
        webview: {
          options: {},
          set html(v) { views.push({ id, title: panel.title, html: v }); },
          get html() { return views.length ? views[views.length - 1].html : ""; },
          onDidReceiveMessage(cb) { msgHandlers.push(cb); return D; },
          postMessage() { return Promise.resolve(true); },
        },
        // Recorded so a test can prove ONE tab is retargeted rather than a new
        // one opened per click — which is invisible from the html alone.
        reveal() { revealed.push(panel.title); },
        onDidDispose() { return D; },
        dispose() {},
      };
      panels.push(panel);
      return panel;
    },
    onDidChangeTerminalShellIntegration: ev,
    onDidChangeActiveTerminal: ev,
    onDidCloseTerminal: ev,
    onDidOpenTerminal: ev,
  },
  workspace: {
    workspaceFile: process.env.HARNESS_WSFILE
      ? { fsPath: process.env.HARNESS_WSFILE }
      : undefined,
    // Env-driven so a test can put the extension in the state where it builds
    // the cockpit: the operator tabs are gated on a workspace FILE plus at
    // least one roster agent whose worktree is one of its folders. With this
    // hardcoded empty, that whole path — board and settings tabs included —
    // had never run once.
    workspaceFolders: JSON.parse(process.env.HARNESS_WSFOLDERS || "[]").map(
      (p) => ({ uri: { fsPath: p }, name: String(p).split("/").pop() })
    ),
    getConfiguration() {
      return { get: () => undefined };
    },
    async openTextDocument(uri) {
      return { fsPath: (uri && uri.fsPath) || String(uri) };
    },
    createFileSystemWatcher() {
      // Keep the callbacks so a test can fire them. With `ev` for all three the
      // roster watcher was unreachable, which left the folder-adoption path
      // uncovered — a mutant that adopted WITHOUT waiting for the roster row
      // stayed green.
      return {
        onDidChange(cb) {
          watcherCbs.push(cb);
          return D;
        },
        onDidCreate(cb) {
          watcherCbs.push(cb);
          return D;
        },
        onDidDelete: ev,
        dispose() {},
      };
    },
    onDidChangeWorkspaceFolders: ev,
    // Recorded, because "adds the folder through the API instead of editing the
    // file" is the whole behaviour under test — and the API call is the only
    // observable difference.
    updateWorkspaceFolders(start, del, ...adds) {
      folderOps.push({
        start,
        del,
        add: adds.map((a) => (a && a.uri ? a.uri.fsPath : String(a))),
      });
      return true;
    },
  },
  // Real value is 999 in the VSCode API; the harness only needs the identity
  // to hold so a separator is distinguishable from a row.
  QuickPickItemKind: { Separator: 999, Default: 0 },
  ThemeIcon: class {
    constructor(id) {
      this.id = id;
    }
  },
  ThemeColor: class {
    constructor(id) {
      this.id = id;
    }
  },
  Uri: { file: (p) => ({ fsPath: p, toString: () => p }) },
  ViewColumn: { Active: -1, One: 1, Beside: -2 },
  RelativePattern: class {
    constructor(base, pattern) {
      this.base = base;
      this.pattern = pattern;
    }
  },
  EventEmitter: class {
    constructor() {
      this.event = ev;
    }
    fire() {}
    dispose() {}
  },
};

// Some commands TYPE into the tab (attaching is a property of that terminal)
// and others run in the BACKGROUND via squadExec -> child_process.execFile.
// Capturing only the first made background commands look like no-ops, so stub
// child_process too and record both. `execSync` stays real: the preview/dry-run
// paths legitimately shell out and expect output.
const execs = [];
const offered = [];
const picks = [];
const views = [];
const panels = [];
const revealed = [];
let settingsCalls = 0;
const msgHandlers = [];
const executed = [];
const opened = [];
const folderOps = [];
const watcherCbs = [];
const realCp = require("child_process");
const cpStub = {
  ...realCp,
  execFile(file, args, opts, cb) {
    execs.push([file, ...(Array.isArray(args) ? args : [])].join(" "));
    const done = typeof opts === "function" ? opts : cb;
    // `squad attached <a>` is a PROBE whose exit code picks the start path, so
    // a stub that always succeeds makes only the attached branch reachable —
    // which is how the typed path silently stopped being covered.
    const isProbe = Array.isArray(args) && args[0] === "attached";
    const notAttached = isProbe && process.env.HARNESS_NOT_ATTACHED;
    // `mcp-hub settings --json` is READ, not fire-and-forget: the settings panel
    // parses its stdout, so a stub that always answered "" made the only
    // realistic path unreachable and every settings test exercised the
    // unparseable-output branch instead.
    const isSettings = Array.isArray(args) && args[0] === "settings";
    // Per-call output and latency, so a test can stage the ORDER replies come
    // back in. Two quick clicks race, and the guard under test only matters
    // when the FIRST call is the slower one — which cannot happen with a stub
    // that answers everything instantly and identically.
    if (isSettings) settingsCalls += 1;
    const nth = settingsCalls;
    const settingsOut =
      nth > 1 && process.env.HARNESS_SETTINGS_OUT2
        ? process.env.HARNESS_SETTINGS_OUT2
        : process.env.HARNESS_SETTINGS_OUT || "";
    const delay =
      nth === 1 ? Number(process.env.HARNESS_SETTINGS_DELAY_FIRST || 0) : 0;
    if (isSettings && delay > 0 && typeof done === "function") {
      setTimeout(() => done(null, settingsOut, ""), delay);
      return { on() {}, unref() {} };
    }
    if (typeof done === "function") {
      if (isSettings && process.env.HARNESS_SETTINGS_FAIL) {
        // stdout is emitted too when a test supplies it: a command can print
        // usable output and STILL exit non-zero, and that is the only case
        // where the error branch's early return is load-bearing rather than
        // masked by the unparseable-output guard behind it.
        done(Object.assign(new Error("exit 1"), { code: 1 }),
             process.env.HARNESS_SETTINGS_OUT || "",
             process.env.HARNESS_SETTINGS_FAIL);
      } else {
        done(notAttached ? Object.assign(new Error("exit 1"), { code: 1 }) : null,
             isSettings ? settingsOut : "", "");
      }
    }
    return { on() {}, unref() {} };
  },
  spawn(file, args) {
    execs.push([file, ...(Array.isArray(args) ? args : [])].join(" "));
    return { on() {}, unref() {}, stdout: { on() {} }, stderr: { on() {} } };
  },
  // Duplicate asks squad WHERE the copy will land before starting it, so this
  // has to be stubbed or the test shells out to the real squad and depends on a
  // sandbox worktree existing. HARNESS_EXEC_FAIL makes it throw the way a
  // refused gate does, with the reason on stdout as execFileSync reports it.
  execFileSync(file, args) {
    execs.push([file, ...(Array.isArray(args) ? args : [])].join(" "));
    if (process.env.HARNESS_EXEC_FAIL) {
      const e = new Error("command failed");
      e.stdout = process.env.HARNESS_EXEC_FAIL;
      throw e;
    }
    return process.env.HARNESS_EXEC_OUT || "";
  },
};

const origRequire = Module.prototype.require;
Module.prototype.require = function (id) {
  if (id === "vscode") return vscode;
  if (id === "child_process") return cpStub;
  return origRequire.apply(this, arguments);
};

// HARNESS_EXT lets a mutation check point this at a deliberately-broken copy —
// a green test proves nothing until you have watched it fail.
const extPath = process.env.HARNESS_EXT || path.join(
  __dirname, "..", "squad", "vscode-squad-terminals", "extension.js"
);
const ext = require(extPath);
ext.activate({ subscriptions: [] });


(async () => {
  if (mode === "shortlabel") {
    // The display rule is mirrored in squad's short_label(); this exposes the JS
    // side so a test can prove the two agree instead of hoping.
    console.log(JSON.stringify({ label: ext.shortLabel(target) }));
    return;
  }
  if (mode === "commands") {
    console.log(JSON.stringify({ registered: registered.sort(), views }));
    return;
  }
  if (mode === "run") {
    const fn = vscode._handlers[target];
    if (!fn) {
      console.log(JSON.stringify({ error: `no such command: ${target}` }));
      process.exitCode = 2;
      return;
    }
    // A right-click passes the clicked terminal as the first argument, and
    // resolveAgent maps it back to a roster row by its NAME. HARNESS_TERMINAL
    // stands in for that click.
    const clicked = process.env.HARNESS_TERMINAL
      ? { name: process.env.HARNESS_TERMINAL, show() {}, sendText(x) { sent.push(x); },
          shellIntegration: true, dispose() {} }
      : undefined;
    try {
      await fn(clicked);
      // A second click on the same command. "One tab, retargeted" is a claim
      // about what the SECOND invocation does, and is invisible to any test
      // that only ever clicks once.
      if (process.env.HARNESS_RUN_TWICE) {
        await fn(clicked);
        for (let i = 0; i < 30; i++) await new Promise((r) => setTimeout(r, 25));
      }
      // withAgents() invokes its callback WITHOUT awaiting it, so the command
      // returns before any dialog has run. Drain the microtask/timer queues
      // until nothing new arrives, rather than guessing a sleep.
      for (let i = 0, last = -1; i < 40 && last !== sent.length + shown.length + execs.length; i++) {
        last = sent.length + shown.length + execs.length;
        await new Promise((r) => setTimeout(r, 25));
      }
    } catch (e) {
      console.log(JSON.stringify({ error: String((e && e.message) || e), sent, shown, execs, offered, opened, picks, views, executed, panelCount: panels.length, revealed, folderOps }));
      process.exitCode = 3;
      return;
    }
    // Simulates the operator changing a dropdown in the rendered page. The
    // page can only act through postMessage, so this is the ONLY route by
    // which an edit is reachable — without it the whole editable half of the
    // panel is untestable.
    if (process.env.HARNESS_WEBVIEW_MSG) {
      for (const cb of msgHandlers) {
        try {
          await cb(JSON.parse(process.env.HARNESS_WEBVIEW_MSG));
        } catch (e) {
          shown.push(`message handler threw: ${String((e && e.message) || e)}`);
        }
      }
      for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 25));
    }
    if (process.env.HARNESS_FIRE_ROSTER) {
      for (const cb of watcherCbs) {
        try {
          await cb();
        } catch (e) {
          shown.push(`watcher threw: ${String((e && e.message) || e)}`);
        }
      }
      for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 25));
    }
    console.log(JSON.stringify({ sent, shown, execs, offered, opened, picks, views, executed, panelCount: panels.length, revealed, folderOps }));
    return;
  }
  console.log(JSON.stringify({ error: `unknown mode: ${mode}` }));
  process.exitCode = 2;
})();
