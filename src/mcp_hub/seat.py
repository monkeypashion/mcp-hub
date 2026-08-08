"""Seat-container door checks — the pure logic under `mcp-hub seat-entry`.

Contract: docs/seat-image.md. This module validates, it never arbitrates:
which credential Claude Code uses when both are set is the CLI's own auth
hierarchy, deliberately not reimplemented here (and, per dt 2026-08-04,
never measured anywhere — do not let a comment claiming a winner creep in
without a measurement record beside it).

The rules are dt's hard-fail shape (codespace-runner.js:2288-2302), built
from two real incidents: an empty `export` clobbering a good token (present
but invalid — set-ness checks pass, claude starts unauthenticated), and a
deleted codespace secret that warned into an unread log for five weeks.
Hence length checks and loud refusal, with the plausible causes in the
error itself.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

# The factory's auth-death exit code. Shared dialect with codespace-runner —
# an auth failure must never be misread as a build failure in either estate.
EXIT_AUTH = 42
# Contract violations other than auth (missing SEAT_IDENTITY/MCP_HUB_URL...).
EXIT_CONTRACT = 43

# dt's measured thresholds: observed OAuth tokens run 110-145 chars, the
# guard trips below 50; inbound/API keys guard at 20. Not invented here.
OAUTH_MIN_LEN = 50
API_KEY_MIN_LEN = 20

_OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"
_API_KEY = "ANTHROPIC_API_KEY"

# The comms a seat needs to BE an agent — read and write messages, be
# discoverable, keep its binding alive. Deliberately excludes the hub's
# authority-shaped tools (decision_*, memory_put, set_squads, unregister):
# a seat that can silently leave its squad or answer decisions on the
# operator's behalf is a different kind of thing, and that is a grant to
# make deliberately rather than to inherit from an image.
SEAT_ALLOWED_TOOLS = (
    "mcp__hub__register",
    "mcp__hub__ping",
    "mcp__hub__list_agents",
    "mcp__hub__list_squads",
    "mcp__hub__list_channels",
    "mcp__hub__get_messages",
    "mcp__hub__get_broadcasts",
    "mcp__hub__get_broadcasts_for_agent",
    "mcp__hub__get_channel_messages",
    "mcp__hub__get_history",
    "mcp__hub__send",
    "mcp__hub__post",
    "mcp__hub__broadcast",
    "mcp__hub__update_bio",
    "mcp__hub__hub_status",
)


@dataclass(frozen=True)
class CredentialVerdict:
    ok: bool
    lane: str  # "oauth" | "api-key" | "both" | ""
    error: str  # names plausible causes; "" when ok


def _plausible(value: str, min_len: int) -> bool:
    # Whitespace is not credential; length is measured after stripping so a
    # padding of spaces can't fake plausibility.
    return len(value.strip()) >= min_len


def validate_seat_credentials(
    environ: Mapping[str, str],
) -> CredentialVerdict:
    """Presence + plausibility of the seat's Anthropic credential.

    Set-but-implausible is FATAL even when the other lane is valid: the
    edge omits unset `env_from_host` names entirely, so an empty or short
    value reaching the container is evidence of a broken launcher or an
    empty-export clobber — falling through would hide the incident and
    silently switch the seat's billing lane.
    """
    oauth = environ.get(_OAUTH)
    key = environ.get(_API_KEY)

    if oauth is not None and not _plausible(oauth, OAUTH_MIN_LEN):
        return CredentialVerdict(
            False,
            "",
            f"{_OAUTH} is set but implausible ({len(oauth)} chars, need "
            f">={OAUTH_MIN_LEN}). Plausible causes: an empty export "
            f"clobbered a good value (last-wins), the secret was deleted "
            f"at the source, or the launcher injected an empty var. "
            f"Refusing to fall through to {_API_KEY} — that would hide "
            f"the fault and silently change billing lane.",
        )
    if key is not None and not _plausible(key, API_KEY_MIN_LEN):
        return CredentialVerdict(
            False,
            "",
            f"{_API_KEY} is set but implausible ({len(key)} chars, need "
            f">={API_KEY_MIN_LEN}). Plausible causes: an empty export "
            f"clobbered a good value, or the launcher injected an empty "
            f"var.",
        )

    if oauth is not None and key is not None:
        return CredentialVerdict(True, "both", "")
    if oauth is not None:
        return CredentialVerdict(True, "oauth", "")
    if key is not None:
        return CredentialVerdict(True, "api-key", "")

    return CredentialVerdict(
        False,
        "",
        f"No Anthropic credential. Set {_OAUTH} (default lane — mint one "
        f"with `claude setup-token` on the edge host and inject it via "
        f"`--env-from-host {_OAUTH}`) or {_API_KEY} (override lane). "
        f"The hub stores only the NAME; the value must exist in the edge "
        f"host's environment at `docker create` time.",
    )


# ------------------------------------------------------------------ contract

_MODES = ("interactive", "headless")


class SeatContractError(Exception):
    """The env doesn't satisfy docs/seat-image.md. Message names the var."""


