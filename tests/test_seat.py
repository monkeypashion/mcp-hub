"""Seat-container credential validation — the door check of mcp-hub-seat.

Contract under test (docs/seat-image.md, "Auth: validate, never arbitrate"):
the entrypoint enforces presence + plausibility ONLY. Which credential wins
when both are valid is Claude Code's own hierarchy — never reimplemented
here, so there is deliberately no test asserting a winner.

The rules encode two real factory incidents (dt, codespace-runner.js:2288):
an empty `export` clobbering a good token, and a deleted secret warning into
a log nobody read for five weeks. Hence: length checks, not set-ness checks,
and loud refusal at the door.
"""

import pytest

from mcp_hub import cli as cli_mod
from mcp_hub.seat import (
    API_KEY_MIN_LEN,
    EXIT_AUTH,
    EXIT_CONTRACT,
    OAUTH_MIN_LEN,
    SeatContractError,
    hooks_settings_content,
    launch_argv,
    marker_content,
    mcp_json_content,
    parse_seat_contract,
    validate_seat_credentials,
)

# Plausible-shaped values, comfortably over the thresholds.
GOOD_OAUTH = "sk-ant-oat01-" + "x" * 100
GOOD_KEY = "sk-ant-api03-" + "x" * 80


def test_exit_code_is_the_factory_convention():
    # 42 distinguishes auth-death from build failure in BOTH estates —
    # changing it silently breaks the shared dialect.
    assert EXIT_AUTH == 42


