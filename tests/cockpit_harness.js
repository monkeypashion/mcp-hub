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
    executeCommand() {
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
    onDidChangeTerminalShellIntegration: ev,
    onDidChangeActiveTerminal: ev,
    onDidCloseTerminal: ev,
    onDidOpenTerminal: ev,
  },
  workspace: {
    workspaceFile: process.env.HARNESS_WSFILE
      ? { fsPath: process.env.HARNESS_WSFILE }
      : undefined,
    workspaceFolders: [],
    getConfiguration() {
      return { get: () => undefined };
    },
    createFileSystemWatcher() {
      return { onDidChange: ev, onDidCreate: ev, onDidDelete: ev, dispose() {} };
    },
    onDidChangeWorkspaceFolders: ev,
    updateWorkspaceFolders() {
      return true;
    },
  },
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

const origRequire = Module.prototype.require;
Module.prototype.require = function (id) {
  if (id === "vscode") return vscode;
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
  if (mode === "commands") {
    console.log(JSON.stringify({ registered: registered.sort() }));
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
      // withAgents() invokes its callback WITHOUT awaiting it, so the command
      // returns before any dialog has run. Drain the microtask/timer queues
      // until nothing new arrives, rather than guessing a sleep.
      for (let i = 0, last = -1; i < 40 && last !== sent.length + shown.length; i++) {
        last = sent.length + shown.length;
        await new Promise((r) => setTimeout(r, 25));
      }
    } catch (e) {
      console.log(JSON.stringify({ error: String((e && e.message) || e), sent, shown }));
      process.exitCode = 3;
      return;
    }
    console.log(JSON.stringify({ sent, shown }));
    return;
  }
  console.log(JSON.stringify({ error: `unknown mode: ${mode}` }));
  process.exitCode = 2;
})();
