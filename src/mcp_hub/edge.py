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
from pathlib import Path
from typing import Any

# Where the seat credentials live on an edge host. The systemd unit reads it
# via `EnvironmentFile=-%h/.mcp-hub/edge-env`; the CLI reads it through
# load_env_file so a hand-run behaves the same way. chmod 600, VALUES inside —
# which is why the hub only ever stores the NAMES.
EDGE_ENV_FILE = Path.home() / ".mcp-hub" / "edge-env"

# Where a seat's claude state lives INSIDE the container — memory,
# transcripts, credentials cache. The image's user is `seat` (seat/Dockerfile),
# so this is that user's ~/.claude and nothing here may derive it from the
# EDGE host's HOME. Destination for a `memory_volume` given as a bare name.
SEAT_STATE_DIR = "/home/seat/.claude"


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
        elif desired == "stopped":
            if local["running"]:
                actions.append({**base, "op": "stop"})
    return actions


def local_roster() -> list[dict[str, str]]:
    """This machine's roster as {agent, worktree} — the fact only it holds.

    Imported lazily: `edge` is the realizer and `cli` is the command surface,
    and importing the latter at module scope makes a cycle. Failure is an
    EMPTY list rather than a raise, because a machine that cannot read its own
    roster must still report its workspaces and observations — but note that
    the caller sends the key regardless, so an unreadable roster is reported as
    "no agents", not as "no roster reported". That is the honest way round: an
    edge that IS reporting should not be treated as one that cannot.
    """
    try:
        from mcp_hub.cli import _roster_all

        return [{"agent": r["agent"], "worktree": r.get("worktree", "")}
                for r in _roster_all() if r.get("agent")]
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
        if present is False:
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
            cmd = ["squad", "add", seat_spec.get("repo", "")]
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
        argv += ["-e", f"SEAT_IDENTITY={seat}"]
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
        for name in spec.get("env_from_host") or []:
            if env.get(name):
                argv += ["-e", f"{name}={env[name]}"]
        for pub in spec.get("ports") or []:
            argv += ["-p", str(pub)]
        for vol in spec.get("volumes") or []:
            argv += ["-v", str(vol)]
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
            cmd = ["docker", "exec", seat, "mcp-hub", "memory-export"]
        elif op == "materialize":
            if not spec.get("image"):
                # Refuse rather than guess: a wrong image would start the
                # wrong software under the right name, which enumeration
                # cannot detect — `docker ps` would report it healthy.
                return {**base, "skipped": True,
                        "reason": "no image in seat spec — refusing to guess one"}
            wanted = list(spec.get("env_from_host") or [])
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
            cmd = self.create_argv(seat, spec, self._environ)
        elif op == "start":
            cmd = ["docker", "start", seat]
        elif op == "stop":
            cmd = ["docker", "stop", seat]
        elif op == "destroy":
            cmd = ["docker", "rm", "-f", seat]
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
    # Skipped entirely when nothing is materialized: `docker inspect` with no
    # arguments is an error, and a pointless call would fail the whole pass.
    images: dict[str, str] = {}
    present = [s for s in seats if s in found]
    if present:
        rc, iout = runner(
            ["docker", "inspect", "--format", "{{.Name}} {{.Image}}", *present]
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
                images[parts[0].lstrip("/").strip()] = parts[1].strip()

    return {
        s: {
            "materialized": s in found,
            "running": found.get(s) == "running",
            "image": images.get(s) if s in found else None,
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
        api.push_observed(p["id"], observed_report(p, enumeration))
        reported += 1

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
            "agents": local_roster(),
            "seats": [
                {"seat": s, **v} for s, v in sorted(local.items())
            ],
        },
    )
    return {
        "placements": len(placements),
        "actions": results,
        "observed_reported": reported,
        "workspaces_reported": len(workspaces),
    }
