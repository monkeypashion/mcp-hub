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
        "enabledMcpjsonServers": ["hub"],
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