@dataclass(frozen=True)
class SeatContract:
    identity: str
    project: str
    hub_url: str
    mode: str
    prompt: str
    squads: str
    repo: str
    # What this seat was STOOD UP TO DO, and the material to do it with.
    # Both default empty, which is every seat placed before they existed.
    brief: str = ""
    inputs: tuple[tuple[str, str], ...] = ()
    # Seconds before a headless turn is killed (124 written as its verdict).
    # 0 = unbounded — the interactive default, where a human is watching.
    # Headless defaults to HEADLESS_TIMEOUT_DEFAULT at parse time: nobody
    # watches a headless seat, so a hung turn would hold the container (and
    # its "running" report) forever.
    timeout: int = 0


# The brief and its material, as they land inside the container. Relative to
# the agent's workdir so a pod's inhabitants each get their own copy and can
# never read each other's — the alternative (one shared /seat/brief.md) makes
# "give these two agents different jobs" unexpressible.
BRIEF_FILE = "BRIEF.md"
INPUTS_DIR = "inputs"

# Where a headless run's result survives. Under ~/.claude because that is
# what the memory volume mounts — the ONE directory that outlives reclaim
# (docker logs die with `docker rm`, and `docker exec` harvest cannot touch
# an exited container: both measured 2026-08-08). In a SUBDIR, not loose in
# ~/.claude: that volume is the agent's MEMORY dir, and a result artifact
# sitting in its root will eventually be read as memory by something
# (mcp-hub-fireblade-wsl's constraint, and right).
#
# Two names because there are two readers with two roots: inside the
# container the path hangs off HOME (`~/.claude/seat-results/...`); reading
# the volume after reclaim, the volume root IS `.claude`, so the artifact
# sits at `seat-results/...` from there.
HEADLESS_RESULTS_SUBDIR = "seat-results"
HEADLESS_RESULTS_DIR = ".claude/" + HEADLESS_RESULTS_SUBDIR

# 30 minutes. Long enough for a real errand, short enough that a hung turn
# does not hold a container (and its `running` report) hostage overnight.
HEADLESS_TIMEOUT_DEFAULT = 1800

# The exit code a timed-out headless turn records — timeout(1)'s own dialect,
# and disjoint from EXIT_AUTH (42) / EXIT_CONTRACT (43).
EXIT_TIMEOUT = 124


def headless_result_paths(home: str, identity: str) -> dict[str, str]:
    """Absolute paths of the three artifact files for one headless run.

    output.log — the turn's stdout, teed as it arrives (partial on timeout,
    deliberately: the partial output is the evidence that diagnoses the hang).
    result.json — the structured verdict written after the turn.
    exit_code — the bare integer, cheap to read from a shell.
    """
    base = os.path.join(home, HEADLESS_RESULTS_DIR, identity)
    return {
        "output": os.path.join(base, "output.log"),
        "result": os.path.join(base, "result.json"),
        "exit_code": os.path.join(base, "exit_code"),
    }


def headless_verdict(exit_code: int, timed_out: bool,
                     output: str) -> dict[str, Any]:
    """The result.json body: liveness (exit code) + the turn's own verdict.

    The exit code is a WEAK signal — `claude -p` exits 0 because the CLI ran,
    not because the task was done. `--output-format json` makes the turn emit
    a structured final record (`is_error`, `subtype`, `result`), so the
    verdict is something the TURN asserts. An unparsable output degrades to
    the exit code alone rather than failing: the artifact must always be
    written, or a broken run leaves nothing to diagnose it with.
    """
    doc: dict[str, Any] = {"exit_code": exit_code, "timed_out": timed_out}
    try:
        rec = json.loads(output)
        if isinstance(rec, dict):
            for k in ("is_error", "subtype", "result", "num_turns",
                      "total_cost_usd", "duration_ms", "session_id"):
                if k in rec:
                    doc[k] = rec[k]
    except ValueError:
        doc["unparsed"] = True
    return doc


def _parse_inputs(raw: str) -> tuple[tuple[str, str], ...]:
    """`SEAT_INPUTS` (JSON {filename: content}) → ordered pairs.

    Names are checked HERE rather than trusted: the edge writes this value
    from a seat spec that lives in the hub's database, so a `../` in a
    filename would let anything able to write a spec drop a file anywhere the
    container user can reach — including `~/.claude/settings.json`, which
    would be arbitrary hook execution on the next launch.
    """
    if not raw.strip():
        return ()
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise SeatContractError(
            f"SEAT_INPUTS is not valid JSON ({e}). The edge generates it from "
            f"the seat spec's `inputs`; a hand-edited value is the usual cause."
        ) from e
    if not isinstance(doc, dict):
        raise SeatContractError(
            f"SEAT_INPUTS must be a JSON object of filename → content, got "
            f"{type(doc).__name__}."
        )
    out = []
    for name, content in doc.items():
        clean = str(name).strip()
        if (not clean or clean != os.path.basename(clean)
                or clean.startswith(".") or "\\" in clean):
            raise SeatContractError(
                f"SEAT_INPUTS filename '{name}' is not a plain filename. "
                f"Paths, parent references and dotfiles are refused: an input "
                f"is material to read, and anything that can escape the "
                f"inputs directory could overwrite the seat's own config."
            )
        out.append((clean, str(content)))
    return tuple(out)


