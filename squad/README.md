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
squad ls                    # status (DEGRADED = tmux session up but claude died)
squad attach [agent]        # attach (starts/restarts if down/degraded); Ctrl-b d to detach; agent keeps running
squad cmd all "/compact"    # inject a slash-command into every agent
squad cmd [agent] "carry on"
squad restart [agent]
squad rm [agent]            # unenroll: kill session + remove from roster (keeps the repo + marker)
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
squad add dreamteam-ai-labs/browser-agent    # clone/pull + enroll in squad.conf automatically
squad restart browser-agent                  # launch it
```

`squad add` does three things: clones/pulls the repo into the right account folder, **creates the
hub-identity marker** `<worktree>/.claude/hub-agent.json` if the repo doesn't have one, and appends
the roster line (default hub args).

- **Name** precedence: explicit 2nd arg → existing marker → repo name.
- **Project** (for a newly-created marker): explicit 3rd arg → repo name.
- So `squad add dreamteam-ai-labs/dreamteam` enrols as `dreamteam` and writes `{name: dreamteam,
  project: dreamteam}`. Override: `squad add <org>/<repo> <name> <project>`.

The marker defines the agent's hub identity (what it registers as) — **commit it in that repo** so it
travels. `squad add` never overwrites an existing marker. Use `squad pull-local` if you just want to
clone a repo *without* enrolling it as an agent.

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
