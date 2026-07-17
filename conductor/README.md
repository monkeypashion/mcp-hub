# conductor — tmux fleet orchestrator

Runs and controls a fleet of Claude Code agents as persistent **tmux** sessions on the fleet host
(`dev-vm-1`, or any Ubuntu box). Sessions survive terminal close / SSH drop / reboot — you re-attach.
Slash-commands (`/compact`, `/clear`) are delivered via `tmux send-keys`, which the MCP hub *cannot*
do (they're client-side keystrokes).

## Install (on the fleet host)

```bash
# from the mcp-hub clone:
ln -sf "$PWD/conductor/conductor" ~/.local/bin/<name>     # pick your command name
mkdir -p ~/.config/conductor
cp conductor/fleet.conf.example ~/.config/conductor/fleet.conf   # then edit the roster
```

## Use

```bash
<name> up                    # start the whole fleet + an ops shell
<name> ls                    # status (DEGRADED = tmux session up but claude died)
<name> attach <agent>        # attach (Ctrl-b d to detach; the agent keeps running)
<name> cmd all "/compact"    # inject a slash-command into every agent
<name> cmd <agent> "carry on"
<name> restart <agent>
<name> down
```

## Config

`~/.config/conductor/fleet.conf` — one line per agent:

```
agent_name | worktree | gh_config_dir (may be empty) | claude_args
```

Env overrides: `CONDUCTOR_CONF` (config path), `CONDUCTOR_PREFIX` (tmux socket + session prefix,
default `fleet`), `CONDUCTOR_SLICE` (optional systemd user slice for CPU/RAM ceilings).

## How it relates to the hub

- **Hub MCP tools** (`list_agents`, `send`, …) come from `~/.claude.json` user-scope config — nothing
  to do here.
- **Live channel-push wake** is the `--dangerously-load-development-channels server:hub` flag in each
  agent's `claude_args` — that's what the roster sets.
- `<name> ls` reports tmux liveness; hub wakeability shows in each agent's statusline.

## Why tmux (not just `claude` in a terminal)

A `claude` launched in a VS Code / SSH terminal dies when that terminal closes. Under the conductor
it lives in a detached tmux session on the host — close the lid, walk away, re-attach from your phone.