def parse_seat_contract(
    environ: Mapping[str, str],
    origin_url: str | None = None,
) -> SeatContract:
    """Read the seat's env into a contract, or refuse naming what's missing.

    `origin_url` is the workdir's `git remote get-url origin` (resolved by
    the caller — this module does no I/O). Project precedence: explicit
    SEAT_PROJECT > origin-derived org/repo > the identity itself.
    """
    identity = (environ.get("SEAT_IDENTITY") or "").strip()
    if not identity:
        raise SeatContractError(
            "SEAT_IDENTITY is required — the placement ASSIGNS the name; "
            "a container must never derive one from its hostname."
        )
    hub_url = (environ.get("MCP_HUB_URL") or "").strip()
    if not hub_url:
        raise SeatContractError(
            "MCP_HUB_URL is required — .mcp.json is generated from it at "
            "start, never baked into the image."
        )
    mode = (environ.get("SEAT_MODE") or "interactive").strip()
    if mode not in _MODES:
        raise SeatContractError(
            f"SEAT_MODE '{mode}' unknown — valid: interactive (default), "
            f"headless."
        )
    prompt = environ.get("SEAT_PROMPT", "")
    brief = environ.get("SEAT_BRIEF", "")
    if mode == "headless" and not prompt.strip() and not brief.strip():
        raise SeatContractError(
            "SEAT_MODE=headless requires SEAT_PROMPT or SEAT_BRIEF — a "
            "one-shot claude with no instruction does nothing and exits, "
            "which the edge would read as a crash."
        )
    raw_timeout = (environ.get("SEAT_TIMEOUT") or "").strip()
    if raw_timeout:
        try:
            timeout = int(raw_timeout)
        except ValueError:
            raise SeatContractError(
                f"SEAT_TIMEOUT '{raw_timeout}' is not an integer number of "
                f"seconds. 0 means unbounded."
            ) from None
        if timeout < 0:
            raise SeatContractError(
                f"SEAT_TIMEOUT {timeout} is negative — use 0 for unbounded."
            )
    else:
        # Unset means the MODE's default, not zero: nobody watches a headless
        # seat, so unbounded-by-omission would let a hung turn hold the
        # container (still reporting `running`) forever.
        timeout = HEADLESS_TIMEOUT_DEFAULT if mode == "headless" else 0

    project = (environ.get("SEAT_PROJECT") or "").strip()
    if not project and origin_url:
        # Same parse as the cli's derived identity — ssh aliases and https
        # forms resolve to the same org/repo.
        from mcp_hub.cli import _parse_org_repo

        parsed = _parse_org_repo(origin_url)
        if parsed:
            project = f"{parsed[0]}/{parsed[1]}"
    if not project:
        project = identity

    return SeatContract(
        identity=identity,
        project=project,
        hub_url=hub_url,
        mode=mode,
        prompt=prompt,
        squads=environ.get("SEAT_SQUADS", ""),
        repo=environ.get("SEAT_REPO", ""),
        brief=environ.get("SEAT_BRIEF", ""),
        inputs=_parse_inputs(environ.get("SEAT_INPUTS", "")),
        timeout=timeout,
    )


# ------------------------------------------------------- N seats, one container
#
# Contract: docs/n-seats-per-container.md. `SEAT_MANIFEST` present means the
# container holds a POD — several agents, one lifecycle. Absent means today's
# 1:1 contract, verbatim and unchanged; that is the whole compatibility story
# and it is why nothing deployed shifts under this.
#
# The design goal here is that NOTHING downstream learns a second shape:
# `agent_contract()` hands back the same `SeatContract` the 1:1 path builds, so
# marker_content, mcp_json_content, launch_argv and first_turn_prompt are used
# unmodified. A pod is N ordinary seats sharing a HOME and a lifecycle, not a
# new kind of thing.

_MANIFEST = "SEAT_MANIFEST"


@dataclass(frozen=True)
class PodAgent:
    """One inhabitant, as the manifest declares it."""
    identity: str
    project: str
    repo: str
    squads: str
    # Empty means "take the pod's brief". Distinguishing "no brief of my own"
    # from "briefed with nothing" matters: the second would silently strip a
    # spike team's shared brief from one member.
    brief: str = ""


@dataclass(frozen=True)
class PodContract:
    agents: tuple[PodAgent, ...]
    hub_url: str
    mode: str
    squad: str
    # Pod-level, and per-agent below it. A spike team is briefed as a TEAM —
    # "here is the question, here is the material" — so the pod-level brief is
    # the common case and `agents[].brief` refines it for one member.
    brief: str = ""
    inputs: tuple[tuple[str, str], ...] = ()


