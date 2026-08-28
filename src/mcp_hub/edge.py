"""Edge realizer brain — `mcp-hub edge apply`.

The hub stores desired state; this module decides what a machine should DO
about it and what it may truthfully REPORT back.

**The unit is a managed thing on a machine, not an agent.** Worktree seats
(tmux + claude) and containers (a squad, a web app, an inference server) are
the same shape to the planner; they differ only in their EXECUTOR and in how
they are ENUMERATED. That is why `plan()` never mentions docker: adding a
substrate is adding an executor, not editing the brain. An agent seat is
simply a unit that additionally carries memory and a harvest step.

Three properties are load-bearing and tested:

- plan() diffs desired against ENUMERATED local state and emits ordered
  actions, carrying the substrate so the caller can pick an executor. An
  UNKNOWN substrate is still refused loudly rather than guessed at.
- discover_workspaces() reports every .code-workspace it finds, including
  unparseable ones — the operator's "never lose track of workspaces"
  requirement means a broken file is reported-with-error, not dropped.
- observed_report() derives state from enumeration alone. It refuses an
  empty enumeration: no evidence means "unknown", and unknown must be an
  error, not a default (evidence contract ①).

Execution (running the actual squad commands) is deliberately elsewhere and
injected — the brain never shells out, so no test of it can touch a roster.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from mcp_hub.spec_guard import (
    SEAT_STATE_DIR,
    check_credential_policy,
    check_repo_mount,
    check_volumes,
)

# Where the seat credentials live on an edge host. The systemd unit reads it
# via `EnvironmentFile=-%h/.mcp-hub/edge-env`; the CLI reads it through
# load_env_file so a hand-run behaves the same way. chmod 600, VALUES inside —
# which is why the hub only ever stores the NAMES.
EDGE_ENV_FILE = Path.home() / ".mcp-hub" / "edge-env"

# SEAT_STATE_DIR — where a seat's claude state lives INSIDE the container
# (memory, transcripts, credentials cache). The image's user is `seat`
# (seat/Dockerfile), so it is that user's ~/.claude and nothing here may derive
# it from the EDGE host's HOME. Destination for a `memory_volume` given as a
# bare name. Defined in spec_guard because the guard needs it too — a
# `repo_mount` landing there would shadow the memory volume.

# The managed root for host-side checkouts (docs/seat-repo-access.md). The
# operator names a REPO; this machine decides the PATH. That split is what
# makes "a checkout outside the managed root" unreachable rather than
# forbidden — there is no operator-supplied host path anywhere in the flow.
#
# Per-SEAT, not per-repo: two seats assigned the same repo must not share a
# working tree, or their index and checked-out ref fight silently.
SEAT_REPOS_ROOT = Path.home() / ".mcp-hub" / "seat-repos"


def _squad_bin() -> str | None:
    """`squad`'s absolute path, found the way every other caller finds it.

    🔴 NOT `shutil.which`. `squad` on PATH is an INTERACTIVE-shell assumption:
    systemd timers get a bare PATH with no `~/.local/bin`, and this edge is
    meant to run from a timer — `edge apply` already died there once on a raw
    FileNotFoundError for exactly this. Here the failure would have been worse
    than a traceback: `which` returns None under the timer, enrolment reports a
    tidy "no squad on this machine", and the seat silently gets no audio. That
    is the bug this whole function exists to fix, reintroduced one layer down.

    A module-level indirection so tests can pin it and never consult the real
    machine's PATH.
    """
    from mcp_hub.cli import _resolve_tool

    return _resolve_tool("squad")


def repo_mount_dir(seat: str, repo: str,
                   root: Path | None = None) -> Path:
    """Host directory holding `repo`'s checkout for `seat`.

    Both components are already validated by `check_repo_mount` before
    anything calls this — the guard refuses a repo that is not a plain
    `org/name`, so nothing here can climb out of the root.
    """
    org, _, name = repo.partition("/")
    return (root or SEAT_REPOS_ROOT) / seat / org / name


def injected_credentials(spec: dict[str, Any]) -> list[str]:
    """The credential NAMES this container actually receives.

    🔴 THE POINT OF `repo_mount`: a seat whose code is mounted from the host
    has no reason to hold a GitHub credential, and holding one is the whole
    exposure the design removes. The host clones — where the token already
    lives, in this machine's own environment — and the container receives a
    DIRECTORY.

    Dropped here, at the one place the value would enter the container, rather
    than asked of whoever writes the spec: a spec that still names the token
    (every existing one does) must not be able to smuggle it back in, and
    editing every spec would be a migration that could be half-done.
    """
    from mcp_hub.seat import SEAT_GITHUB_TOKEN

    wanted = [str(n) for n in (spec.get("env_from_host") or [])]
    if spec.get("repo_mount") or spec.get("extra_repo_mounts"):
        return [n for n in wanted if n != SEAT_GITHUB_TOKEN]
    return wanted


def repo_mount_argv(seat: str, repo_mount: dict[str, Any],
                    root: Path | None = None) -> list[list[str]]:
    """Git commands that bring the host checkout to the wanted state.

    Clone when absent, fetch-and-reset when present: an assigned repo changes
    per build, so the second and later materializations of a seat must be able
    to MOVE the checkout rather than only create it. `reset --hard` is right
    here precisely because this tree is the edge's, not a human's — the seat
    cannot push, so there is no work in it that origin does not already have
    (docs/seat-repo-access.md, "Named limits").
    """
    from mcp_hub.seat import credential_helper_value, https_repo_url

    repo = str(repo_mount.get("repo") or "")
    ref = str(repo_mount.get("ref") or "").strip()
    dest = repo_mount_dir(seat, repo, root)
    url = https_repo_url(repo)
    # THE CREDENTIAL, ON THE HOST — and the reason this feature works at all.
    #
    # Measured 2026-08-11 against the live private repo: a bare `git clone`
    # from the edge host fails `could not read Username`, because the host has
    # no credential helper configured and nothing here supplied one. The
    # design said "the host clones, where the credential already lives" and
    # the first implementation never handed it to git — declared, not
    # enforced, in the very change that closes an instance of that shape.
    #
    # `-c` rather than `git config`: nothing is written to the host's git
    # configuration, and the helper string carries the literal `${VAR}` (see
    # credential_helper_value), so the VALUE never appears in an argv, a log
    # line, or an error message. Verified on the same live clone: the token
    # appears nowhere in the resulting `.git/config`.
    cred = ["-c", f"credential.helper={credential_helper_value()}"]
    # A 40-hex ref is a COMMIT PIN, not a branch: `clone --branch <sha>`
    # and `checkout origin/<sha>` both fail on real git (a sha is not a
    # remote ref). POC-2's pins are exactly this shape, so the argv
    # branches on it rather than leaving the seat a git error.
    is_sha = bool(re.fullmatch(r"[0-9a-f]{40}", ref))
    if (dest / ".git").is_dir():
        base = ["git", *cred, "-C", str(dest)]
        cmds = [[*base, "fetch", "--prune", "origin"]]
        if is_sha:
            cmds.append([*base, "checkout", "--detach", ref])
        elif ref:
            cmds.append([*base, "checkout", "--detach", f"origin/{ref}"])
        else:
            cmds.append([*base, "reset", "--hard", "origin/HEAD"])
        return cmds
    argv = ["git", *cred, "clone", url, str(dest)]
    if ref and not is_sha:
        # `--branch` takes a branch or a tag, which is what a `ref` is in
        # every case this feature has: an operator assigning a build.
        argv[-2:-2] = ["--branch", ref]
    cmds = [argv]
    if is_sha:
        cmds.append(["git", *cred, "-C", str(dest),
                     "checkout", "--detach", ref])
    return cmds

# The image's WORKDIR, and the root a pod hangs its per-agent workdirs under
# (`<root>/<identity>`). Harvest targets these with `docker exec -w`, because
# `memory-export` resolves identity from its cwd — so the path IS the choice of
# which agent is being harvested. Must match seat/Dockerfile.
SEAT_WORK_DIR = "/home/seat/work"


def load_env_file(path: Path) -> dict[str, str]:
    """Parse systemd's EnvironmentFile dialect. Missing file → {}.

    Narrow on purpose: `KEY=VALUE` per line, `#` comments, one optional layer
    of surrounding quotes. systemd supports more (line continuations, `$`
    expansion) and this does not — an edge-env that needs those is better
    simplified than half-understood, because a MISREAD credential is worse
    than an absent one: it produces a container that starts and fails
    somewhere else.

    A malformed line is skipped, not fatal. This runs on the path that keeps
    seats alive, and refusing the whole file over one bad line would take the
    good credentials down with it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def apply_env_file(environ: dict[str, str], loaded: dict[str, str]) -> list[str]:
    """Fill in names the environment doesn't already have. Returns the names
    it supplied (never the values — this list gets printed).

    An ambient NON-EMPTY value wins: an operator who exported something did so
    deliberately. An ambient EMPTY one does not, because an empty export
    clobbering a good credential is a real incident shape (docs/seat-image.md)
    and a var that is present-but-empty is indistinguishable from that.
    """
    supplied = []
    for key, value in loaded.items():
        if not environ.get(key) and value:
            environ[key] = value
            supplied.append(key)
    return sorted(supplied)


