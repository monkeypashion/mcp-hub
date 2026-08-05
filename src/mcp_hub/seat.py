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

from dataclasses import dataclass
from typing import Mapping

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
    if mode == "headless" and not prompt.strip():
        raise SeatContractError(
            "SEAT_MODE=headless requires SEAT_PROMPT — a one-shot claude "
            "with no prompt does nothing and exits, which the edge would "
            "read as a crash."
        )

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
    )


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


def launch_argv(contract: SeatContract, workdir: str) -> list[str]:
    """The command PID 1 hands claude to.

    Interactive: claude under a named tmux session so hub push-wake works
    (channels flag) and the operator can `docker exec -it <seat> tmux
    attach -t seat`. Headless: `claude -p` with the exit code passed
    through — codespace-runner's shape, reserved for the factory merge.
    """
    if contract.mode == "headless":
        return ["claude", "-p", contract.prompt]
    return [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "seat",
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
_CHROME_TOKENS = ("forshortcuts", "bypassingpermissions", "acceptedits")


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
    return (
        f"You are {contract.identity}, a containerized seat on project "
        f"{contract.project}. Call register(name=\"{contract.identity}\", "
        f"project=\"{contract.project}\") on the hub now to bind this "
        f"session for wake, then stand by for instructions."
    )