def _tmux_safe(name: str) -> bool:
    """tmux reads `.` as its pane separator, so a dotted session name produces
    an agent that RUNS and cannot be addressed — measured on the fleet when a
    dotted agent name shipped. `:` is tmux's window separator, same problem."""
    return "." not in name and ":" not in name


def parse_pod_manifest(environ: Mapping[str, str]) -> PodContract | None:
    """The pod contract, or None when this is an ordinary 1:1 seat.

    None is the 1:1 signal and must stay cheap: an image running the old
    contract never touches any of this.
    """
    raw = (environ.get(_MANIFEST) or "").strip()
    if not raw:
        return None

    if (environ.get("SEAT_IDENTITY") or "").strip():
        raise SeatContractError(
            f"{_MANIFEST} and SEAT_IDENTITY are both set — a container that "
            f"can be read as both a pod and a single seat has an ambiguous "
            f"identity, and ambiguity is how a message reaches the wrong "
            f"lane. Send one or the other."
        )

    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise SeatContractError(
            f"{_MANIFEST} is not valid JSON ({e}). It is generated by the "
            f"edge from the seat spec's `agents`; a hand-edited value is the "
            f"usual cause."
        ) from e

    # An object leaves room for pod-level fields (the workspace name, later
    # additions) without minting another env var; a bare list is unambiguous
    # and refusing it would be pedantry rather than a guard.
    if isinstance(doc, list):
        rows, squad = doc, ""
    elif isinstance(doc, dict):
        rows, squad = doc.get("agents"), str(doc.get("squad") or "")
    else:
        raise SeatContractError(
            f"{_MANIFEST} must be a JSON object with `agents`, or a JSON "
            f"list of agents — got {type(doc).__name__}."
        )
    if not isinstance(rows, list) or not rows:
        raise SeatContractError(
            f"{_MANIFEST} declares no agents. An empty pod would start a "
            f"container that runs nothing and reports healthy."
        )

    hub_url = (environ.get("MCP_HUB_URL") or "").strip()
    if not hub_url:
        raise SeatContractError(
            "MCP_HUB_URL is required — .mcp.json is generated from it at "
            "start, never baked into the image."
        )
    mode = (environ.get("SEAT_MODE") or "interactive").strip()
    if mode not in _MODES:
        raise SeatContractError(
            f"SEAT_MODE '{mode}' unknown — valid: interactive (default), "
            f"headless."
        )
    if mode == "headless":
        # One PROMPT cannot address N agents, and guessing which one it meant
        # is exactly the kind of choice that puts work in the wrong lane.
        #
        # ⚠️ A BRIEF is not subject to this and must not be confused with it.
        # A prompt is a single turn's input, consumed once, by one claude. A
        # brief is a FILE every inhabitant reads, which is precisely what a
        # spike team needs — so `SEAT_BRIEF` is supported for pods while
        # `SEAT_PROMPT` stays refused.
        raise SeatContractError(
            "SEAT_MODE=headless has no meaning for a pod — SEAT_PROMPT is "
            "single-valued and a pod has several agents. Place headless seats "
            "1:1. (A pod-wide BRIEF is a different thing and is supported: "
            "see SEAT_BRIEF.)"
        )

    agents: list[PodAgent] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise SeatContractError(
                f"{_MANIFEST} agent #{i} is {type(r).__name__}, not an object."
            )
        ident = str(r.get("identity") or "").strip()
        if not ident:
            raise SeatContractError(
                f"{_MANIFEST} agent #{i} has no identity — the placement "
                f"ASSIGNS every name; a container must never derive one."
            )
        if not _tmux_safe(ident):
            raise SeatContractError(
                f"{_MANIFEST} agent '{ident}' contains `.` or `:`, which tmux "
                f"reads as its pane and window separators — the session would "
                f"start and be unaddressable, so the agent would run and "
                f"nobody could reach it."
            )
        if ident in seen:
            # Two agents of one name share a workdir, a marker and a session:
            # whichever registers last silently owns the identity, which is
            # the collapse that derived identity exists to prevent.
            raise SeatContractError(
                f"{_MANIFEST} names '{ident}' twice. Identities are unique "
                f"per pod — duplicates would share a workdir and a session, "
                f"and the last to register would silently own the name."
            )
        seen.add(ident)
        agents.append(PodAgent(
            identity=ident,
            project=str(r.get("project") or "").strip(),
            repo=str(r.get("repo") or "").strip(),
            squads=str(r.get("squads") or "").strip(),
            brief=str(r.get("brief") or ""),
        ))

    return PodContract(
        agents=tuple(agents), hub_url=hub_url, mode=mode, squad=squad,
        brief=environ.get("SEAT_BRIEF", ""),
        inputs=_parse_inputs(environ.get("SEAT_INPUTS", "")),
    )