def plan(
    placements: list[dict[str, Any]],
    local_seats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Diff desired placements against enumerated local seat state.

    `local_seats` maps seat identity -> {"materialized": bool,
    "running": bool} as ENUMERATED by the caller (squad ls: roster
    enrollment + tmux liveness), never read from any record. "materialized"
    means ENROLLED — a folder on disk that squad doesn't know is not
    startable, which is why folder existence was the wrong signal (found the
    hour before the first live run, via a repo that existed unenrolled).
    """
    actions: list[dict[str, Any]] = []
    for p in placements:
        seat = p["seat"]
        substrate = p.get("substrate", "worktree")
        base = {"placement": p["id"], "seat": seat, "substrate": substrate}
        if substrate not in ("worktree", "docker"):
            actions.append(
                {
                    **base,
                    "op": "skip",
                    "reason": f"substrate '{substrate}' not realizable by this edge",
                }
            )
            continue
        local = local_seats.get(seat, {"materialized": False, "running": False})
        desired = p["desired"]
        if desired == "reclaimed":
            if not local["materialized"]:
                # ALREADY GONE — the reclaim finished, possibly passes ago.
                # Without this the three steps below were re-planned on every
                # single pass forever: `edge-probe-dev-vm-1` and
                # `claude-seat-dev-vm-1` were still being harvested, verified
                # and destroyed every two minutes on 2026-08-06, long after
                # their containers ceased to exist.
                #
                # Absence is sound evidence HERE and only here: enumeration
                # RAISES when it cannot see the substrate, so "not
                # materialized" means we looked and it was not there — never
                # "we could not look". A reclaim is also the one desired state
                # whose completion IS an absence, so there is nothing else to
                # wait for.
                #
                # Skipping harvest with it is right too: `docker exec` into a
                # container that does not exist cannot preserve anything, so
                # the alternative is three commands that all fail while
                # claiming to protect memory.
                continue
            # Harvest before destroy, always: the memory delta is work
            # product, and a clone whose learnings die with the substrate is
            # the vacuous green of scheduling.
            actions.extend(
                [
                    {**base, "op": "harvest"},
                    {**base, "op": "verify"},
                    {**base, "op": "destroy"},
                ]
            )
        elif desired == "running":
            if not local["materialized"]:
                actions.append({**base, "op": "materialize"})
                # A just-materialized seat has no history: --continue would
                # exit ("No conversation found to continue", live 2026-07-29).
                actions.append({**base, "op": "start", "fresh": True})
            elif not local["running"]:
                actions.append({**base, "op": "start"})
        elif desired == "ran":
            # HEADLESS terminal state: run ONCE, ever. Materialize if absent;
            # an exited container is deliberately NOT restarted — restart is
            # exactly the re-run-the-errand bug this state exists to prevent.
            # The report says how it went (completed converges, failed
            # diverges loudly); acting on a failure is the operator's call,
            # not the reconciler's — a retry loop on a deterministic failure
            # is 30 wasted runs a night, invisibly.
            #
            # A container stuck at `created` (materialized, start failed)
            # also plans nothing and is deliberately LEFT TO DIVERGE: no
            # exit code is enumerated for `created`, so observed reads
            # `stopped` against desired `ran` — loud, and honest about a
            # start this edge could not confirm ever ran. Auto-starting it
            # here would risk the re-run this state exists to prevent.
            if not local["materialized"]:
                actions.append({**base, "op": "materialize"})
                actions.append({**base, "op": "start", "fresh": True})
        elif desired == "stopped":
            if local["running"]:
                actions.append({**base, "op": "stop"})
    return actions


def live_sessions(runner: Any) -> set[str] | None:
    """Agent names with a live pane, or None when we could not look.

    squad runs its panes on a DEDICATED tmux socket (`-L squad`) with the
    session named for the agent, so this is a structured question with a
    structured answer — no parsing of `squad ls`'s rendered table, which is
    the habit that put repo basenames in the board.

    None, not the empty set, on failure. `tmux ls` also exits non-zero when
    there is genuinely no server, so the two cases are indistinguishable from
    the exit code — and guessing "nothing is running" is the dangerous half:
    it would quietly clear every warning on the board. Unknown keeps the
    weaker claim.
    """
    rc, out = runner(["tmux", "-L", "squad", "ls", "-F", "#{session_name}"])
    if rc != 0:
        return None
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def host_tmux_sessions(runner: Any,
                       socket_dir: Path | None = None
                       ) -> dict[str, set[str]] | None:
    """Session names per socket for EVERY tmux server on the box — or None
    when we could not look.

    A reclaim's absence evidence used to be the substrate's PRIMARY artifact
    only — the container for docker, the roster row for a worktree seat. Five
    sessions wearing seat names then ran 4-6 days on a socket every reader
    could list, because each instrument iterated the KNOWN and asked about
    it: not roster rows (squad rm had deleted those), not tmux placements
    (their rows were docker), honestly absent as containers (dt's sweep,
    2026-08-12). This helper is the inverse — enumerate the box, let the
    caller subtract the known.

    Three answers, kept distinct on fb's rule that UNKNOWN ≠ ABSENT:
      {}    measured empty — no socket dir means no servers, a real negative
      {...} measured contents; a dead socket file ("no server running") is a
            measured-empty server, not a failure
      None  could not look — a listing that failed for any other reason.
            Callers must treat this as unknown, never as clean.
    """
    d = socket_dir if socket_dir is not None \
        else Path(f"/tmp/tmux-{os.getuid()}")
    if not d.is_dir():
        return {}
    try:
        socks = sorted(p for p in d.iterdir() if p.is_socket())
    except OSError:
        return None
    out: dict[str, set[str]] = {}
    for s in socks:
        rc, o = runner(["tmux", "-S", str(s), "ls", "-F", "#{session_name}"])
        if rc != 0:
            if "no server" in o.lower():
                continue
            return None
        out[s.name] = {ln.strip() for ln in o.splitlines() if ln.strip()}
    return out


def local_roster(runner: Any = None) -> list[dict[str, Any]]:
    """This machine's roster — {agent, worktree, comms, running?}.

    Imported lazily: `edge` is the realizer and `cli` is the command surface,
    and importing the latter at module scope makes a cycle. Failure is an
    EMPTY list rather than a raise, because a machine that cannot read its own
    roster must still report its workspaces and observations — but note that
    the caller sends the key regardless, so an unreadable roster is reported as
    "no agents", not as "no roster reported". That is the honest way round: an
    edge that IS reporting should not be treated as one that cannot.

    `comms` and `running` are what let the board tell an agent that SHOULD be
    on the hub and isn't from one that was never going to be. Without them
    every enrolled folder on a box became a warning the moment off-hub rows
    started rendering — dev-vm-1 raised twenty, of which exactly one was
    actionable, and a warning on twenty rows is a warning on none.

    - `comms`: its launch args carry the hub channels flag. A plain scratch
      folder has none and is never expected on the hub, so it must never
      warn (`squad add-folder` omits the flag deliberately — it is inert
      without a hub identity).
    - `running`: OMITTED when liveness could not be read, never defaulted.
      An absent key is unknown; `False` would be a claim that the agent is
      down, which is exactly the false calm this is meant to avoid.
    """
    try:
        from mcp_hub.cli import _roster_all

        live = live_sessions(runner) if runner is not None else None
        out: list[dict[str, Any]] = []
        for r in _roster_all():
            if not r.get("agent"):
                continue
            row: dict[str, Any] = {
                "agent": r["agent"],
                "worktree": r.get("worktree", ""),
                "comms": "server:hub" in (r.get("args") or ""),
            }
            if live is not None:
                row["running"] = r["agent"] in live
            out.append(row)
        return out
    except Exception:  # noqa: BLE001 — the edge never dies of a bad roster
        return []


def discover_workspaces(scan_dirs: list[Path]) -> list[dict[str, Any]]:
    """Enumerate .code-workspace files under the given directories (flat).

    Every file found is reported — a workspace whose JSONC fails to parse is
    returned with an `error` field rather than silently dropped, because the
    registry's whole point is that nothing gets lost track of.
    """
    found: list[dict[str, Any]] = []
    for d in scan_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.code-workspace")):
            entry: dict[str, Any] = {"path": str(f)}
            try:
                raw = re.sub(r"//[^\n]*", "", f.read_text(encoding="utf-8"))
                data = json.loads(raw)
                entry["folders"] = len(data.get("folders", []))
            except (OSError, json.JSONDecodeError) as e:
                entry["error"] = str(e)
            found.append(entry)
    return found


def observed_report(
    placement: dict[str, Any], enumeration: dict[str, Any]
) -> dict[str, Any]:
    """Build the observed-state report for one placement from enumeration.

    State comes ONLY from what was enumerated — never from the placement's
    own `desired` field. An empty enumeration is refused: it would make the
    report an assertion over an empty set.
    """
    if not enumeration:
        raise ValueError(
            f"empty enumeration for placement {placement['id']}: refusing to "
            "report a state no evidence supports"
        )
    state = "running" if enumeration.get("alive") else "stopped"
    # A HEADLESS seat that exited has FINISHED, not stopped. Without this,
    # "finished cleanly" == "crashed" == "never started" — all `stopped` —
    # and against desired=running the remedy (start) would RE-RUN the errand.
    # The exit code is the discriminator: 0 completed, anything else failed
    # (including the door's own 42/43/124, which are failures with names).
    # `exit_code` is only enumerated for containers docker says are `exited`,
    # so a merely-created one cannot read as completed here.
    if (enumeration.get("headless") and not enumeration.get("alive")
            and "exit_code" in enumeration):
        state = "completed" if enumeration["exit_code"] == 0 else "failed"
    # A COMPLETED reclaim. Without this a successful harvest-then-destroy
    # reports `saw stopped` against `want reclaimed` — diverged forever,
    # a finished job that looks like a failed one (measured on the live
    # busybox probe). Absence is the evidence: we enumerated and it was
    # not there. Absence WITHOUT a reclaim request stays `stopped`, or a
    # container something else destroyed would be quietly absolved.
    #
    # Both substrates' absence keys are read — docker says `exists`,
    # worktree says `enrolled` — or reclaim would look complete for
    # containers and permanently diverged for tmux seats.
    if placement.get("desired") == "reclaimed":
        present = enumeration.get("exists", enumeration.get("enrolled"))
        # ⚠️ ABSENCE NEEDS BOTH FACTS FALSE, and each `is False` is
        # deliberate (UNKNOWN ≠ ABSENT, in every clause):
        #
        #   present — the substrate's primary artifact is gone.
        #   alive   — nothing by this name is RUNNING. The old check read
        #             `present` alone, and for a worktree seat `present` is
        #             the ROSTER ROW — a record, deleted by `squad rm`
        #             before its kill-session (whose failure it swallows).
        #             A visibly-running session was reported `reclaimed`
        #             while `alive: True` sat in this same dict, proven by
        #             execution 2026-08-12. A record asserts; only the
        #             process measurement is evidence of destruction.
        #
        #   host_sessions — the box-wide sweep (host_tmux_sessions): a
        #             session wearing the seat's name on ANY socket means
        #             the seat's footprint outlives its primary artifact —
        #             the five 6-day survivors were exactly this, honest
        #             container-absence with seat-named sessions running.
        #             Non-empty -> `leftover`, a diverged state the board
        #             surfaces, never silently converged. The
        #             `host_sessions_unknown` flag is a sweep that FAILED:
        #             we could not look everywhere, so nothing is granted.
        #             Key absent entirely = an edge that didn't sweep (old
        #             pass shape) — verdict falls back to the two facts
        #             above rather than refusing retroactively.
        if present is False and enumeration.get("alive") is False:
            if enumeration.get("host_sessions"):
                state = "leftover"
            elif not enumeration.get("host_sessions_unknown"):
                state = "reclaimed"
    # A container running the WRONG IMAGE is not converged, and "running"
    # would be true-but-useless: `docker ps` cannot see the difference, so
    # the drift would stay invisible forever (it bit this build for real —
    # the edge recreated a seat from a stale `latest` 13 seconds before the
    # new image finished building). The hub marks any observed != desired as
    # diverged, so naming the state is the whole mechanism.
    #
    # Only when it is actually UP: drift is about what it runs, and a
    # stopped container reporting stale-image would hide that it is down.
    # `is False` deliberately — an ABSENT key is unknown, not a mismatch.
    if state == "running" and enumeration.get("image_matches") is False:
        state = "stale-image"
    return {"state": state, "enumeration": enumeration}


def seed_first_launch(folder: str, claude_json: Path | None = None) -> bool:
    """Pre-authorize a materialized seat's first launch — transport's rule,
    inherited: the placement IS the operator's explicit trust act, so seed
    folder trust + the hub MCP approval instead of parking the first launch
    on dialogs nobody is watching (all three seams observed live 2026-07-29).

    ⚠️ BELT AND BRACES, NOT THE MECHANISM. `~/.claude.json` is session STATE
    and sessions rewrite it, so the `enabledMcpjsonServers` half of this does
    not survive. Measured 2026-08-04: of the paths on this box that actually
    HAVE a `.mcp.json`, 3 of 4 had lost the flag — including one transport
    seeded itself. dev-vm-1 saw 1 of ~30 survive.

    The durable form is `"enabledMcpjsonServers": ["hub"]` in USER-SCOPE
    `~/.claude/settings.json`, which is config rather than state and
    pre-approves by name even in untrusted folders. Its trade is that it
    approves any server NAMED hub anywhere on the box — fine while every
    `.mcp.json` is generated by our tooling, and a per-box operator decision
    rather than something this function should assume.

    Keep this anyway: on a box without that line it is the only thing that
    works, and where the line exists it is harmless. Just do not believe it
    is what makes first launch work.

    (Careful reading the evidence: an absent flag only means a LOST approval
    where a `.mcp.json` exists. Most tracked paths have none, and counting
    those makes the problem look far worse than it is.)

    Missing file: ours to create. Unparseable file: NEVER clobber — fail
    open, return False, the operator answers one dialog instead of losing
    their settings.
    """
    import os
    import tempfile

    p = claude_json or (Path.home() / ".claude.json")
    if not p.exists():
        data: dict[str, Any] = {}
    else:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            assert isinstance(data, dict)
        except Exception:  # noqa: BLE001 — any unreadable shape: hands off
            return False
    entry = data.setdefault("projects", {}).setdefault(folder, {})
    entry["hasTrustDialogAccepted"] = True
    enabled = entry.get("enabledMcpjsonServers")
    enabled = enabled if isinstance(enabled, list) else []
    if "hub" not in enabled:
        enabled.append("hub")
    entry["enabledMcpjsonServers"] = enabled
    entry.setdefault("allowedTools", [])
    entry.setdefault("disabledMcpjsonServers", [])
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".claude.json.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, p)  # live file: replace atomically
    return True


class HubAPI:
    """Thin client for the machine-facing slice of /api/v1.

    Accepts an injected client (any object with .get/.post taking
    headers/json — a starlette TestClient in tests, an httpx.Client in
    production) so the full loop is testable against the real API in-process
    without a socket.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str = "",
        client: Any = None,
    ) -> None:
        if client is None:
            import httpx

            client = httpx.Client(base_url=base_url or "", timeout=30)
        self._c = client
        self._h = {"Authorization": f"Bearer {token}"}

    def pull_placements(self, machine: str) -> list[dict[str, Any]]:
        r = self._c.get(
            f"/api/v1/machines/{machine}/placements", headers=self._h
        )
        r.raise_for_status()
        return r.json()["placements"]

    def push_observed(self, placement_id: str, report: dict[str, Any]) -> dict:
        r = self._c.post(
            f"/api/v1/placements/{placement_id}/observed",
            headers=self._h,
            json=report,
        )
        r.raise_for_status()
        return r.json()

    def push_status(self, machine: str, payload: dict[str, Any]) -> None:
        r = self._c.post(
            f"/api/v1/machines/{machine}/status", headers=self._h, json=payload
        )
        r.raise_for_status()

    # -- seat control plane (cards #144/#152) -------------------------------

    def pull_seats(self, machine: str) -> list[dict[str, Any]]:
        """This machine's seat declarations, spec included — the lane leg's
        discovery door. /seats itself is operator-only, and lane seats have
        no placement to carry their spec in pull_placements."""
        r = self._c.get(
            f"/api/v1/machines/{machine}/seats", headers=self._h
        )
        r.raise_for_status()
        return r.json().get("seats", [])

    def pull_seat_actions(self, seat: str) -> list[dict[str, Any]]:
        r = self._c.get(f"/api/v1/seats/{seat}/actions", headers=self._h)
        r.raise_for_status()
        return r.json()["actions"]

    def report_seat_action(
        self, seat: str, action_id: int, report: dict[str, Any]
    ) -> None:
        r = self._c.patch(
            f"/api/v1/seats/{seat}/actions/{action_id}",
            headers=self._h, json=report,
        )
        r.raise_for_status()

    def seat_watched(self, seat: str) -> bool:
        r = self._c.get(f"/api/v1/seats/{seat}/watch", headers=self._h)
        r.raise_for_status()
        return bool(r.json().get("watching"))

    def push_seat_pane(self, seat: str, pane: str) -> None:
        r = self._c.post(
            f"/api/v1/seats/{seat}/view", headers=self._h, json={"pane": pane}
        )
        r.raise_for_status()


