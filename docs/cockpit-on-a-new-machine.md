# Setting up the WSL + VSCode cockpit on a new machine

**Audience: a Claude Code agent doing this setup.** Read all of Part 0 before
running anything — it decides how much of Parts 2 and 3 apply to you.

The goal: **one VSCode window, one terminal tab per Claude Code agent**, each a
persistent tmux session, driven from the tab's right-click menu (restart, stop,
answer a permission dialog, switch model, send a prompt). Sessions survive
VSCode closing; you re-attach.

This document is written from a **working reference machine** (`fireblade-wsl`).
Every version below was read off it, not recalled.

---

## Part 0 — the three layers, and which ones you want

```
LAYER 3   the MCP hub          inter-agent messaging, wake, presence glyphs
          ↑ OPTIONAL. If this machine already has its own hub, DO NOT install ours.
LAYER 2   the VSCode extension one tab per agent, context menu, live tab titles
          ↑ needs layer 1. Calls a hub BINARY for 6 verbs, all optional (see 3b).
LAYER 1   squad + tmux         the roster, the sessions, attach/restart/answer
          ↑ needs NOTHING above it
```

🔴 **Layers 1 + 2 give you the entire cockpit with no hub at all.** An agent
with no hub identity still gets a tab, attaches, restarts, answers dialogs,
switches models. Verified in the source: `squad` calls the hub binary from
**exactly one place** — `spawn_daemon()`, which starts the heartbeat daemon, and
that is pure layer 3.

**If this machine runs a different MCP hub variant, install layers 1 and 2 only.**
See Part 3 for exactly where the two can collide, because they *can*.

---

## Part 1 — the WSL + Ubuntu base

### 1.1 The distro

```
Ubuntu 24.04.1 LTS (Noble Numbat)     VERSION_ID="24.04"
kernel 5.15.167.4-microsoft-standard-WSL2
```

From an **admin PowerShell** on the Windows side:

```powershell
wsl --install --distribution Ubuntu-24.04
wsl --set-default-version 2
wsl --status                      # confirm default version 2
```

If a different Ubuntu is already installed, `wsl --list --online` shows what is
available. Match **24.04** — 22.04 ships python 3.10 and tmux 3.2a, and the
version table in 1.3 assumes 24.04's.

### 1.2 `/etc/wsl.conf` — do this early, it needs a restart

```ini
[boot]
systemd=true

[user]
default=<your-username>

[network]
hostname=<machine>-wsl
```

Then from PowerShell: `wsl --shutdown`, and reopen.

🔴 **The `hostname` line is load-bearing, not cosmetic.** Agent identity is
derived as `<repo-or-folder>-<hostname>`, so the hostname is baked into every
roster row, every tmux session name and every hub identity this machine ever
creates. Set it *before* enrolling anything. Changing it later renames every
agent and orphans their state.

🔴 **`systemd=true` is required** for the auto-restart units in Part 2.5. Verify
after the restart:
```bash
systemctl --user status >/dev/null 2>&1 && echo "systemd user session OK"
```

### 1.3 Packages

```bash
sudo apt-get update
sudo apt-get install -y \
  git tmux python3-pip python3-venv build-essential \
  rsync unzip openssh-server pkg-config autoconf
```

Reference machine has, and you should match:

| tool | version here | notes |
|---|---|---|
| git | 2.43.0 | |
| python3 | 3.12.3 | ships with 24.04 |
| tmux | 3.4 | ships with 24.04 |
| rsync | 3.2.7 | |
| node | v24.18.0 | **via nvm**, not apt |
| npm | 11.16.0 | |
| claude | 2.1.246 | at `~/.local/bin/claude` |

**Optional on the reference machine, skip unless you want them:**
- `tailscale tailscale-archive-keyring` — the fleet's private network. A work
  laptop probably must not join it; check your employer's policy first.
- `pipewire pipewire-pulse wireplumber sox alsa-utils pulseaudio-utils` — the
  `/voice` audio rig. Not needed for the cockpit.