def agent_contract(
    pod: PodContract,
    agent: PodAgent,
    origin_url: str | None = None,
) -> SeatContract:
    """One inhabitant as an ordinary SeatContract.

    Deliberately returns the SAME type the 1:1 path produces, so every helper
    downstream — marker, .mcp.json, launch argv, first-turn prompt — is reused
    rather than reimplemented. Project precedence matches
    `parse_seat_contract` exactly: explicit > origin-derived > the identity.
    """
    project = agent.project
    if not project and origin_url:
        from mcp_hub.cli import _parse_org_repo

        parsed = _parse_org_repo(origin_url)
        if parsed:
            project = f"{parsed[0]}/{parsed[1]}"
    if not project:
        project = agent.identity
    return SeatContract(
        identity=agent.identity,
        project=project,
        hub_url=pod.hub_url,
        mode=pod.mode,
        prompt="",
        squads=agent.squads,
        repo=agent.repo,
        # Per-agent brief REPLACES the pod's rather than appending to it: a
        # member given its own brief has been singled out deliberately, and
        # concatenating would hand it two sets of instructions with no way to
        # tell which governs.
        brief=agent.brief or pod.brief,
        inputs=pod.inputs,
    )


def pod_workspace(pod: PodContract, workdirs: Mapping[str, str]) -> dict:
    """The `.code-workspace` written INSIDE the container.

    This file is the point of the pod shape: it makes one Dev Containers
    window a squad view. Folders are the agents' workdirs as they exist in
    here — container paths, never the host's, because nothing on the host can
    see them.
    """
    return {
        "folders": [{"name": a.identity, "path": workdirs[a.identity]}
                    for a in pod.agents if a.identity in workdirs],
        "settings": {},
    }


# ------------------------------------------------------- fetching the code
#
# A container has no ssh key, no ~/.gitconfig and no credential store, so the
# clone step in seat-entry could never have worked against a private repo — and
# measured 2026-08-07, it had never run at all: every seat on the fleet was
# handed a host bind-mount instead, so its workdir was never empty.
#
# The credential rides the SAME channel as the Anthropic one: the hub stores
# the NAME, the edge host supplies the VALUE via `--env-from-host`. Nothing new
# to learn, nothing secret in the control plane.

# PREFIXED, and never the bare `GITHUB_TOKEN` — dt, 2026-08-07, from their own
# post-mortem (`codespace-runner.js:1138-1151`): the factory injected a
# deps-READ token under that name, it clobbered the environment's native WRITE
# token, and every agent push failed with "Repository not found". The rule they
# paid for is ONE TOKEN PER ROLE, EACH WITH ITS OWN NAME — a read token that
# borrows a well-known name will silently shadow a write token.
#
# We are not in a codespace today so nothing would collide yet, which is
# exactly why the name is worth fixing NOW: seat-image.md aims this image at
# the factory estate too, and the collision would appear only there, only on a
# push, long after anyone connected it to this line.
SEAT_GITHUB_TOKEN = "SEAT_GITHUB_TOKEN"

# github.com, always. A seat spec written on a machine with ssh aliases carries
# `git@github-monkeypashion:org/repo.git`, and that alias exists only in that
# machine's ~/.ssh/config — inside a container it resolves to nothing.
_GITHUB_HTTPS = "https://github.com/{org}/{repo}.git"


def https_repo_url(url: str) -> str:
    """Any git remote form → the https URL a container can actually fetch.

    Reuses `_parse_org_repo`, which already ignores the host entirely, so an
    ssh alias and a canonical URL land on the same answer. Returns "" when the
    form is unparseable rather than guessing — a wrong URL would clone the
    wrong repo under the right name, which nothing downstream could detect.
    """
    from mcp_hub.cli import _parse_org_repo

    parsed = _parse_org_repo(url or "")
    if not parsed:
        return ""
    return _GITHUB_HTTPS.format(org=parsed[0], repo=parsed[1])


def credential_helper_argv(token_var: str = SEAT_GITHUB_TOKEN) -> list[str]:
    """`git config --global` argv installing an ENV-READING credential helper.

    The token is never written to disk. The obvious alternative — cloning from
    `https://user:$TOKEN@github.com/...` — persists the credential verbatim in
    `.git/config` as `remote.origin.url`, where it survives the container, ends
    up in `git remote -v`, and would be read back by our own project
    derivation. A helper reads the variable at the moment git asks, and leaves
    nothing behind.

    `x-access-token` is the username GitHub expects for token auth; the
    password field carries the token.
    """
    helper = (
        f'!f() {{ echo username=x-access-token; echo "password=${token_var}"; }}; f'
    )
    return ["git", "config", "--global", "credential.helper", helper]


# ------------------------------------------------------------ file contents


def marker_content(contract: SeatContract) -> dict:
    """The legacy-marker shape `_discover_agent_from_marker` reads.

    The marker (not derivation) is how an ASSIGNED identity wins inside a
    container: the repo is deliberately NOT opted into ~/.mcp-hub/config.json
    `projects`, so derivation never applies and the marker is authoritative.
    """
    return {"name": contract.identity, "project": contract.project}