# -- seat control plane, edge leg (cards #144/#152, phase 1) ----------------
#
# The hub records INTENT; this is where a machine carries it out on its own
# seats and reports what it OBSERVED. Phase 1 is `interrupt` and `prompt`.
#
# Everything goes through the injected runner, exactly like SquadExecutor,
# and for a sharper reason: this code sends KEYSTROKES to a live agent's
# terminal. No import of subprocess here means no test path can type into a
# real seat.

_SEAT_PHASE1_VERBS = ("interrupt", "prompt")


def _seat_tmux_argv(seat: dict[str, Any], args: list[str]) -> list[str]:
    """tmux argv for this seat, on the host or inside its container.

    A docker seat's pane lives INSIDE the container, so host tmux would
    either miss it or — worse — hit a same-named host session. The substrate
    decides the door; it is never inferred from the session name.
    """
    session = seat.get("session") or "seat"
    # `-t` goes IMMEDIATELY after the subcommand, before any other argument.
    # tmux stops parsing options at `-l`: everything after it is literal
    # keys, so a trailing `-t <session>` is TYPED — into whichever session
    # tmux considers current, which is some other lane's pane. Measured
    # 2026-08-28: three console fires at one lane put the prompt text plus
    # the string `-tmcp-hub-dev-vm-1` into two other sessions' input boxes,
    # while the bare Enter (its -t still parsed) went to the right pane.
    # The fake runner in the tests never parsed tmux options, which is why
    # the order shipped green; the argv-shape test now pins it.
    sub, rest = args[0], args[1:]
    if seat.get("substrate") == "docker":
        return ["docker", "exec", seat["identity"], "tmux", sub, "-t", session,
                *rest]
    return ["tmux", "-L", "squad", sub, "-t", session, *rest]


def _capture_pane(seat: dict[str, Any], runner: Any) -> tuple[bool, str]:
    rc, out = runner(_seat_tmux_argv(seat, ["capture-pane", "-p"]))
    return rc == 0, out


# How long the seat's TUI is given to catch up between typing the text and
# submitting it, and again before the after-capture. MEASURED 2026-08-28 on
# an IDLE lane (two console fires, identical outcome): text and Enter sent in
# the same instant left the text sitting in the input box unsubmitted, and
# the lane's next channel wake discarded the draft; the same text with a 1s
# pause before Enter submitted (the dt-poc nudge). The after-capture needs
# the pause for a different reason — the TUI renders asynchronously, and a
# capture taken in the same instant as the keys shows the frame from BEFORE
# them (fire 2 witnessed an empty box that the text had not yet reached).
SEAT_SETTLE_SECONDS = 1.0


def _prompt_submitted(pane: str, text: str) -> str:
    """'' when the pane shows the prompt SUBMITTED, else the reason it does
    not. The witness is the pane, never the send: claude's TUI renders a
    submitted message as a transcript line and keeps an unsubmitted one in
    the input box — the LAST prompt-marker line. Two console fires reported
    "done" while the text sat in that box; "keys sent" is not "submitted".
    """
    probe = (text.splitlines() or [""])[0][:30]
    if not probe:
        return ""  # nothing typed — an Enter alone has no witness to check
    lines = pane.splitlines()
    box_idx = max(
        (i for i, ln in enumerate(lines) if ln.lstrip().startswith("❯")),
        default=-1,
    )
    if box_idx >= 0 and probe in lines[box_idx]:
        return ("the text is still in the input box — Enter did not submit "
                "it")
    if any(probe in ln for i, ln in enumerate(lines) if i != box_idx):
        return ""
    return ("the typed text is not visible anywhere in the pane after "
            "settling — submission unverified")