### 1.4 node via nvm

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"
nvm install 24 && nvm alias default 24
```

### 1.5 Shell setup

`~/.bashrc` on the reference machine starts with, and needs:

```bash
export PATH="$HOME/.local/bin:$PATH"
[ -z "${LANG:-}" ] && export LANG=en_GB.UTF-8

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"
```

⚠️ **`~/.local/bin` on PATH is needed twice over** — `claude` and `squad` both
live there. Note that a *non-interactive* shell (ssh commands, systemd units,
scripts) does not read `.bashrc`, so anything automated must use absolute paths.

### 1.6 Claude Code

Install per Anthropic's current instructions; confirm it lands on PATH:
```bash
command -v claude && claude --version
```
Nothing below works without it — agents will enrol and then fail to start.

### 1.7 Where to put the tree

The reference machine keeps its repos on the **Windows** drive (`/mnt/d/Projects/...`)
so they are visible to Windows tools. That works — it is what this machine runs —
but two consequences:

- **`/mnt/*` paths are slower.** WSL2's Windows-drive mounts have real I/O
  overhead. A Linux-side path (`~/Projects`) is faster.
- **It forces the systemd drop-in in Part 2.5**, because the shipped unit files
  hardcode `%h/Projects/code/monkeypashion/mcp-hub`.

Unless you need Windows-side visibility, prefer `~/Projects`. **Decide now** —
see the "do not move the tree" trap in Part 4.

### 1.8 🔴 Dual access: edit from Windows OR from WSL Remote, one tree

This is how the reference machine is set up and it is worth reproducing. The
tree lives on the Windows drive, so **both of these open the same bytes**:

```
Windows        D:\Projects\code\monkeypashion\mcp-hub
WSL / Remote   /mnt/d/Projects/code/monkeypashion/mcp-hub
```

Edit from either side; there is no sync, no copy, no second checkout. That part
is free — it is just the WSL drive mount.

**What is NOT free, and is the reason this works at all: you need TWO venvs.**

```
.venv        POSIX   -> .venv/bin/mcp-hub          used from WSL
.venv_win    Windows -> .venv_win/Scripts/mcp-hub.exe   used from Windows
```

A virtualenv bakes absolute paths and platform-specific binaries into itself, so
a single tree **cannot** share one venv across both OSes — the Linux venv is
unusable from Windows and vice versa. Create both:

```bash
# from WSL, in the tree:
python3 -m venv .venv && .venv/bin/pip install -q -e .
```
```powershell
# from Windows PowerShell, in D:\Projects\code\monkeypashion\mcp-hub:
py -m venv .venv_win
.\.venv_win\Scripts\pip install -e .
```

⚠️ **`.gitignore` covers `.venv/` but NOT `.venv_win/`.** On the reference
machine `.venv_win/` is excluded via `.git/info/exclude`, which is **local-only
and does not travel with a clone**. On a fresh clone you must add it yourself,
or the Windows venv shows up as thousands of untracked files:

```bash
echo '.venv_win/' >> .git/info/exclude
```

### 1.9 ⚠️ The cockpit itself is WSL-Remote ONLY

Measured on the reference machine: the extension is installed under
`~/.vscode-server/extensions/` (the WSL remote extension host) and there is
**nothing** under `C:\Users\<you>\.vscode\extensions`.

| you open… | files | editing | agent tabs + right-click cockpit |
|---|---|---|---|
| **WSL Remote** window (`/mnt/d/...`) | ✅ | ✅ | ✅ |
| **native Windows** window (`D:\...`) | ✅ | ✅ | ❌ |

That asymmetry is structural, not a missing step: `squad`, tmux and the agent
sessions are all Linux-side. A native Windows window has no tmux to attach to.

⇒ **Use a WSL Remote window for cockpit work.** Open the Windows side freely for
editing, diffing, or any Windows tool — just do not expect tabs there. If you
want the Windows side to have the extension too, install it into
`C:\Users\<you>\.vscode\extensions\` as well, but it still will not find tmux,
so the tabs would be decorative. Not recommended.

⚠️ **Watch the drive.** The reference machine's `D:` is at **100% (488M free)**.
Each venv is a few hundred MB and agent transcripts accumulate; a full drive
breaks installs in confusing ways. Check before you start.

---

## Part 2 — the cockpit (layers 1 + 2)

### 2.1 Clone and install

```bash
git clone https://github.com/monkeypashion/mcp-hub.git \
  ~/Projects/code/monkeypashion/mcp-hub
cd ~/Projects/code/monkeypashion/mcp-hub
python3 -m venv .venv && .venv/bin/pip install -q -e .

mkdir -p ~/.local/bin ~/.config/squad
ln -sfn "$PWD/squad/squad" ~/.local/bin/squad
: > ~/.config/squad/squad.conf
```

⚠️ **Symlink, never copy** — `git pull` then updates the script and the
extension in one move. A copied `squad` silently goes stale.

⚠️ **Note what is NOT here:** no `~/.local/bin/mcp-hub` symlink. See Part 3.

The venv is still worth creating: `hub_bin()` prefers `<repo>/.venv/bin/mcp-hub`
over anything on PATH, so our CLI stays reachable *by full path* without ever
taking the `mcp-hub` name globally.

### 2.2 Install the VSCode extension

🔴 **The most error-prone step, and it fails silently.**

```bash
V=$(python3 -c "import json;print(json.load(open('squad/vscode-squad-terminals/package.json'))['version'])")
mkdir -p ~/.vscode-server/extensions
ln -sfn "$PWD/squad/vscode-squad-terminals" \
        ~/.vscode-server/extensions/monkeypashion.squad-terminals-$V
```

`~/.vscode-server/extensions` is correct for a Remote-WSL window (VSCode running
on Windows, connected into WSL) — which is this setup. Use `~/.vscode/extensions`
only for a native-Linux VSCode.

Then **Reload Window**.

**The rule: the installed directory must be named `<id>-<version-from-package.json>`.**
VSCode caches the extension on the **folder name**, not the version inside
`package.json`. After a `git pull` that bumps the version, the name goes stale,
VSCode reports "invalid extensions detected", never activates the extension, and
**the terminal panel does not open at all** — which reads as a cockpit bug rather
than a version bug. There is a verb for it:

```bash
squad ext-align       # run after EVERY pull, then Reload Window
```

### 2.3 The workspace file

Tabs come from **folder membership in a `.code-workspace` file** — not from an
open folder. Create `~/Projects/work.code-workspace`:

```jsonc
{
  "folders": [
    { "name": "project-a", "path": "/home/you/Projects/project-a" },
    { "name": "project-b", "path": "/home/you/Projects/project-b" }
  ],
  "settings": {
    // 🔴 LOAD-BEARING. Without these two, VSCode names tabs after the process
    // ("tmux project-a") and every glyph / model / context% the title-painter
    // pushes is silently ignored. This cost a full day of ghost-chasing once.
    "terminal.integrated.tabs.title": "${sequence}",
    "terminal.integrated.tabs.description": "${progress}",

    // Stops VSCode spawning a filler "bash <first-folder>" tab into a restored
    // empty panel. The cockpit reveals the panel itself once built.
    "terminal.integrated.hideOnStartup": "whenEmpty",

    "terminal.integrated.cwd": "/home/you/Projects"
  }
}
```

Open via **File → Open Workspace from File**. A plain folder window produces no
tabs — the extension returns early without a `workspaceFile`.

### 2.4 Enrol agents

```bash
squad add-folder ~/Projects/project-a --to ~/Projects/work.code-workspace
```

Roster is `~/.config/squad/squad.conf`, pipe-delimited, five fields:

```
agent_name | worktree | gh_config_dir | claude_args | class
project-a-yourhost|/home/you/Projects/project-a||--continue|faculty
```

- **agent_name** is derived (`<repo-or-folder>-<hostname>`, sanitised to
  `[a-z0-9_-]`). Do not hand-pick it — other code re-derives it and must agree.
- **class**: empty/`squad` = always-on (swept by `up`/`heal`); `faculty` =
  on-demand. `add-folder` gives `faculty`.
- **`--continue`** means a relaunch resumes the conversation. Keep it. `squad
  heal` **refuses to auto-restart an agent without it**, so an agent missing it
  can be detected-broken and still unrecoverable without you.

🔴 **Do NOT add `--dangerously-load-development-channels server:hub`** and do
**not** pass `--hub` to `add-folder`. Both are layer 3. The flag is inert
without a hub identity, and a roster claiming comms it does not have is a lying
instrument.

🔴 **Do NOT install the hooks** (`stop-hook`, `session-start`,
`heartbeat-daemon` in `~/.claude/settings.json`). They are layer 3 and they
would fight your existing hub. `squad/bootstrap-host` installs them — so **do
not run bootstrap-host on this machine**; use 2.1 instead, which is the same
thing minus that step.

**Reload Window.** You should now see one tab per folder.

### 2.5 Live tab titles

Glyphs and status in tab titles are pushed by a watcher. Without it the tabs
work but are inert.

```bash
mkdir -p ~/.config/systemd/user
ln -sfn "$PWD/squad/systemd/squad-who.service" ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now squad-who.service
```

🔴 **If your clone is NOT at `~/Projects/code/monkeypashion/mcp-hub`, the
symlink alone gives you a unit that loads and cannot run.** Every unit's
`ExecStart` is absolute, because systemd user units get a bare `PATH` and **will
not expand a variable in the executable position**. Add a drop-in:

```bash
mkdir -p ~/.config/systemd/user/squad-who.service.d
printf '[Service]\nExecStart=\nExecStart=%s\n' \
  "$HOME/path/to/mcp-hub/squad/squad who --watch" \
  > ~/.config/systemd/user/squad-who.service.d/override.conf
systemctl --user daemon-reload && systemctl --user restart squad-who.service
```

The empty `ExecStart=` first is **required** — it clears the inherited value
rather than appending a second command.

⚠️ **`enabled` and `firing` are not `working`.** A unit stays loaded in the
running manager after its file disappears, so listings keep showing a healthy
NEXT/LAST while every run dies `status=203/EXEC`. Checks that distinguish them:
```bash
systemctl --user list-unit-files | grep squad    # a dangling symlink reads "bad"
journalctl --user -u squad-who.service -n 20
```

---

## Part 3 — the hub question (layer 3)

### 3a. 🔴 Where our CLI and yours actually collide

My earlier advice that installing our CLI is "harmless" was **wrong**. Three
real collision surfaces:

| surface | collision |
|---|---|
| `~/.local/bin/mcp-hub` | **name collision.** `ln -sfn` would silently replace your variant's binary on PATH. |
| `~/.mcp-hub/` | shared state dir: `config.json`, machine tokens, per-agent status files |
| `~/.claude/settings.json` hooks | our `stop-hook`/`session-start`/`heartbeat-daemon` would run alongside yours |

**⇒ Do not symlink our `mcp-hub` into `~/.local/bin`, and do not install the
hooks.** Part 2.1 already omits both. The venv binary at
`<repo>/.venv/bin/mcp-hub` stays available by full path if you ever want it, and
`squad`'s `hub_bin()` prefers exactly that path over PATH — so the two never
have to meet.

### 3b. Using our CLI as an interface spec — yes, this works

The extension shells out to a **binary at a path**, so our CLI is effectively a
contract your variant can implement. The complete set of verbs the cockpit ever
invokes:

```
board [--workspace <path>]              the fleet dashboard tab
focus <minutes> --agent <name>          do-not-disturb
focus --off --agent <name>
settings --cwd <dir> --json             model rows for the quick-pick
seats logs|update|clone <agent> …       container seats      ─┐ fleet-runtime,
squads <sub>                            squad management      │ almost certainly
capsules list|attach <id> --json        capsules             ─┘ irrelevant here
```

**Only the first three are plausibly worth implementing on a laptop.** The rest
are fleet-runtime features for docker seats and multi-machine placement.

Three ways to wire it, in increasing effort:

1. **Do nothing.** Board tab and focus/settings entries error when clicked
   (`ENOENT`); every agent tab, the whole right-click menu's session verbs, and
   the live titles work perfectly. **This is the recommended starting point.**
2. **Point the extension at your binary.** `MCP_HUB` is a single `const` at the
   top of `extension.js` (`path.join(os.homedir(), ".local", "bin", "mcp-hub")`).
   Change it to your variant's path. Then whichever of the six verbs your
   variant implements will work, and the others will error.
3. **Implement the three verbs** in your variant, matching the argument shapes
   above. `mcp-hub settings --cwd <dir> --json` is the only one with a
   non-obvious contract — read `src/mcp_hub/cli.py` for the shape.

⚠️ **Option 2 without option 3 is the dangerous middle.** If your binary already
owns the name `mcp-hub` and the extension calls it with *our* arguments
(`mcp-hub board --workspace /path/x.code-workspace`), you get a foreign CLI
invoked with arguments it never agreed to. Either make the verbs match, or leave
the path pointing at nothing. Do not let it point at a stranger.

---

## Part 4 — traps, all learned the hard way

1. **Tabs show process names, not status** ⇒ workspace missing
   `tabs.title: "${sequence}"`. Part 2.3.
2. **The panel does not open at all after a `git pull`** ⇒ stale extension
   directory name. `squad ext-align`, Reload Window.
3. **Extension changes never appear** ⇒ same cause. VSCode keys the cache on the
   folder name, not on `package.json`.
4. **Do not move the tree after installing.** Absolute paths get baked into the
   venv, the systemd units, and Claude's own memory directories (keyed on an
   *encoded absolute path*). Moving it orphans agent memory silently. Decide the
   location in Part 1.7 and leave it.
5. **Changing the WSL hostname later renames every agent.** Identity derives
   from it. Set it in Part 1.2, before enrolling anything.
6. **`squad` not found in ssh commands / systemd units / scripts.**
   Non-interactive shells do not read `.bashrc`, so `~/.local/bin` is not on
   PATH. Use absolute paths there.
7. **Agents auto-starting on window open** — they should not. Tabs attach with
   `--no-start` deliberately. If yours do, check `squadTerminals.autoStart`.
8. **`systemctl --user` fails entirely** ⇒ `systemd=true` missing from
   `/etc/wsl.conf`, or you did not `wsl --shutdown` after adding it.
9. **"The cockpit stopped working" when nothing changed** ⇒ check which window
   you are in. A native Windows window on `D:\...` opens the same files with no
   tabs (Part 1.9). It looks like a broken cockpit and is a wrong-window.
10. **`pip install -e .` from Windows clobbering the Linux venv, or vice versa**
    ⇒ you used `.venv` from both sides. Two venvs, two names (Part 1.8).
11. **A full disk.** Two venvs plus transcripts add up; the reference machine's
    drive is at 100%. Failures from a full disk rarely say "disk full".

---

## Part 5 — verify

```bash
squad ls                # roster + tmux liveness. HUB column shows ? with no hub — CORRECT.
squad start <agent>
squad who               # who is waiting on you
tmux -L squad ls        # the sessions really exist
systemctl --user status squad-who.service
```

In VSCode: right-click an agent tab → **Squad** → Answer, Send prompt, Restart,
Stop, Model, Launch settings.

**Working looks like:** one tab per agent; titles showing live status; the
right-click menu acting on the agent; sessions surviving a VSCode close.

---

## What you do NOT need

`src/mcp_hub/` (beyond reading it as a reference), `seat/`, the
`squad/systemd/mcp-hub-edge*` units, capsules, placements, the hub server, and
`squad/bootstrap-host`. The cockpit is **`squad/squad` plus
`squad/vscode-squad-terminals/`**, and nothing else.