def mcp_json_content(contract: SeatContract) -> dict:
    """Generated from MCP_HUB_URL at start — the same `?agent=` stamping as
    compose_capsule's mcp.json.template. Safe here because a container's
    ~/.claude.json is per-seat, not shared (the 2026-07-27 misroute needed a
    SHARED file)."""
    sep = "&" if "?" in contract.hub_url else "?"
    return {
        "mcpServers": {
            "hub": {
                "type": "http",
                "url": f"{contract.hub_url}{sep}agent={contract.identity}",
            }
        }
    }


def hooks_settings_content() -> dict:
    """Container-local ~/.claude/settings.json — the standard fleet contract.

    `async: True` on the heartbeat daemon is load-bearing (without it the
    hook runner kills the daemon on return). `enabledMcpjsonServers` lives
    here because user-scope settings are CONFIG and survive; the same flag
    seeded into ~/.claude.json is session STATE and measured not to
    (2026-08-04, 3 of 4 paths had lost it).
    """
    return {
        # A THEME is not cosmetic here: it is the first step of claude's
        # first-run wizard, and a fresh container HOME has none. Measured on
        # the first live seat — container running, tmux alive, claude parked
        # on the theme picker forever, never registering. Half the fix; the
        # other half is onboarding_state() in ~/.claude.json.
        "theme": "dark",
        # Bypass mode has its OWN one-time acceptance dialog, and its
        # default row is "No, exit" — so the seat's first-turn Enter landed
        # on it and confirmed its own exit, cleanly, code 0. Key taken from
        # the claude binary's settings schema ("Whether the user has
        # accepted the bypass permissions mode dialog"), not guessed.
        #
        # Seeding it is legitimate ONLY because card #360 exists: the
        # operator accepted this posture explicitly, so the image records
        # THEIR decision. An image that pre-accepted a dangerous-mode
        # dialog nobody had agreed to would be smuggling one.
        "skipDangerousModePermissionPrompt": True,
        "enabledMcpjsonServers": ["hub"],
        # Pre-granted COMMS, nothing else. Measured: the seat called
        # register() on its first turn and stopped on the tool-permission
        # dialog — a container has nobody to click Yes, so the capability
        # that makes it an agent has to be config, the way the fleet's own
        # repos already do it in .claude/settings.local.json.
        #
        # Enumerated rather than server-wide (`mcp__hub` would allow every
        # tool, including ones added later and ones that mutate OTHER
        # agents' state). No Bash/Edit/Write here: joining a hub is not a
        # licence to run commands — those grants are the operator's to
        # make, per repo.
        "permissions": {
            # THE CONTAINER IS THE SANDBOX (operator decision 2026-08-05,
            # card #360). A seat that cannot run a command is an observer,
            # not a worker — the first live seat woke, went to work, and
            # stopped on `git status` with nobody inside to approve it.
            # Docker already bounds the blast radius; bounding it a second
            # time with a dialog nobody can answer only means no work
            # happens.
            #
            # ⚠️ This is sound ONLY while the seat is genuinely contained:
            # non-root user, no host mounts beyond its own memory volume,
            # and NO DOCKER SOCKET. A spec that mounts the socket turns
            # this from a sandbox into root on the host — see
            # docs/seat-image.md.
            "defaultMode": "bypassPermissions",
            # Kept alongside the mode on purpose: if a policy ever disables
            # it, the seat can still reach the hub to say it is stuck.
            "allow": list(SEAT_ALLOWED_TOOLS),
        },
        "hooks": {
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "mcp-hub stop-hook"}
                    ],
                }
            ],
            "SessionStart": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "mcp-hub session-start",
                        },
                        {
                            "type": "command",
                            "command": "mcp-hub heartbeat-daemon",
                            "async": True,
                        },
                    ],
                }
            ],
        },
    }


def launch_argv(contract: SeatContract, workdir: str,
                session: str = "seat") -> list[str]:
    """The command PID 1 hands claude to.

    Interactive: claude under a named tmux session so hub push-wake works
    (channels flag) and the operator can `docker exec -it <seat> tmux
    attach -t seat`. Headless: `claude -p` with the exit code passed
    through — codespace-runner's shape, reserved for the factory merge.

    `session` defaults to `seat` so every 1:1 container keeps the exact name
    the attach affordance and the launch dance already use. A pod passes the
    agent's identity instead, because N sessions cannot share one name.
    """
    if contract.mode == "headless":
        # A brief STANDS IN for a prompt here. The brief is already on disk
        # beside the seat, so pointing at it beats inlining: the text stays
        # readable in the workdir afterwards, it can be arbitrarily long
        # without fighting an argv limit, and it can reference ./inputs/.
        # An explicit prompt still wins — it is the more specific instruction.
        #
        # `--output-format json` (verified against 2.1.226 in the image, not
        # assumed): the exit code only says the CLI ran, so the turn's own
        # structured record (`is_error`/`subtype`/`result`) is what carries
        # the task verdict into result.json. It costs no live visibility —
        # text mode also prints nothing until the turn ends (measured).
        if contract.prompt:
            return ["claude", "-p", contract.prompt,
                    "--output-format", "json"]
        material = (f" Your material is in ./{INPUTS_DIR}/."
                    if contract.inputs else "")
        return ["claude", "-p",
                f"Read ./{BRIEF_FILE} and carry it out.{material}",
                "--output-format", "json"]
    return [
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        workdir,
        "claude --dangerously-load-development-channels server:hub",
    ]


