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
squad up                    # start the whole squad + an ops shell
squad ls                    # status (DEGRADED = tmux session up but claude died)
squad attach <agent>        # attach (starts it first if down); Ctrl-b d to detach; agent keeps running
squad cmd all "/compact"    # inject a slash-command into every agent
squad cmd <agent> "carry on"
squad restart <agent>
squad down
```

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
