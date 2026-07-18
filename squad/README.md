# squad — tmux squad orchestrator

Runs and controls a **squad** of Claude Code agents as persistent **tmux** sessions on the host
(`dev-vm-1`, or any Ubuntu box). Sessions survive terminal close / SSH drop / reboot — you re-attach.
Slash-commands (`/compact`, `/clear`) are delivered via `tmux send-keys`, which the MCP hub *cannot*
do (they're client-side keystrokes).

## Install (on the host)

```bash
# from the mcp-hub clone:
ln -sf "$PWD/squad/squad" ~/.local/bin/squad
mkdir -p ~/.config/squad
cp squad/squad.conf.example ~/.config/squad/squad.conf   # then edit the roster
```

## Use

```bash
squad up                    # start the whole squad
squad ls                    # status: tmux liveness + hub presence per agent
squad attach [agent]        # attach (starts/restarts if down/degraded); Ctrl-b d to detach; agent keeps running
squad cmd all "/compact"    # inject a slash-command into every agent
squad cmd [agent] "carry on"
squad restart [agent]
squad rm [agent]            # inverse of add: kills session, unenrolls, opts the repo out, retires its daemon
squad heal                  # nudge UP-but-offline agents to re-register (post hub-redeploy recovery)
squad down                  # stop the whole squad (kills the tmux server + keep-alive daemons)
```

**`[agent]` defaults to your current directory.** Any verb that targets one agent
(`attach`, `restart`, `cmd`, `key`, `args`, `handoff`, `rm`) derives the agent from
`$PWD` when you omit the name — so from inside a repo you just run `squad attach`,
`squad restart`, or `squad cmd "/compact"`. For `cmd`/`key`/`args` the first token is
treated as the agent only if it's a known roster name (or `all`); otherwise it's the
payload and the agent is `$PWD`'s. Run one of these outside every worktree and squad
tells you to name an agent explicitly.

## Adding an agent

```bash
squad add dreamteam-ai-labs/browser-agent      # clone/pull + opt in + enroll
squad restart browser-agent-$(hostname)        # launch it (name is derived)
```

`squad add` is the whole onboarding — and it's idempotent, so re-running it is always safe. It
clones/pulls the repo into the right account folder, opts the project into the hub (the
machine-local `~/.mcp-hub/config.json` list — maintained by `add`/`rm`, never edited by hand),
**migrates any legacy-named roster entry** for that worktree (renames the line, the live tmux
session, and retires the old daemon state), and appends the roster line if missing. `squad rm`
is the exact inverse. On machines without squad (e.g. Windows), `mcp-hub onboard` from inside
the repo does the opt-in half.

**Identity is derived, not configured** — nothing identity-related is written into the repo:

- **name** = `<repo>-<hostname>` (e.g. `mcp-hub-dev-vm-1`) — unique per clone/machine.
- **project** = `<org>/<repo>` from `git remote get-url origin` — identical for every clone of the
  repo, so clones discover each other on the hub and can DM/coordinate instead of fighting over
  one identity.

The old committed `.claude/hub-agent.json` marker is deprecated (it made every clone register as
the same agent). The hub cli still honours it as a fallback for unmigrated repos. Use
`squad pull-local` if you just want to clone a repo *without* enrolling it as an agent.

## Config

`~/.config/squad/squad.conf` — one line per agent:

```
agent_name | worktree | gh_config_dir (may be empty) | claude_args
```

Env overrides: `SQUAD_CONF` (config path), `SQUAD_SOCKET` (dedicated tmux socket, default
`squad`), `SQUAD_SLICE` (optional systemd user slice for CPU/RAM ceilings).

The tmux **session name IS the agent name** (squad runs on its own tmux socket, so no prefix is
needed). The ops shell session is `ops` — so don't name an agent `ops`.

## How it relates to the hub

- **Hub MCP tools** (`list_agents`, `send`, …) come from `~/.claude.json` user-scope config — nothing
  to do here.
- **Live channel-push wake** is the `--dangerously-load-development-channels server:hub` flag in each
  agent's `claude_args` — that's what the roster sets.
- `squad ls` reports tmux liveness; hub wakeability shows in each agent's statusline.

## Why tmux (not just `claude` in a terminal)

A `claude` launched in a VS Code / SSH terminal dies when that terminal closes. Under squad it
lives in a detached tmux session on the host — close the lid, walk away, re-attach from your phone.