def realize_seat_action(
    action: dict[str, Any], seat: dict[str, Any], runner: Any,
    pause: Any = None,
) -> dict[str, Any]:
    """Carry out one seat action and report what was OBSERVED.

    Returns the PATCH body the hub expects: `status` (done | refused |
    failed), `observed`, `pane_after`.

    ⚠️ FAIL CLOSED. The pane is captured BEFORE anything is typed, and a
    capture that fails refuses the action outright rather than sending a
    keystroke blind. This is the seat-entry lesson, and it is not
    negotiable: a blind keypress lands on whatever row happens to be
    default, which is how a seat once confirmed its own death — cleanly,
    exit 0, with nothing anywhere that looked wrong.

    ⚠️ The verb set is checked here as well as at the hub. The hub refusing
    to WRITE an unknown verb and the edge refusing to EXECUTE one are
    different guarantees; keeping only the first would mean one compromised
    writer becomes one executed keystroke.
    """
    pause = pause or time.sleep
    kind = action.get("kind") or ""
    if kind not in _SEAT_PHASE1_VERBS:
        return {
            "status": "refused",
            "observed": {"why": f"'{kind}' is not a phase-1 seat verb "
                                f"({', '.join(_SEAT_PHASE1_VERBS)}); the edge "
                                f"executes nothing it does not recognise"},
            "pane_after": None,
        }

    readable, before = _capture_pane(seat, runner)
    if not readable:
        return {
            "status": "refused",
            "observed": {
                "why": "could not capture the seat's pane — refusing to send "
                       "keystrokes to a terminal we cannot read",
                "runner_output": before,
            },
            "pane_after": None,
        }

    if kind == "interrupt":
        # Escape ALONE. Escape-then-Enter would interrupt and then submit
        # whatever was left in the box — a different act than the one asked.
        rc, out = runner(_seat_tmux_argv(seat, ["send-keys", "Escape"]))
        sent: dict[str, Any] = {"sent": "Escape"}
    else:
        text = str(action.get("args", {}).get("text") or "")
        # Two sends, deliberately. `send-keys "<text>" Enter` in one call
        # makes tmux interpret the literal as a KEY NAME whenever it happens
        # to match one — a prompt whose text is "Enter" or "C-c" would be
        # executed as that key. `-l` types it literally; Enter submits.
        rc, out = runner(
            _seat_tmux_argv(seat, ["send-keys", "-l", text])
        )
        if rc == 0:
            # Settle before Enter — see SEAT_SETTLE_SECONDS. An Enter in the
            # same instant as the text left it unsubmitted on an idle lane.
            pause(SEAT_SETTLE_SECONDS)
            rc, out = runner(_seat_tmux_argv(seat, ["send-keys", "Enter"]))
        sent = {"sent": "text+Enter", "chars": len(text)}

    # Captured AFTER — the evidence is the pane the action produced, not the
    # one it started from. "We sent Escape" is an assumption with a number
    # attached; this is an observation. Settled first, or the capture shows
    # the frame from before the keys (see SEAT_SETTLE_SECONDS).
    pause(SEAT_SETTLE_SECONDS)
    _, after = _capture_pane(seat, runner)

    if rc != 0:
        return {
            "status": "failed",
            "observed": {**sent, "why": "send-keys failed",
                         "runner_output": out},
            "pane_after": after,
        }
    if kind == "prompt":
        why = _prompt_submitted(after, text)
        if why:
            return {
                "status": "failed",
                "observed": {**sent, "why": why},
                "pane_after": after,
            }
    return {"status": "done", "observed": sent, "pane_after": after}


def seat_control_pass(
    api: Any, placements: list[dict[str, Any]], runner: Any,
    machine: str = "",
) -> dict[str, Any]:
    """Realize pending seat actions and stream panes for watched seats.

    Runs alongside the placement reconcile on the same timer + doorbell.

    ⚠️ EVERY SEAT IS ISOLATED, and the whole leg is isolated from the
    reconcile that carries it. A seat whose tmux is wedged, or a hub route
    that 500s, must not stop the OTHER seats' actions and must not stop
    placements converging — the same reason `mcp-hub-edge` is its own
    systemd unit rather than folded into `squad-heal`: a oneshot that fails
    takes its whole ExecStart chain with it.

    Placement-backed seats are touched only while a RUNNING placement holds
    them — a reclaimed or stopped seat has no pane to drive, and asking
    would turn a normal state into an error every pass. When `machine` is
    given, LANE seats (spec.substrate == "lane", no placement ever) are
    also driven — see the lane-leg comment below for why they exist and why
    a placement-shadowed identity is skipped.
    """
    realized: list[dict[str, Any]] = []
    streamed = 0
    errors: list[str] = []

    def _drive(seat_id: str, seat: dict[str, Any]) -> None:
        nonlocal streamed
        try:
            for action in api.pull_seat_actions(seat_id):
                if action.get("status") != "pending":
                    continue
                report = realize_seat_action(action, seat, runner)
                api.report_seat_action(seat_id, action["id"], report)
                realized.append({"seat": seat_id, "id": action["id"],
                                 "kind": action.get("kind"),
                                 "status": report["status"]})
        except Exception as exc:  # noqa: BLE001 — one seat must not stop the rest
            errors.append(f"{seat_id}: actions: {exc}")

        try:
            # View ON DEMAND: ask first, capture only if someone is looking.
            # Capturing unconditionally would put every pane on the wire for
            # no reader, which is the cost and exposure the design refuses.
            if api.seat_watched(seat_id):
                ok, pane = _capture_pane(seat, runner)
                if ok:
                    api.push_seat_pane(seat_id, pane)
                    streamed += 1
                # A failed capture pushes NOTHING. An empty pane would read
                # as "the seat is showing nothing", which is a measurement;
                # absence is not.
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{seat_id}: view: {exc}")

    for p in placements:
        if p.get("desired") != "running":
            continue
        seat_id = p["seat"]
        _drive(seat_id, {
            "identity": seat_id,
            "substrate": p.get("substrate", "worktree"),
            # A worktree seat's tmux session is its own name; a container's
            # is the seat image's fixed inner session.
            "session": "seat" if p.get("substrate") == "docker" else seat_id,
        })

    # Lane leg: interactive squad lanes enrolled as seats with
    # spec.substrate == "lane". They have NO placements — a placement means
    # the edge owns lifecycle (materialize/destroy), and lane lifecycle is
    # squad's (`heal`/`up`); giving lanes placements is the double-owner
    # collision `capsules place --as` exists to prevent. So they are
    # discovered via the machine-scoped seats route and driven through
    # squad's own tmux socket (the non-docker door of _seat_tmux_argv,
    # which already speaks it). Console verbs reach them; nothing manages
    # them.
    if machine:
        placed = {p["seat"] for p in placements}
        try:
            lane_rows = api.pull_seats(machine)
        except Exception as exc:  # noqa: BLE001 — discovery must not stop placements
            errors.append(f"lane discovery: {exc}")
            lane_rows = []
        for srow in lane_rows:
            spec = srow.get("spec") or {}
            if spec.get("substrate") != "lane":
                continue
            identity = srow.get("identity") or ""
            if not identity:
                continue
            if identity in placed:
                # A same-named placement owns the pane door (any desired
                # state — a stopped placement is still the authority on
                # which door). The lane leg driving it too would type into
                # a second, same-named pane: the stale host-session shadow
                # of the dt-poc collision class.
                continue
            _drive(identity, {
                "identity": identity,
                "substrate": "lane",
                "session": spec.get("session") or identity,
            })

    return {"realized": realized, "streamed": streamed, "errors": errors}


class SquadExecutor:
    """Maps planned actions onto the proven squad verbs via an injected runner.

    runner(cmd: list[str], cwd: str | None = None) -> (returncode, output).
    Production passes a subprocess wrapper; tests pass a recorder — this class
    never imports subprocess, so no test path can reach a real shell.
    """

    def __init__(self, runner: Any) -> None:
        self._run = runner

    def execute(
        self, action: dict[str, Any], seat_spec: dict[str, Any]
    ) -> dict[str, Any]:
        op = action["op"]
        seat = action["seat"]
        base = {"op": op, "seat": seat}
        if op == "skip":
            return {**base, "skipped": True, "reason": action.get("reason", "")}
        if op == "verify":
            # Verification is the orchestrator's re-enumeration, not a shell
            # command — a verify that shells out to the thing it verifies
            # would be self-assertion.
            return {**base, "deferred": "verified by re-enumeration"}
        if op == "materialize":
            # TWO materialize verbs, picked by what the seat HAS — not by a
            # flag, because the seat already says which it is.
            #
            #   repo   -> `squad add <org/repo>`   clone/pull, then enrol
            #   folder -> `squad add-folder <dir>` enrol what is already there
            #
            # The second is why plain folders are placeable at all. Most of
            # the on-demand roster has no git remote, and `squad add-folder`
            # was built for exactly them ("git optional"). Passing an empty
            # repo to `squad add` would have run `squad add ""` — a failure
            # reported as a materialize that did not work, rather than the
            # enrolment that was actually meant.
            repo = seat_spec.get("repo", "")
            folder = seat_spec.get("folder", "")
            if repo:
                cmd = ["squad", "add", repo]
            elif folder:
                # --name carries the hub's ASSIGNED identity. Without it
                # add-folder derives <basename>-<hostname>, which need not
                # equal the seat — materialize would "succeed" and the very
                # next `squad start <seat>` would fail on a name that is not
                # in the roster.
                cmd = ["squad", "add-folder", folder, "--name", seat]
            else:
                return {**base, "skipped": True,
                        "reason": "seat has neither repo nor folder — nothing "
                                  "to materialize from"}
        elif op == "start":
            if action.get("fresh"):
                cmd = ["squad", "restart", seat, "--fresh"]
            else:
                cmd = ["squad", "start", seat]
        elif op == "stop":
            cmd = ["squad", "stop", seat]
        elif op == "harvest":
            cmd = ["mcp-hub", "memory-export"]
        elif op == "destroy":
            cmd = ["squad", "rm", seat]
        else:
            return {**base, "skipped": True, "reason": f"unknown op '{op}'"}
        if op == "harvest" and seat_spec.get("folder"):
            rc, out = self._run(cmd, cwd=seat_spec["folder"])
        else:
            rc, out = self._run(cmd)
        return {**base, "rc": rc, "output": out[-400:]}