def onboarding_state(claude_version: str) -> dict:
    """Keys that tell claude its first-run wizard is already done.

    A container's HOME is fresh every time, so without these claude opens
    the onboarding wizard and BLOCKS — a seat that looks perfectly healthy
    to `docker ps` and never becomes an agent. Version-stamped because
    claude re-onboards when its version outruns the recorded one.
    """
    return {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": claude_version,
    }


# ------------------------------------------------------------- launch dance

# Flattened dialog tokens. Matching is on FLATTENED text (letters/digits
# only, lowercased) because a narrow pane re-wraps dialog text mid-word —
# squad learned this on a live wedge; a phrase with spaces silently stops
# matching at 80 columns.
_CHANNELS_TOKEN = "loadingdevelopmentchannels"
# claude's own chrome: proof it is past the dialog phase and running.
#
# CAPTURED from a live seat (2.1.222), not composed. The first version of
# this list was written from an INVENTED fixture ("Bypassing Permissions")
# and rejected a perfectly healthy pane whose footer actually reads
# "⏵⏵ bypass permissions on (shift+tab to cycle)" — the same
# paraphrase-instead-of-capture mistake this module warns about elsewhere.
#
# `bypasspermissionson` deliberately, NOT `bypasspermissions`: the bypass
# ACCEPTANCE DIALOG says "Bypass Permissions mode", and a token matching
# both would call that dialog settled — the exact confusion that let a
# blind Enter confirm "No, exit".
_CHROME_TOKENS = (
    "forshortcuts",
    "shifttabtocycle",
    "bypasspermissionson",
    "acceptedits",
)