def test_valid_oauth_token_alone_passes():
    v = validate_seat_credentials({"CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH})
    assert v.ok
    assert v.lane == "oauth"
    assert v.error == ""


def test_valid_api_key_alone_passes():
    v = validate_seat_credentials({"ANTHROPIC_API_KEY": GOOD_KEY})
    assert v.ok
    assert v.lane == "api-key"


def test_both_valid_reports_both_and_arbitrates_nothing():
    v = validate_seat_credentials(
        {"CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH, "ANTHROPIC_API_KEY": GOOD_KEY}
    )
    assert v.ok
    # "both", not a winner — precedence is CC's, and unmeasured (dt 2026-08-04).
    assert v.lane == "both"


def test_no_credential_at_all_is_fatal_and_names_both_vars():
    v = validate_seat_credentials({})
    assert not v.ok
    assert "CLAUDE_CODE_OAUTH_TOKEN" in v.error
    assert "ANTHROPIC_API_KEY" in v.error
    # The fix path is named, not just the failure.
    assert "setup-token" in v.error


def test_short_oauth_token_is_fatal():
    v = validate_seat_credentials(
        {"CLAUDE_CODE_OAUTH_TOKEN": "x" * (OAUTH_MIN_LEN - 1)}
    )
    assert not v.ok
    # The error must name the clobber incident's shape as a plausible cause.
    assert "empty" in v.error or "clobber" in v.error


def test_short_oauth_is_fatal_even_with_a_valid_api_key():
    """The load-bearing rule: NO silent lane switch.

    A set-but-implausible OAuth token is evidence of the empty-export
    clobber. Falling through to the API key would hide the incident AND
    silently move the seat from subscription to API billing.
    """
    v = validate_seat_credentials(
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "x" * (OAUTH_MIN_LEN - 1),
            "ANTHROPIC_API_KEY": GOOD_KEY,
        }
    )
    assert not v.ok


def test_empty_string_oauth_is_fatal_not_a_fallthrough():
    # `-e CLAUDE_CODE_OAUTH_TOKEN=` (empty) is anomalous by construction:
    # the edge OMITS unset env_from_host names, so empty means a broken
    # launcher, not an absent credential.
    v = validate_seat_credentials(
        {"CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_API_KEY": GOOD_KEY}
    )
    assert not v.ok


def test_absent_oauth_with_valid_key_is_the_override_lane():
    # ABSENT (never set) is the legitimate API-key-only seat — distinct
    # from empty-string-set, which is the clobber shape above.
    v = validate_seat_credentials({"ANTHROPIC_API_KEY": GOOD_KEY})
    assert v.ok
    assert v.lane == "api-key"


def test_short_api_key_is_fatal():
    v = validate_seat_credentials(
        {"ANTHROPIC_API_KEY": "x" * (API_KEY_MIN_LEN - 1)}
    )
    assert not v.ok


def test_whitespace_only_token_is_fatal():
    # Presence is not validity; neither is length made of spaces.
    v = validate_seat_credentials(
        {"CLAUDE_CODE_OAUTH_TOKEN": " " * (OAUTH_MIN_LEN + 10)}
    )
    assert not v.ok


def test_boundary_lengths_pass_exactly_at_threshold():
    assert validate_seat_credentials(
        {"CLAUDE_CODE_OAUTH_TOKEN": "x" * OAUTH_MIN_LEN}
    ).ok
    assert validate_seat_credentials(
        {"ANTHROPIC_API_KEY": "x" * API_KEY_MIN_LEN}
    ).ok


def test_thresholds_are_dts_measured_numbers():
    # >=50 (observed tokens 110-145 chars) and >=20 — from the factory's
    # hard-fail shape, not invented here.
    assert OAUTH_MIN_LEN == 50
    assert API_KEY_MIN_LEN == 20


# ---------------------------------------------------------------- contract --

BASE_ENV = {
    "SEAT_IDENTITY": "probe-seat-1",
    "MCP_HUB_URL": "http://100.109.6.114:8090/mcp",
    "CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH,
}


def test_contract_minimal_env_parses():
    c = parse_seat_contract(BASE_ENV)
    assert c.identity == "probe-seat-1"
    assert c.hub_url == "http://100.109.6.114:8090/mcp"
    assert c.mode == "interactive"  # the default


def test_contract_missing_identity_names_the_var():
    env = dict(BASE_ENV)
    del env["SEAT_IDENTITY"]
    with pytest.raises(SeatContractError, match="SEAT_IDENTITY"):
        parse_seat_contract(env)


def test_contract_missing_hub_url_names_the_var():
    env = dict(BASE_ENV)
    del env["MCP_HUB_URL"]
    with pytest.raises(SeatContractError, match="MCP_HUB_URL"):
        parse_seat_contract(env)


def test_contract_rejects_unknown_mode_naming_valid_set():
    with pytest.raises(SeatContractError, match="interactive.*headless"):
        parse_seat_contract({**BASE_ENV, "SEAT_MODE": "detached"})


def test_contract_headless_requires_prompt():
    with pytest.raises(SeatContractError, match="SEAT_PROMPT"):
        parse_seat_contract({**BASE_ENV, "SEAT_MODE": "headless"})


def test_project_precedence_explicit_beats_origin_beats_identity():
    origin = "git@github-monkeypashion:monkeypashion/mcp-hub.git"
    # explicit wins
    c = parse_seat_contract(
        {**BASE_ENV, "SEAT_PROJECT": "org/explicit"}, origin_url=origin
    )
    assert c.project == "org/explicit"
    # origin-derived next — same parse as the cli, so ssh aliases resolve
    c = parse_seat_contract(BASE_ENV, origin_url=origin)
    assert c.project == "monkeypashion/mcp-hub"
    # identity as last resort
    c = parse_seat_contract(BASE_ENV)
    assert c.project == "probe-seat-1"


def test_marker_round_trips_through_the_clis_actual_reader(tmp_path):
    """Cross-contract test: the marker we WRITE must be read by the marker
    reader the hooks actually USE — not by a re-implementation of it."""
    from mcp_hub.cli import _discover_agent_from_marker

    c = parse_seat_contract(BASE_ENV)
    marker_dir = tmp_path / ".claude"
    marker_dir.mkdir()
    import json as _json

    (marker_dir / "hub-agent.json").write_text(
        _json.dumps(marker_content(c))
    )
    name, project = _discover_agent_from_marker(str(tmp_path))
    assert name == "probe-seat-1"
    assert project == "probe-seat-1"  # identity fallback in BASE_ENV


def test_mcp_json_stamps_agent_into_url():
    c = parse_seat_contract(BASE_ENV)
    j = mcp_json_content(c)
    url = j["mcpServers"]["hub"]["url"]
    assert url.startswith("http://100.109.6.114:8090/mcp")
    assert "agent=probe-seat-1" in url


def test_hooks_settings_carry_the_fleet_contract():
    s = hooks_settings_content()
    hooks = s["hooks"]
    stop = hooks["Stop"][0]["hooks"][0]["command"]
    assert "stop-hook" in stop
    starts = hooks["SessionStart"][0]["hooks"]
    cmds = [h["command"] for h in starts]
    assert any("session-start" in c for c in cmds)
    daemon = [h for h in starts if "heartbeat-daemon" in h["command"]]
    # async:true is load-bearing — without it the runner kills the daemon.
    assert daemon and daemon[0].get("async") is True
    # The durable MCP pre-approval lives HERE (user-scope settings), not in
    # ~/.claude.json session state — measured 2026-08-04, it doesn't survive.
    assert s["enabledMcpjsonServers"] == ["hub"]


def test_interactive_launch_runs_claude_under_tmux_with_channels():
    c = parse_seat_contract(BASE_ENV)
    argv = launch_argv(c, workdir="/home/seat/work")
    assert argv[0] == "tmux"
    # Every flag asserted EXACTLY — a stray "-d," (real defect, caught by
    # eye not by the first version of this test) passes loose containment.
    assert "-d" in argv
    assert "seat" in argv  # the named session the operator attaches to
    joined = " ".join(argv)
    assert "--dangerously-load-development-channels server:hub" in joined
    assert "/home/seat/work" in argv  # tmux -c workdir


def test_headless_launch_is_claude_dash_p():
    c = parse_seat_contract(
        {**BASE_ENV, "SEAT_MODE": "headless", "SEAT_PROMPT": "do the thing"}
    )
    argv = launch_argv(c, workdir="/home/seat/work")
    assert argv[:2] == ["claude", "-p"]
    assert "do the thing" in argv


def test_exit_codes_are_distinct():
    assert EXIT_CONTRACT == 43
    assert EXIT_CONTRACT != EXIT_AUTH


# --------------------------------------------------------------- seat-entry --


def _entry(argv, env, monkeypatch, tmp_path):
    """Run seat-entry with a controlled env + HOME, return exit code."""
    for k in (
        "SEAT_IDENTITY", "SEAT_PROJECT", "SEAT_REPO", "MCP_HUB_URL",
        "SEAT_MODE", "SEAT_PROMPT", "SEAT_SQUADS",
        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    return cli_mod.main(["seat-entry", *argv])


def test_entry_no_credential_exits_42(monkeypatch, tmp_path, capsys):
    rc = _entry(
        ["--prepare-only", "--workdir", str(tmp_path / "work")],
        {"SEAT_IDENTITY": "s1", "MCP_HUB_URL": "http://hub/mcp"},
        monkeypatch,
        tmp_path,
    )
    assert rc == EXIT_AUTH
    assert "REFUSED (auth)" in capsys.readouterr().err


def test_entry_no_identity_exits_43(monkeypatch, tmp_path, capsys):
    rc = _entry(
        ["--prepare-only", "--workdir", str(tmp_path / "work")],
        {"MCP_HUB_URL": "http://hub/mcp",
         "CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH},
        monkeypatch,
        tmp_path,
    )
    assert rc == EXIT_CONTRACT
    assert "SEAT_IDENTITY" in capsys.readouterr().err


def test_entry_headless_is_refused_as_reserved(monkeypatch, tmp_path, capsys):
    rc = _entry(
        ["--prepare-only", "--workdir", str(tmp_path / "work")],
        {"SEAT_IDENTITY": "s1", "MCP_HUB_URL": "http://hub/mcp",
         "SEAT_MODE": "headless", "SEAT_PROMPT": "go",
         "CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH},
        monkeypatch,
        tmp_path,
    )
    assert rc == EXIT_CONTRACT
    assert "reserved" in capsys.readouterr().err


def test_entry_prepare_only_writes_the_contract_files(
    monkeypatch, tmp_path, capsys
):
    import json as _json

    work = tmp_path / "work"
    rc = _entry(
        ["--prepare-only", "--workdir", str(work)],
        {"SEAT_IDENTITY": "probe-seat-1", "MCP_HUB_URL": "http://hub/mcp",
         "CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH},
        monkeypatch,
        tmp_path,
    )
    assert rc == 0
    # marker readable by the cli's actual reader
    from mcp_hub.cli import _discover_agent_from_marker

    name, project = _discover_agent_from_marker(str(work))
    assert name == "probe-seat-1"
    # .mcp.json generated from MCP_HUB_URL
    mcp = _json.loads((work / ".mcp.json").read_text())
    assert mcp["mcpServers"]["hub"]["url"].startswith("http://hub/mcp")
    # hook settings in the container-local home
    settings = _json.loads(
        (tmp_path / "home" / ".claude" / "settings.json").read_text()
    )
    assert settings["enabledMcpjsonServers"] == ["hub"]
    # trust seeded for the workdir
    claude_json = _json.loads(
        (tmp_path / "home" / ".claude.json").read_text()
    )
    assert claude_json["projects"][str(work)]["hasTrustDialogAccepted"] is True
    # the summary names the credential lane
    assert "subscription OAuth" in capsys.readouterr().out


def test_entry_is_idempotent_and_preserves_existing_settings(
    monkeypatch, tmp_path, capsys
):
    import json as _json

    work = tmp_path / "work"
    env = {"SEAT_IDENTITY": "s1", "MCP_HUB_URL": "http://hub/mcp",
           "CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH}
    assert _entry(["--prepare-only", "--workdir", str(work)], env,
                  monkeypatch, tmp_path) == 0
    # Operator (or memory volume) settings must not be clobbered on restart.
    settings_path = tmp_path / "home" / ".claude" / "settings.json"
    settings_path.write_text(_json.dumps({"custom": True}))
    assert _entry(["--prepare-only", "--workdir", str(work)], env,
                  monkeypatch, tmp_path) == 0
    assert _json.loads(settings_path.read_text()) == {"custom": True}
    assert "left untouched" in capsys.readouterr().err


# --------------------------------------------------------------- onboarding --


def test_onboarding_state_marks_the_wizard_done():
    """A fresh container HOME makes claude open its first-run wizard (theme
    picker) and wait forever — measured on the first live seat: container
    running, tmux alive, claude never started a session, never registered.
    'Running' meant nothing."""
    from mcp_hub.seat import onboarding_state

    s = onboarding_state("2.1.221")
    assert s["hasCompletedOnboarding"] is True
    # Version-stamped: claude re-onboards when its version outruns this.
    assert s["lastOnboardingVersion"] == "2.1.221"


def test_settings_carry_a_theme_so_the_picker_never_opens():
    # The theme picker is the FIRST wizard step and lives in settings.json,
    # not ~/.claude.json — both halves are needed or the seat still blocks.
    assert hooks_settings_content()["theme"]


def test_entry_seeds_onboarding_without_clobbering_trust(
    monkeypatch, tmp_path
):
    import json as _json

    work = tmp_path / "work"
    rc = _entry(
        ["--prepare-only", "--workdir", str(work)],
        {"SEAT_IDENTITY": "s1", "MCP_HUB_URL": "http://hub/mcp",
         "CLAUDE_CODE_OAUTH_TOKEN": GOOD_OAUTH},
        monkeypatch,
        tmp_path,
    )
    assert rc == 0
    data = _json.loads((tmp_path / "home" / ".claude.json").read_text())
    assert data["hasCompletedOnboarding"] is True
    assert data.get("lastOnboardingVersion")
    # The trust seed shares this file — one must not overwrite the other.
    assert data["projects"][str(work)]["hasTrustDialogAccepted"] is True


# ------------------------------------------------------------- launch dance --

# The real dialog, captured from the live seat 2026-08-04 — not paraphrased.
# claude pops it for --dangerously-load-development-channels and WAITS.
CHANNELS_DIALOG = """
  WARNING: Loading development channels

  --dangerously-load-development-channels is for local channel development
  only. Do not use this option to run channels you have downloaded off the
  internet.

  Please use --channels to run a list of approved channels.

  Channels: server:hub

  > 1. I am using this for local development
    2. Exit

  Enter to confirm - Esc to cancel
"""

STARTED_PANE = """
> Try "how does authentication work in this codebase?"

  ? for shortcuts                                  Bypassing Permissions
"""


def test_dance_answers_the_channels_dialog():
    """The seat launches claude itself, so it needs its own launch dance —
    squad's lives on the host and cannot reach inside a container."""
    from mcp_hub.seat import startup_dance_action

    assert startup_dance_action(CHANNELS_DIALOG) == "Enter"


def test_dance_is_wrap_tolerant():
    """A narrow pane re-wraps dialog text mid-word, so matching must be on
    flattened tokens — never on phrases with spaces (squad's rule, learned
    on a live wedge)."""
    from mcp_hub.seat import startup_dance_action

    wrapped = CHANNELS_DIALOG.replace("development\n", "developme\nnt\n")
    assert startup_dance_action(wrapped) == "Enter"


def test_dance_does_nothing_once_claude_is_up():
    from mcp_hub.seat import startup_dance_action

    assert startup_dance_action(STARTED_PANE) is None


def test_dance_does_not_match_an_empty_pane():
    from mcp_hub.seat import startup_dance_action

    assert startup_dance_action("") is None


def test_pane_is_settled_only_when_chrome_shows():
    """Exit condition for the dance loop: claude's own chrome. Without it
    the loop would spin the full timeout on every healthy start."""
    from mcp_hub.seat import pane_is_settled

    assert pane_is_settled(STARTED_PANE)
    assert not pane_is_settled(CHANNELS_DIALOG)


# ------------------------------------------------------------- first turn --


def test_first_turn_prompt_names_the_assigned_identity():
    """MEASURED: hooks fire, the heartbeat daemon runs, and claude still
    never registers — because ~/.claude/projects/ is empty, i.e. no turn
    ever ran. The SessionStart register instruction rides in
    additionalContext, which is only consumed BY a turn. A fleet agent gets
    its first turn from the operator; a container has no operator, so the
    seat must start its own."""
    from mcp_hub.seat import first_turn_prompt

    c = parse_seat_contract(BASE_ENV)
    p = first_turn_prompt(c)
    assert "probe-seat-1" in p
    assert "register" in p.lower()


def test_first_turn_prompt_is_single_line():
    """tmux send-keys -l sends the buffer verbatim: an embedded newline
    would submit a half-typed prompt and leave the rest as a second turn."""
    from mcp_hub.seat import first_turn_prompt

    assert "\n" not in first_turn_prompt(parse_seat_contract(BASE_ENV))


def test_first_turn_prompt_carries_the_project_for_register():
    from mcp_hub.seat import first_turn_prompt

    c = parse_seat_contract({**BASE_ENV, "SEAT_PROJECT": "org/thing"})
    assert "org/thing" in first_turn_prompt(c)