class DockerExecutor:
    """Realize a placement as a container, via an injected runner.

    The unit this edge manages is a CONTAINER; an agent seat is one that
    additionally carries memory and a harvest step. So nothing here knows what
    is inside the image — nginx, an inference server and a squad seat are the
    same shape to it, which is the point.

    The container NAME is the seat identity. That is the whole enumeration
    contract: `docker ps` is asked what exists, and the answer maps back to a
    placement without a side table to drift out of step. It is also why
    `--name` is not optional and why materialize refuses without an image
    rather than inventing one.
    """

    def __init__(self, runner: Any, environ: dict[str, str] | None = None) -> None:
        self._run = runner
        # This machine's own environment, injected so tests never read the
        # real one. It is the ONLY place a secret value comes from.
        self._environ = environ if environ is not None else dict(os.environ)

    def _prepare_repo_mount(self, seat: str,
                            spec: dict[str, Any]) -> dict[str, Any] | None:
        """Bring the host checkout to the wanted state. None = nothing to do
        or done; a dict = the skip result explaining the failure.

        The credential is used HERE, on the host, out of this process's own
        environment — never passed to the container. That inversion is the
        whole feature: `git` on this machine already knows how to authenticate
        (the edge's `~/.mcp-hub/edge-env` is loaded by the systemd unit), so
        the token never has to travel.
        """
        mounts = [m for m in
                  [spec.get("repo_mount"),
                   *(spec.get("extra_repo_mounts") or [])] if m]
        if not mounts:
            return None
        for rm in mounts:
            skip = self._prepare_one_checkout(seat, rm)
            if skip:
                return skip
        return None

    def _prepare_one_checkout(self, seat: str,
                              rm: dict[str, Any]) -> dict[str, Any] | None:
        """One repo's clone/fetch leg of _prepare_repo_mount — extras run
        the identical sequence as the primary, so a failure in either
        stops the materialize the same way (a brief naming a path that
        does not exist is a silently absent reference)."""
        dest = repo_mount_dir(seat, str(rm.get("repo") or ""))
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"skipped": True, "reason": (
                f"could not create the managed checkout root {dest.parent}: "
                f"{exc}"
            )}
        from mcp_hub.seat import SEAT_GITHUB_TOKEN

        for argv in repo_mount_argv(seat, rm):
            rc, out = self._run(argv)
            if rc != 0:
                # The likeliest cause, named rather than left in a git error.
                # A hand-run `edge apply` does not load ~/.mcp-hub/edge-env
                # (the systemd unit does, via EnvironmentFile), so the same
                # command clones from the timer and fails from a terminal —
                # the identical trap the credentials gate above documents.
                hint = ""
                if not self._environ.get(SEAT_GITHUB_TOKEN):
                    hint = (
                        f" {SEAT_GITHUB_TOKEN} is not set in this edge's "
                        f"environment, which is almost certainly why: the "
                        f"host does the cloning now, so the token has to be "
                        f"HERE. If this is a hand-run: "
                        f"`set -a; . ~/.mcp-hub/edge-env; set +a`"
                    )
                return {"skipped": True, "reason": (
                    f"repo_mount: `git … {argv[-2] if len(argv) > 2 else ''}` "
                    f"failed rc={rc} — refusing to start a seat over an "
                    f"incomplete checkout.{hint} {out[-200:]}"
                )}
        if rm.get("npm_ci"):
            # Host-side dependency install for checkouts whose mounted
            # scripts the seat must `require()` (POC-2 tier-1 checks): a
            # bare clone has no node_modules and the seat image carries no
            # npm. `--ignore-scripts` is NON-NEGOTIABLE — without it, `npm
            # ci` executes lifecycle scripts from the repo and every dep on
            # the HOST, outside the container sandbox, which is exactly the
            # boundary the repo_mount design keeps. A dep needing build
            # scripts fails visibly at require-time in the seat, which is
            # the honest outcome.
            import shutil

            npm = shutil.which("npm")
            if not npm:
                return {"skipped": True, "reason": (
                    "repo_mount npm_ci: npm is not on the edge's PATH — "
                    "systemd user units get a bare PATH, so an nvm-installed "
                    "npm is invisible here. Add its directory to PATH in "
                    "~/.mcp-hub/edge-env (loaded by the unit via "
                    "EnvironmentFile) rather than installing globally."
                )}
            rc, out = self._run(
                [npm, "ci", "--omit=dev", "--ignore-scripts"],
                cwd=str(dest))
            if rc != 0:
                return {"skipped": True, "reason": (
                    f"repo_mount npm_ci failed rc={rc} in {dest} — refusing "
                    f"to start a seat whose mounted scripts cannot resolve "
                    f"their imports. {out[-200:]}"
                )}
        return None

    def _enrol_container(self, seat: str,
                         spec: dict[str, Any]) -> dict[str, Any] | None:
        """Record this container in the machine's squad roster.

        🔴 WHY THIS EXISTS. `voice_host.py` authorises a connecting container
        by membership in `~/.config/squad/squad.conf`, and its comment states
        that "the edge shells out to `squad add-container`, so both creation
        paths land here with no special case". **The edge did no such thing.**
        The only `add-container` call site was an operator-run enrol verb, so
        every seat materialized by `edge apply` was refused audio — measured
        2026-08-11: zero streams all day, both live seats recording RMS 0,
        three REFUSED lines naming the empty roster.

        This makes the sentence true rather than changing the gate. The gate is
        right: membership is the RECORD OF A DECISION that this container is
        one of ours, and inferring it from the image was already killed (a
        measurement of the population mistaken for a constraint on it).

        BEST EFFORT, NEVER FATAL. A machine may have no `squad` at all (the
        docker-only edge is a supported shape), and a container that runs
        without audio is far better than a placement that refuses to
        materialize. The result is REPORTED either way, so a silent failure
        here cannot masquerade as success — which is the whole bug being fixed.
        """
        if spec.get("agents"):
            # A POD's rows are per-AGENT and name a tmux session each; enrolling
            # one row for the container would give the workspace a tab that
            # attaches to a session no inhabitant uses. Pods are enrolled by the
            # operator verb that knows their agents. Named, not silently skipped.
            return {"skipped": True, "reason": "pod — enrolled per agent"}
        squad_bin = _squad_bin()
        if not squad_bin:
            return {"skipped": True, "reason": "no `squad` on this machine"}
        # The row needs a real host directory: it is the tab's cwd, and
        # add-container refuses a folder that does not exist. A repo_mount seat
        # already has one and it is the RIGHT one — the checkout the seat is
        # working in. Otherwise a per-seat directory, created here.
        rm = spec.get("repo_mount")
        if rm:
            folder = repo_mount_dir(seat, str(rm.get("repo") or ""))
        else:
            folder = SEAT_REPOS_ROOT.parent / "seat-folders" / seat
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"skipped": True, "reason": f"folder {folder}: {exc}"}
        rc, out = self._run([squad_bin, "add-container", seat, str(folder),
                             seat])
        if rc != 0 and "already enrolled" in (out or ""):
            # Idempotent: a re-materialized seat keeps the row it already has.
            return {"ok": True, "already": True}
        return {"ok": rc == 0, "rc": rc, "output": (out or "")[-200:]}

    def _deenrol_container(self, seat: str,
                           spec: dict[str, Any]) -> dict[str, Any] | None:
        """Remove the destroyed container's row from the squad roster.

        The exact mirror of `_enrol_container`, because the asymmetry was
        measured (2026-08-11, fb: `voicebar rows after destroy: 1`): the edge
        enrolled on materialize and left the row behind on destroy, so
        `squad ls` and the workspace tabs drifted — and a REUSED container
        name would inherit an authorisation nobody granted, since the voice
        gate reads membership as the record of a decision.

        BEST EFFORT, NEVER FATAL, REPORTED EITHER WAY — the same contract as
        enrolment, for the same reason: the destroy itself must never be
        blocked by roster bookkeeping, and a skipped cleanup must not read
        as a done one.
        """
        if spec.get("agents"):
            # Pods were never enrolled by materialize (their rows are
            # per-agent, operator-made); destroying the container must not
            # reach past what materialize added.
            return {"skipped": True, "reason": "pod — enrolled per agent"}
        squad_bin = _squad_bin()
        if not squad_bin:
            return {"skipped": True, "reason": "no `squad` on this machine"}
        rc, out = self._run([squad_bin, "rm", seat])
        if rc != 0 and "unknown agent" in (out or ""):
            # Idempotent: a row already gone (operator ran `squad rm` first,
            # or the seat predates enrolment) is the wanted end state.
            return {"ok": True, "already": True}
        return {"ok": rc == 0, "rc": rc, "output": (out or "")[-200:]}

    @staticmethod
    def create_argv(seat: str, spec: dict[str, Any],
                    environ: dict[str, str] | None = None) -> list[str]:
        """`docker create` for one seat. Created, not run: materialize and
        start are separate ops because `desired: stopped` on a seat that does
        not exist yet must produce a container that is NOT running."""
        argv = ["docker", "create", "--name", seat]
        # Restart policy is deliberately NOT `always`: this edge is the thing
        # that decides what runs. Docker restarting a container the hub asked
        # to stop would make `observed` disagree with reality every 2 minutes.
        argv += ["--restart", "no"]
        for k, v in (spec.get("env") or {}).items():
            argv += ["-e", f"{k}={v}"]
        # The container name IS the seat identity, so SEAT_IDENTITY is
        # injected from the placement rather than trusted from the spec —
        # and injected AFTER spec env (docker: last -e wins) so a stale or
        # hand-edited spec cannot make a container report one name to
        # docker and another to the hub. Name/identity agreement is true
        # by construction, not by convention.
        #
        # A POD (`spec.agents`) has no single identity, so it gets a manifest
        # instead and SEAT_IDENTITY is NOT sent — the entrypoint refuses a
        # container carrying both, because one readable as either shape has an
        # ambiguous identity (docs/n-seats-per-container.md).
        #
        # A spec that sets `env.SEAT_IDENTITY` *and* `agents` is malformed, and
        # it is left to trip that refusal rather than being silently corrected
        # here: a launcher that quietly fixes a contradictory spec hides the
        # contradiction, and the next reader inherits it.
        # The CONTAINER's own name, injected for BOTH shapes. A 1:1 seat can
        # read SEAT_IDENTITY, but a POD has no single identity — and /voice is
        # per CONTAINER (one audio island serving all N agents), so it needs a
        # name that exists in both shapes. Derived here rather than declared in
        # the spec, for the same reason SEAT_IDENTITY is: name and identity
        # agreement true by construction, not by convention
        # (docs/seat-voice.md, "how this gets into EVERY container").
        argv += ["-e", f"SEAT_CONTAINER={seat}"]
        pod_agents = spec.get("agents")
        if pod_agents:
            argv += ["-e", "SEAT_MANIFEST=" + json.dumps(
                {"squad": str(spec.get("squad") or ""), "agents": pod_agents},
                separators=(",", ":"),
            )]
        else:
            argv += ["-e", f"SEAT_IDENTITY={seat}"]
        # WHAT THIS SEAT IS FOR, and the material for it. Both shapes: a brief
        # is a file every inhabitant reads, so unlike SEAT_PROMPT it is
        # meaningful for a pod (see seat.parse_pod_manifest).
        #
        # Carried as env rather than a bind mount on purpose — the spec has to
        # survive a trip through the hub to a machine that has never seen the
        # operator's filesystem, and a host path would name a file that does
        # not exist there. It is the same reason the repo is cloned rather
        # than mounted.
        if spec.get("brief"):
            argv += ["-e", f"SEAT_BRIEF={spec['brief']}"]
        if spec.get("inputs"):
            argv += ["-e", "SEAT_INPUTS=" + json.dumps(
                spec["inputs"], separators=(",", ":"))]
        # SECRETS: the hub stores the NAME, this machine supplies the VALUE.
        #
        # A seat spec lives in the hub's SQLite and anything holding the
        # operator token can read it, so an API key passed through `env` would
        # sit in plaintext in the control plane and in every backup of it.
        # Naming the variable instead keeps the control plane free of secrets
        # entirely — the hub can be fully compromised without leaking one.
        #
        # A name that is not set HERE is omitted rather than passed as empty:
        # an empty ANTHROPIC_API_KEY authenticates as nothing and produces a
        # confusing 401 inside the container, where a missing one fails at the
        # door with an obvious message.
        env = environ if environ is not None else {}
        for name in injected_credentials(spec):
            if env.get(name):
                argv += ["-e", f"{name}={env[name]}"]
        for pub in spec.get("ports") or []:
            argv += ["-p", str(pub)]
        for vol in spec.get("volumes") or []:
            argv += ["-v", str(vol)]
        # The host checkout (docs/seat-repo-access.md). Placed BEFORE the
        # memory volume so that a dest colliding with the state dir would be
        # overridden by it rather than shadowing it — belt to the guard's
        # braces, which refuses such a dest at both write time and here.
        rm = spec.get("repo_mount")
        if rm:
            dest = str(rm.get("dest") or "").strip() or SEAT_WORK_DIR
            argv += ["-v", f"{repo_mount_dir(seat, str(rm.get('repo') or ''))}"
                           f":{dest}"]
        # Reference repos (extra_repo_mounts) mount READ-ONLY, always: a
        # reference the seat could edit is a fork nobody asked for, and the
        # ro flag is enforced here rather than declared in the spec so no
        # spec can forget it. dest is guaranteed by the guard.
        for extra in spec.get("extra_repo_mounts") or []:
            argv += ["-v",
                     f"{repo_mount_dir(seat, str(extra.get('repo') or ''))}"
                     f":{str(extra.get('dest') or '').strip()}:ro"]
        # THE MEMORY VOLUME IS A MOUNT, not just a flag.
        #
        # It was declared on every seat and read in exactly one place — the
        # harvest branch, to decide whether a seat "has learnings" — and
        # never mounted. So ~/.claude was container-local: reclaim would
        # `memory-export` from a directory that only ever existed inside the
        # container it was about to destroy, and the edge would report a
        # successful harvest of nothing. Measured 2026-08-06 by destroying
        # three live seats on a stated belief that their memory was durable;
        # `docker inspect` showed one mount, the worktree.
        #
        # A bare name gets the documented destination rather than being
        # guessed at or silently ignored (docs/seat-image.md: the whole claude
        # state dir — memory, transcripts, credentials cache).
        memvol = str(spec.get("memory_volume") or "")
        if memvol:
            argv += ["-v", memvol if ":" in memvol
                     else f"{memvol}:{SEAT_STATE_DIR}"]
        if spec.get("network"):
            argv += ["--network", str(spec["network"])]
        argv.append(spec["image"])
        cmd = spec.get("command")
        if cmd:
            argv += list(cmd) if isinstance(cmd, list) else [str(cmd)]
        return argv

    def execute(self, action: dict[str, Any],
                seat_spec: dict[str, Any]) -> dict[str, Any]:
        op = action["op"]
        seat = action["seat"]
        base = {"op": op, "seat": seat, "substrate": "docker"}
        spec = (seat_spec or {}).get("spec") or {}
        if op == "skip":
            return {**base, "skipped": True, "reason": action.get("reason", "")}
        if op == "verify":
            return {**base, "deferred": "verified by re-enumeration"}
        if op == "harvest":
            # Not every container has learnings. A seat image that mounts a
            # memory volume harvests through that volume; a web app has
            # nothing to harvest and saying so beats running a no-op that
            # LOOKS like it preserved something.
            if not spec.get("memory_volume"):
                return {**base, "skipped": True,
                        "reason": "no memory_volume in spec — nothing to harvest"}
            agents = spec.get("agents") or []
            if agents:
                # A POD harvests ONCE PER AGENT. `memory-export` resolves
                # identity from its cwd (the marker in that workdir), so `-w`
                # is what selects which agent is being harvested — the same
                # mechanism the 1:1 seat gets for free from the image's
                # WORKDIR, made explicit because there are now N of them.
                results = []
                for a in agents:
                    ident = str((a or {}).get("identity") or "")
                    if not ident:
                        continue
                    rc, out = self._run([
                        "docker", "exec", "-w", f"{SEAT_WORK_DIR}/{ident}",
                        seat, "mcp-hub", "memory-export",
                    ])
                    results.append({"identity": ident, "rc": rc,
                                    "output": out[-200:]})
                # The WORST rc wins. A pod where one agent's export failed has
                # NOT been harvested, and reporting 0 because the others
                # succeeded would let reclaim destroy the container on the
                # strength of a partial save — which is the whole failure this
                # step exists to prevent.
                return {**base, "rc": max((r["rc"] for r in results), default=0),
                        "agents": results,
                        "output": "; ".join(
                            f"{r['identity']}: rc={r['rc']}" for r in results
                        ) or "no identities in manifest"}
            cmd = ["docker", "exec", seat, "mcp-hub", "memory-export"]
        elif op == "materialize":
            if not spec.get("image"):
                # Refuse rather than guess: a wrong image would start the
                # wrong software under the right name, which enumeration
                # cannot detect — `docker ps` would report it healthy.
                return {**base, "skipped": True,
                        "reason": "no image in seat spec — refusing to guess one"}
            if ((spec.get("env") or {}).get("SEAT_MODE") == "headless"
                    and not spec.get("memory_volume")):
                # The memory volume is where a headless RESULT survives —
                # docker logs die with `docker rm`, and exec-harvest refuses
                # on an exited container (both measured 2026-08-08). Without
                # one, seat-entry refuses at its door anyway (exit 43); this
                # skip catches it a pass earlier, with the fix in the reason
                # instead of a dead container in the enumeration.
                return {**base, "skipped": True, "reason": (
                    "SEAT_MODE=headless with no memory_volume — the result "
                    "would die with the container. Re-declare the seat with "
                    "--memory-volume <name>."
                )}
            wanted = injected_credentials(spec)
            if wanted and not any(self._environ.get(n) for n in wanted):
                # NONE of the named credentials is set here, so this container
                # is guaranteed to die at its own door (exit 42) the moment it
                # starts. Creating it anyway produces a seat that exists, is
                # observed, and can never be an agent.
                #
                # The precise condition is "not one of them" rather than "any
                # missing": a spec naming both lanes with only one set is the
                # NORMAL healthy case, and refusing that would break every
                # seat on the fleet.
                #
                # Measured cause, every time: a hand-run `edge apply`. The
                # systemd unit loads ~/.mcp-hub/edge-env via EnvironmentFile
                # and a shell does not, so the same command builds a live seat
                # from the timer and a dead one from a terminal.
                return {**base, "skipped": True, "reason": (
                    f"none of the credentials this spec names is set in the "
                    f"edge's environment ({', '.join(wanted)}) — refusing to "
                    f"create a container that would exit 42 at its door. If "
                    f"this is a hand-run, load the same file the timer does: "
                    f"`set -a; . ~/.mcp-hub/edge-env; set +a`"
                )}
            # THE LAST GATE BEFORE THE PREMISE BECOMES FALSE (W2.5). The hub
            # refuses such a spec at write time, but a spec stored BEFORE
            # that guard existed would otherwise materialize here — and the
            # seat runs in bypassPermissions on the sole grounds that the
            # container contains it. The edge is the only place that can
            # still say no, so it does, rather than trusting a validation
            # that happened somewhere else at some other time.
            bad = check_volumes(spec.get("volumes"))
            if bad:
                return {**base, "skipped": True, "reason": bad}
            bad = check_repo_mount(spec.get("repo_mount"))
            if bad:
                return {**base, "skipped": True, "reason": bad}
            # The container-credential policy: a seat that declares its
            # approved lists is held to them HERE, at the last place that
            # can refuse before the value enters the container. Same
            # fail-closed shape as check_volumes, same reasoning as W2.5.
            bad = check_credential_policy(spec)
            if bad:
                return {**base, "skipped": True, "reason": bad}
            # THE CHECKOUT HAPPENS FIRST, AND ITS FAILURE STOPS THE
            # MATERIALIZE. A container started over a missing or half-fetched
            # directory looks healthy to `docker ps` and gives the agent an
            # empty workdir — the seat would sit there with nothing to do and
            # nothing saying why. Refusing here names the git failure instead.
            prep = self._prepare_repo_mount(seat, spec)
            if prep is not None:
                return {**base, **prep}
            cmd = self.create_argv(seat, spec, self._environ)
            rc, out = self._run(cmd)
            enrol = self._enrol_container(seat, spec) if rc == 0 else None
            res = {**base, "rc": rc, "output": out[-400:]}
            if enrol:
                res["enrolled"] = enrol
            return res
        elif op == "start":
            cmd = ["docker", "start", seat]
        elif op == "stop":
            cmd = ["docker", "stop", seat]
        elif op == "destroy":
            cmd = ["docker", "rm", "-f", seat]
            rc, out = self._run(cmd)
            deenrol = self._deenrol_container(seat, spec) if rc == 0 else None
            res = {**base, "rc": rc, "output": out[-400:]}
            if deenrol:
                res["deenrolled"] = deenrol
            return res
        else:
            return {**base, "skipped": True, "reason": f"unknown op '{op}'"}
        rc, out = self._run(cmd)
        return {**base, "rc": rc, "output": out[-400:]}