def _flatten(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def startup_dance_action(pane_text: str) -> str | None:
    """Which key answers the dialog currently on the pane, if any.

    The seat launches claude INSIDE the container, so squad's host-side
    launch dance cannot reach it — this is the same idea, ported and
    narrowed to the one dialog a seat can hit (no --continue, so no
    resume dialog; trust is already seeded).

    Option 1 ("I am using this for local development") is the default and
    is correct here: `server:hub` IS our own hub.
    """
    flat = _flatten(pane_text)
    if _CHANNELS_TOKEN in flat:
        return "Enter"
    return None


def first_turn_is_safe(pane_text: str) -> bool:
    """Whether typing into this pane can only reach a PROMPT.

    The rule bought by the fifth gate: a blind Enter is safe only when
    claude's own chrome is visible. Any dialog — known or unknown, present
    or future — must stop the first turn rather than receive a keystroke
    that happens to land on whatever row is default today. The bypass
    dialog's default was "No, exit", so the seat confirmed its own death.
    """
    return pane_is_settled(pane_text)


def pane_is_settled(pane_text: str) -> bool:
    """True once claude's chrome is visible — the dance loop's exit.

    Without this the loop would burn its whole timeout on every healthy
    start, delaying registration for no reason.
    """
    flat = _flatten(pane_text)
    return any(t in flat for t in _CHROME_TOKENS)


def first_turn_prompt(contract: SeatContract) -> str:
    """The message the seat types to itself to start its FIRST turn.

    A container has no operator to type anything, and claude's
    SessionStart `additionalContext` — which carries the register
    instruction — is only consumed when a turn actually runs. Measured on
    the first live seat: hooks fired, the heartbeat daemon ran, and
    `~/.claude/projects/` was empty, so no turn had ever executed and the
    seat never registered despite looking healthy at every other layer.

    Single line by construction: `tmux send-keys -l` sends the buffer
    verbatim, so an embedded newline would submit half a prompt.
    """
    # SEAT_SQUADS was parsed into the contract and then dropped here, while
    # docs/seat-image.md said it was "passed to register()". A seat placed as
    # part of a squad came up squadless, so `capsules compose` — which reads
    # squad_members — froze a squad with no members in it. Empty is omitted
    # rather than sent as "": register treats empty as NO OPINION (so a
    # reconnect cannot drop an agent out of its squads), which means an empty
    # string would be indistinguishable from not asking.
    squads = (f", squads=\"{contract.squads}\"" if contract.squads else "")
    # WITHOUT this the brief is a file nobody opens. A seat's first turn is
    # generated, not typed, so a brief written to disk and never mentioned is
    # invisible — the agent registers, sees an empty workdir listing at best,
    # and stands by exactly as if it had been given no job at all. Presence is
    # not flow: writing the file is not delivering the brief.
    if contract.brief:
        n = len(contract.inputs)
        material = (
            f" Your material is in ./{INPUTS_DIR}/ ({n} file(s))." if n else ""
        )
        tail = (
            f"then READ ./{BRIEF_FILE} — it is what you were stood up to do —"
            f"{material} and begin."
        )
    else:
        tail = "then stand by for instructions."
    return (
        f"You are {contract.identity}, a containerized seat on project "
        f"{contract.project}. Call register(name=\"{contract.identity}\", "
        f"project=\"{contract.project}\"{squads}) on the hub now to bind this "
        f"session for wake, {tail}"
    )


def brief_files(contract: SeatContract) -> dict[str, str]:
    """Relative path → content, for everything the brief puts in a workdir.

    Returned as data rather than written here so the whole thing stays a pure
    function the tests can check without a filesystem — the same reason every
    other artifact in this module is rendered, not written.
    """
    out: dict[str, str] = {}
    if contract.brief:
        body = contract.brief if contract.brief.endswith("\n") else \
            contract.brief + "\n"
        out[BRIEF_FILE] = body
    for name, content in contract.inputs:
        out[f"{INPUTS_DIR}/{name}"] = content
    return out


# -------------------------------------------------------------- supervisor

# How old the daemon's status cache may be and still count as evidence.
# The daemon writes every 60s, so two missed writes means something is
# wrong with the DAEMON, not with the binding.
STATUS_STALE_SECONDS = 150.0


def needs_reregister(status: dict | None, now: float,
                     stale_after: float = STATUS_STALE_SECONDS,
                     after: float = 0.0) -> bool:
    """Whether this seat has fallen off the hub and should nudge itself.

    Every hub deploy drops all bindings. A human-driven agent re-registers
    at its next turn boundary, because someone types something; an IDLE
    container has no turn boundary and stays silently offline forever —
    measured 2026-08-05, a seat up 5 hours, healthy, and absent from
    `list_agents`.

    The evidence rule decides everything else here: only a FRESH status
    file saying `online: false` is grounds to act. A missing, stale, or
    undated file says nothing about presence, and treating that silence as
    "offline" would nudge a healthy seat on every cycle exactly when the
    daemon is the thing that broke.
    """
    if not status:
        return False
    ts = status.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    if now - ts > stale_after:
        return False
    # Evidence must postdate our last action. A cache written before we
    # nudged (or before the first turn ran) describes the world we were
    # trying to change, and acting on it types into a pane that is already
    # mid-registration — measured: a nudge 6s after the first turn.
    if ts <= after:
        return False
    return status.get("online") is False


# ---- making a placed capsule OPENABLE --------------------------------------
#
# `compose` freezes a squad and `place` writes one placement per seat; the edge
# then makes containers exist. None of that gives the operator a way IN. The
# capsule's own `bootstrap.sh` was meant to close the gap and is a stub — four
# lines of comment, no logic — so standing up a squad meant running
# `squad add-container` by hand once per seat and hand-editing a workspace file.
#
# This decides what to do; the caller does it. Nothing here touches the disk,
# so the whole policy is testable without a machine, and `--dry-run` prints
# exactly what a real run would perform rather than a second description of it.

ATTACH_SKIP = "skip"
ATTACH_ENROL = "enrol"
ATTACH_PRESENT = "already"
ATTACH_REFUSE = "refuse"


def capsule_attach_plan(
    seats: list[dict[str, Any]],
    this_machine: str,
    enrolled: set[str],
    dir_exists,
) -> list[tuple[str, str, str]]:
    """[(identity, action, detail)] for one capsule on THIS machine.

    A missing work directory is REFUSED, never created. Docker would happily
    create a missing bind-mount source — owned by root — and the seat runs as
    uid 1000, so it would come up unable to write its own worktree. That was
    the first of the six gates between "container running" and "agent on hub",
    and it is invisible to `docker ps`. A directory that is supposed to hold a
    repo is not something to conjure silently.
    """
    from mcp_hub.fleet_tree import container_image, seat_folder

    out: list[tuple[str, str, str]] = []
    for s in seats:
        ident = str(s.get("identity") or "")
        if not ident:
            continue
        if not container_image(s):
            # A worktree seat is a normal agent; it has a tab already.
            out.append((ident, ATTACH_SKIP, "not a container seat"))
            continue
        machine = str(s.get("machine") or "")
        if machine and this_machine and machine != this_machine:
            # Paths and the roster are per-machine. Enrolling another box's
            # seat here would add a row whose folder does not exist.
            out.append((ident, ATTACH_SKIP, f"belongs to {machine}"))
            continue
        folder = seat_folder(s)
        if not folder:
            out.append((ident, ATTACH_REFUSE, "no work mount in its spec"))
            continue
        if not dir_exists(folder):
            out.append((ident, ATTACH_REFUSE, f"no such folder: {folder}"))
            continue
        if ident in enrolled:
            out.append((ident, ATTACH_PRESENT, folder))
            continue
        out.append((ident, ATTACH_ENROL, folder))
    return out