def _docker_permission_hint(output: str) -> str:
    """Name the cause a docker permission error almost always has HERE.

    Observed live on dev-vm-1, 2026-08-04: `docker ps` worked in an ssh
    session and failed inside the edge's systemd unit, on the same box, as the
    same user. The user was added to the `docker` group AFTER the systemd
    --user manager started, and every service the manager spawns inherits its
    supplementary groups as they were then. PAM builds fresh credentials for
    each login, which is exactly why an interactive check says everything is
    fine.

    Diagnosing that from `permission denied` alone took twenty minutes. The
    error is the right place to spend the sentence.
    """
    low = output.lower()
    if "permission denied" not in low or "docker.sock" not in low:
        return ""
    return (
        "\n  Likely cause: this runs as a systemd --user service, and the user "
        "manager was started BEFORE the account joined the `docker` group — it "
        "keeps the supplementary groups it had then, while an interactive shell "
        "gets fresh ones and works fine."
        "\n  Check:  grep ^Groups: /proc/$(pgrep -u $UID -f 'systemd --user' "
        "| head -1)/status   against   getent group docker"
        "\n  Fix:    log the user fully out and back in, or reboot. NOTE that "
        "`loginctl terminate-user` restarts the manager and will kill any agent "
        "panes and timers it owns — on a box running seats that is not a quiet "
        "operation."
    )


def enumerate_docker(runner: Any, seats: list[str]) -> dict[str, dict[str, Any]]:
    """What docker ACTUALLY has, for the named seats.

    `-a` is load-bearing: a stopped container still exists, and calling it
    unmaterialized would make the planner create a second one under a name
    that is already taken — the run would fail every pass, forever.
    """
    rc, out = runner(
        ["docker", "ps", "-a", "--format", "{{.Names}}\\t{{.State}}"]
    )
    if rc != 0:
        raise EnumerationFailed(
            f"`docker ps` failed (rc={rc}) — refusing to plan or report against "
            f"state this pass never observed. Output: {out.strip()[:300]}"
            f"{_docker_permission_hint(out)}"
        )
    found: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            found[parts[0].strip()] = parts[1].strip()

    # WHICH IMAGE each container actually runs — a second call, deliberately.
    # `docker ps --format {{.ImageID}}` does not exist (measured: template
    # error), and `{{.Image}}` is the TAG, which reads identically for a
    # container created from last month's `latest`. Only inspect exposes the
    # image ID that would reveal drift.
    #
    # The SAME call now carries `.State.Status` and `.State.ExitCode` — how a
    # finished headless seat becomes distinguishable from a crashed or
    # never-started one. `docker ps` cannot say (its {{.Status}} is freetext);
    # inspect can, and we are already paying for the call.
    #
    # Skipped entirely when nothing is materialized: `docker inspect` with no
    # arguments is an error, and a pointless call would fail the whole pass.
    images: dict[str, str] = {}
    exit_codes: dict[str, int] = {}
    present = [s for s in seats if s in found]
    if present:
        rc, iout = runner(
            ["docker", "inspect", "--format",
             "{{.Name}} {{.Image}} {{.State.Status}} {{.State.ExitCode}}",
             *present]
        )
        if rc != 0:
            raise EnumerationFailed(
                f"`docker inspect` failed (rc={rc}) — refusing to report an "
                f"image identity this pass never observed. Output: "
                f"{iout.strip()[:300]}"
            )
        for line in iout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0].lstrip("/").strip()
                images[name] = parts[1].strip()
                # Exit code ONLY for `exited` — a `created` container also
                # reports code 0, and reading that as "completed" would call
                # a job done that never ran.
                if len(parts) >= 4 and parts[2] == "exited":
                    try:
                        exit_codes[name] = int(parts[3])
                    except ValueError:
                        pass

    return {
        s: {
            "materialized": s in found,
            "running": found.get(s) == "running",
            "image": images.get(s) if s in found else None,
            **({"exit_code": exit_codes[s]} if s in exit_codes else {}),
        }
        for s in seats
    }


def resolve_image_id(runner: Any, image: str) -> str | None:
    """Current image ID for a tag, or None when it cannot be resolved.

    None means UNKNOWN, never "drift": an image that has not been pulled on
    this box yields no comparison, and claiming divergence from an absent
    fact would make every seat on a fresh machine look broken. Unknown is
    the honest answer and it stays out of the report.
    """
    rc, out = runner(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"]
    )
    if rc != 0:
        return None
    return out.strip().splitlines()[0].strip() if out.strip() else None


# ── The doorbell, machine side ───────────────────────────────────────────────
#
# The timer stays. This only ACCELERATES it: the hub says "something changed
# for you" and the edge runs the same pass it would have run at the next tick.
# A lost bell therefore costs latency and never work, which is the property
# that lets the stream be simple.
#
# Shape borrowed (read, not guessed) from `dreamteam` 0d17942 and vps-hetzner's
# independently-built egress-sync — but hub-native: the edge dials the hub it
# already holds a bearer for, and no other estate is in the runtime path.

WATCH_SILENCE_S = 60.0        # no event AND no heartbeat for this long = dead
WATCH_BACKOFF_MIN_S = 1.0
WATCH_BACKOFF_MAX_S = 30.0


def next_backoff(current: float,
                 minimum: float = WATCH_BACKOFF_MIN_S,
                 maximum: float = WATCH_BACKOFF_MAX_S) -> float:
    """Double, bounded. Pure so the schedule can be asserted, not observed."""
    return min(max(current * 2.0, minimum), maximum)


def is_wake(line: str) -> bool:
    """True for a doorbell, False for a heartbeat, blank or comment.

    The heartbeat is a COMMENT line (`: heartbeat`) precisely so it can never
    be mistaken for a bell — it proves the stream is alive and nothing more.
    """
    return line.startswith("event: wake")


class Coalescer:
    """One pass at a time; a bell during a pass earns exactly ONE more after.

    Both halves matter and each has a failure of its own:

    · Without the guard, a burst of writes starts overlapping passes that
      enumerate and act on the same substrate concurrently — two `squad start`
      for one agent, and a report built from a half-finished pass.
    · Without the trailing re-run, a bell that lands mid-pass is SWALLOWED:
      the pass already read its placements before that write existed, so the
      change waits for the timer and the doorbell silently did nothing.
    """

    def __init__(self) -> None:
        self.running = False
        self.pending = False

    def request(self) -> bool:
        """A bell rang. True if the caller should start a pass NOW."""
        if self.running:
            self.pending = True
            return False
        self.running = True
        return True

    def finished(self) -> bool:
        """A pass ended. True if another must run immediately."""
        if self.pending:
            self.pending = False
            return True          # stays running — the caller loops
        self.running = False
        return False


def watch_once(lines: Any, coalesce: Coalescer, run_pass: Any,
               log: Any = None) -> None:
    """Consume ONE connection's lines, running a pass per bell.

    `lines` is any iterable of decoded SSE lines — an httpx response's
    `iter_lines()` in production, a list in a test. Returns when the stream
    ends, which the caller treats as a disconnect.
    """
    log = log or (lambda _m: None)
    for raw in lines:
        line = (raw or "").rstrip("\r")
        if not is_wake(line):
            continue            # heartbeat, comment, data line, blank
        if not coalesce.request():
            log("doorbell mid-pass — queued one trailing pass")
            continue
        while True:
            try:
                run_pass("doorbell")
            except Exception as e:  # noqa: BLE001
                # 🔴 A failed pass must NEVER kill the stream. The timer floor
                # and the next bell both catch up; dropping the connection
                # because one reconcile threw would turn a transient error
                # into a silently deaf edge.
                log(f"doorbell pass failed (non-fatal, timer floor covers): {e}")
            if not coalesce.finished():
                break


def watch_forever(base_url: str, token: str, machine: str, run_pass: Any,
                  log: Any = None, sleeper: Any = None,
                  silence_s: float = WATCH_SILENCE_S,
                  connect: Any = None, max_connects: int | None = None) -> None:
    """Hold the doorbell open, reconnecting forever.

    A full pass runs on EVERY (re)connect, before any event is read. That
    covers whatever happened while disconnected — and here it is nearly free,
    because the pass is a full resync regardless: there is no cursor to resume
    and no backlog to replay.

    `connect`, `sleeper` and `max_connects` are injected for tests; production
    passes none of them.
    """
    import time as _time

    log = log or (lambda m: print(m, flush=True))
    sleeper = sleeper or _time.sleep
    coalesce = Coalescer()
    backoff = WATCH_BACKOFF_MIN_S
    connects = 0

    if connect is None:
        import httpx

        def connect(url: str, headers: dict, timeout: float):  # noqa: F811
            client = httpx.Client(base_url=base_url, timeout=timeout)
            return client.stream("GET", url, headers=headers)

    while max_connects is None or connects < max_connects:
        connects += 1
        try:
            # read=silence_s makes a SILENT stream fail instead of hanging:
            # without it a dead-but-open socket looks exactly like a quiet one
            # and the edge waits forever, deaf, with nothing to log.
            ctx = connect(
                f"/api/v1/machines/{machine}/watch",
                {"Authorization": f"Bearer {token}",
                 "Accept": "text/event-stream"},
                silence_s,
            )
            with ctx as response:
                if getattr(response, "status_code", 200) >= 400:
                    raise RuntimeError(
                        f"watch refused: HTTP {response.status_code}")
                log(f"doorbell: connected to {base_url} as {machine}")
                backoff = WATCH_BACKOFF_MIN_S   # a healthy connect resets it
                # Resync FIRST, before reading a single event.
                if coalesce.request():
                    while True:
                        try:
                            run_pass("reconnect-resync")
                        except Exception as e:  # noqa: BLE001
                            log(f"doorbell resync failed (non-fatal): {e}")
                        if not coalesce.finished():
                            break
                watch_once(response.iter_lines(), coalesce, run_pass, log)
            log("doorbell: stream ended")
        except Exception as e:  # noqa: BLE001 — reconnecting IS the handler
            log(f"doorbell: {type(e).__name__}: {e}")
        if max_connects is not None and connects >= max_connects:
            break
        sleeper(backoff)
        backoff = next_backoff(backoff)


class EnumerationFailed(RuntimeError):
    """The substrate could not be enumerated, so nothing may be claimed.

    Distinct from "enumerated, found nothing": the second is a fact, the
    first is the absence of one. Only the second may reach a report.
    """


def edge_apply(
    api: HubAPI,
    machine: str,
    runner: Any,
    scan_dirs: list[Path],
    seeder: Any = None,
    session_sweep: Any = None,
) -> dict[str, Any]:
    """One reconcile pass: pull → enumerate → plan → execute → report.

    Reports are built from a FRESH enumeration taken after execution — the
    loop observes the effect of its own actions rather than assuming them.
    """
    placements = api.pull_placements(machine)
    docker_seats = [p["seat"] for p in placements
                    if p.get("substrate") == "docker"]
    worktree_seats = [p["seat"] for p in placements
                      if p.get("substrate", "worktree") == "worktree"]

    def enumerate_now() -> dict[str, dict[str, Any]]:
        # Each substrate is enumerated by its OWN authority, and only when
        # something needs it: a docker-only box may have no squad installed at
        # all, and running `squad ls` there would turn a healthy machine into
        # an EnumerationFailed every two minutes.
        state: dict[str, dict[str, Any]] = {}
        if docker_seats:
            state.update(enumerate_docker(runner, docker_seats))
        if not worktree_seats:
            return state
        # One truthful source for both facts: `squad ls` rows carry roster
        # enrollment (the row exists) and tmux liveness (the up/down column).
        rc, out = runner(["squad", "ls"])
        if rc != 0:
            # A failed enumeration used to fall through as an EMPTY set, which
            # is not "nothing is enrolled" — it is "I did not look". Every
            # placement would then plan a `materialize`, and the run would
            # report observations it never made. That is the evidence
            # contract's first rule inverted: an assertion over an empty set
            # must be a hard error, never a quiet success.
            raise EnumerationFailed(
                f"`squad ls` failed (rc={rc}) — refusing to plan or report "
                f"against state this pass never observed. Output: "
                f"{out.strip()[:300]}"
            )
        enrolled: dict[str, bool] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("up", "down"):
                enrolled[parts[0]] = parts[1] == "up"
        for seat in worktree_seats:
            state[seat] = {
                "materialized": seat in enrolled,
                "running": enrolled.get(seat, False),
            }
        return state

    actions = plan(placements, enumerate_now())
    # Injectable seeder: production seeds the real ~/.claude.json; tests
    # inject a recorder. A side-effecting default reachable from tests wrote
    # a bogus entry into a REAL claude.json once (2026-07-29) — hence
    # injection, not discipline, as the guard.
    seed = seed_first_launch if seeder is None else seeder
    if any(a["op"] == "materialize" for a in actions):
        for p in placements:
            spec = p.get("seat_spec") or {}
            # Worktree only: folder-trust and the MCP approval are host
            # Claude Code state. A container brings its own filesystem and
            # never reads ~/.claude.json, so seeding for it would write an
            # entry naming a path that does not exist on this box.
            if p["substrate"] == "worktree" and spec.get("folder"):
                seed(spec["folder"])
    # One executor per substrate, chosen by the ACTION, not by the machine —
    # a box can run agent seats in tmux and a web app in a container at the
    # same time, and that is the ordinary case rather than the exotic one.
    executors = {"worktree": SquadExecutor(runner), "docker": DockerExecutor(runner)}
    specs = {p["seat"]: (p.get("seat_spec") or {}) for p in placements}
    results = []
    for a in actions:
        ex = executors.get(a.get("substrate", "worktree"))
        if ex is None:
            results.append({"op": a["op"], "seat": a["seat"], "skipped": True,
                            "reason": f"no executor for '{a.get('substrate')}'"})
            continue
        results.append(ex.execute(a, specs.get(a["seat"], {})))

    local = enumerate_now()
    # Resolve each DISTINCT spec image once — a squad of ten seats off one
    # image asks docker once, not ten times.
    want_image_ids: dict[str, str | None] = {}
    for p in placements:
        if p.get("substrate") != "docker":
            continue
        img = (specs.get(p["seat"], {}).get("spec") or {}).get("image")
        if img and img not in want_image_ids:
            want_image_ids[img] = resolve_image_id(runner, img)
    # The box-wide sweep, run at most ONCE per pass and only when a reclaim
    # verdict is actually pending: reclaim's absence evidence must cover the
    # seat's FOOTPRINT, not just its primary artifact. `swept` and `sweep`
    # are separate facts — a sweep that ran and FAILED (None) must reach
    # observed_report as `host_sessions_unknown`, never as silence, or a
    # failed look reads as a clean one.
    # `session_sweep` is injectable like `seeder`: the default reads the
    # box's real socket dir, which a test must never depend on.
    sweep_fn = session_sweep or (lambda: host_tmux_sessions(runner))
    sweep: dict[str, set[str]] | None = None
    swept = any(p.get("desired") == "reclaimed" for p in placements)
    if swept:
        sweep = sweep_fn()
    reported = 0
    for p in placements:
        state = local[p["seat"]]
        # Name the enumeration after what was actually asked: calling a
        # container a `tmux_session` would put a false claim in the observed
        # record, which is the one place the fleet trusts completely.
        if p.get("substrate") == "docker":
            enumeration = {
                "container": p["seat"],
                "alive": state["running"],
                "exists": state["materialized"],
            }
            # Exit code + mode ride ALONG so observed_report can tell a
            # finished headless errand from a crash. The mode comes from the
            # SPEC's env — the same single source seat-entry reads — never
            # inferred from how the container looks.
            if "exit_code" in state:
                enumeration["exit_code"] = state["exit_code"]
            spec_env = (specs.get(p["seat"], {}).get("spec") or {}) \
                .get("env") or {}
            if spec_env.get("SEAT_MODE") == "headless":
                enumeration["headless"] = True
            # Image drift, reported only where BOTH sides are known: the
            # image the container runs (enumerated) and the current ID of
            # the tag its spec names (resolved). Either unknown -> the key
            # is absent, and absent means unknown rather than agreement.
            want = (specs.get(p["seat"], {}).get("spec") or {}).get("image")
            want_id = want_image_ids.get(want) if want else None
            if state.get("image") and want_id:
                enumeration["image"] = state["image"]
                enumeration["want_image"] = want_id
                enumeration["image_matches"] = state["image"] == want_id
        else:
            enumeration = {
                "tmux_session": p["seat"],
                "alive": state["running"],
                "enrolled": state["materialized"],
            }
        if swept and p.get("desired") == "reclaimed":
            if sweep is None:
                enumeration["host_sessions_unknown"] = True
            else:
                enumeration["host_sessions"] = sorted(
                    sock for sock, names in sweep.items()
                    if p["seat"] in names
                )
        api.push_observed(p["id"], observed_report(p, enumeration))
        reported += 1

    # Seat control (cards #144/#152) rides the same pass — but AFTER the
    # placements are reported, and inside its own guard: a wedged pane must
    # not cost the fleet its reconcile.
    try:
        seat_control = seat_control_pass(api, placements, runner,
                                         machine=machine)
    except Exception as exc:  # noqa: BLE001 — never let control break convergence
        seat_control = {"realized": [], "streamed": 0, "errors": [str(exc)]}

    workspaces = discover_workspaces([Path(d) for d in scan_dirs])
    # The roster, agent → worktree. Only this machine can say which folder an
    # agent sits in: a remote row in the board carries no worktree, so without
    # this the board matches by repo BASENAME and a box with several clones of
    # one repo has every clone claimed by every workspace listing any of them.
    # Reported even when empty — an empty LIST is "no agents here", while the
    # key being ABSENT is "this edge does not report rosters", and the board
    # falls back to basename matching only for the second.
    api.push_status(
        machine,
        {
            "workspaces": workspaces,
            # The runner is what lets it read tmux liveness; without one the
            # `running` key is omitted rather than guessed.
            "agents": local_roster(runner),
            "seats": [
                {"seat": s, **v} for s, v in sorted(local.items())
            ],
            # The pass reports on ITSELF (W1.2). Until this key existed the
            # hub's only machine fact was last_seen, so a machine whose edge
            # died 203/EXEC for five days read exactly like a healthy quiet
            # one — the summary below went to stdout and the journal, i.e.
            # to the one place nothing was watching.
            "edge": {
                "ts": time.time(),
                "result": "ok",
                "placements": len(placements),
                "actions": len(results),
                # Seat-control failures are REPORTED, not swallowed. They
                # are isolated from convergence on purpose, and an isolated
                # failure with no reader is the defect this fleet spent
                # today naming — a control is not done until it names who
                # receives it firing.
                "seat_actions": len(seat_control["realized"]),
                "panes_streamed": seat_control["streamed"],
                "errors": seat_control["errors"],
            },
        },
    )
    return {
        "placements": len(placements),
        "actions": results,
        "observed_reported": reported,
        "workspaces_reported": len(workspaces),
        "seat_control": seat_control,
    }


def push_failure(api: HubAPI, machine: str, error: str) -> None:
    """Report a FAILED pass to the hub — from the except path, because
    push_status is the LAST step of a successful pass and a raise anywhere
    earlier means no status ever posts (the failure mode that kept
    EnumerationFailed on stderr only).

    Wrapped so the failure-reporter cannot die of its own report:
    push_status raises on HTTP error, and an exception here would replace
    the real error with the reporting error at exactly the moment the real
    one matters most.
    """
    try:
        api.push_status(
            machine,
            {"edge": {
                "ts": time.time(),
                "result": "failed",
                "errors": [str(error)[:500]],
            }},
        )
    except Exception:  # noqa: BLE001 — see docstring
        pass
