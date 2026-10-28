"""v2-fleet r18 follow-up (F57 v5, T72 v7; j6-every-cli-verb-is-dispatched):
one list of verbs.

A CLI verb lived in three hand-kept places: cli.build_parser()'s subparsers,
server._CLI_SUBCOMMANDS (the set server.main checks argv[1] against before
handing argv to cli.main) and cli.main's if-chain. A verb missing from the set
answers the server parser's 'unrecognized arguments'; a verb missing from the
chain falls through to print_help and exits 0. THE RULE: the parser is the
only list. Each top-level verb's parser carries its handler, cli.main runs the
handler of the verb parsed (no if-chain), and server.main dispatches on the
verbs the parser defines when it runs. A verb with no handler fails loudly
before anything runs: a non-zero exit (or a raise) with an error naming the
verb and saying it has "no handler". Every existing verb runs exactly the code
it runs today; server._CLI_SUBCOMMANDS stays importable and equal to the
parser's verbs.

The seams this file pins (the item text names the behaviour, not these):
  * a verb is added through ONE seam: cli.build_parser wrapped, so the parser
    it returns has one more top-level verb. server.main and cli.main must
    both call cli.build_parser when they run.
  * a verb's handler is its subparser's default `handler`
    (set_defaults(handler=fn)), called as fn(args) and returning the rc.
  * the functions today's chain calls are cli module globals (stop_hook_command
    ... hibernate_command) and load_meter.run and lane_doorbell.command, looked
    up when the verb runs; R4 replaces each with a recorder.

Off the seat: temporary HOME and MCP_HUB_STATE_DIR; no server starts (V2's
recorder returns 0; X2 and X3 exit before any server code; V3 is a subprocess
with no opt-in record).
FALSIFIERS (red on the parent by behaviour): X1, X2, X3. REGRESSION (green on
the parent): V1, V2, V3, R4. GUARDS: (5), (6). The carried
tests/test_cli_subcommands_reachable.py, test_cli_discoverability.py, r17
lane-doorbell tests and test_cli.py are (7), in the same command.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_hub import cli, server

# Today's chain: verb -> (module, function name) it calls.
CHAIN = {
    "stop-hook": ("cli", "stop_hook_command"),
    "session-start": ("cli", "session_start_command"),
    "agent-key": ("cli", "agent_key_command"),
    "session-rewake": ("cli", "session_rewake_command"),
    "heartbeat-daemon": ("cli", "heartbeat_daemon_command"),
    "onboard": ("cli", "onboard_command"),
    "memory-export": ("cli", "memory_export_command"),
    "memory-import": ("cli", "memory_import_command"),
    "memory-verify": ("cli", "memory_verify_command"),
    "identity": ("cli", "identity_command"),
    "settings": ("cli", "settings_command"),
    "board": ("cli", "settings_command"),
    "mute": ("cli", "mute_command"),
    "transport-history": ("cli", "transport_history_command"),
    "rebind-url": ("cli", "rebind_url_command"),
    "edge": ("cli", "edge_command"),
    "workspaces": ("cli", "workspaces_command"),
    "seats": ("cli", "seats_command"),
    "placements": ("cli", "placements_command"),
    "capsules": ("cli", "capsules_command"),
    "squads": ("cli", "squads_command"),
    "machines": ("cli", "machines_command"),
    "send": ("cli", "send_command"),
    "focus": ("cli", "focus_command"),
    "voice-client": ("cli", "voice_client_command"),
    "voice-host": ("cli", "voice_host_command"),
    "seat-entry": ("cli", "seat_entry_command"),
    "hibernate": ("cli", "hibernate_command"),
    "load-meter": ("load_meter", "run"),
    "lane-doorbell": ("lane_doorbell", "command"),
}
# The minimal arguments each verb needs to parse.
MINIMAL = {
    "agent-key": ["header"],
    "mute": ["--agent", "a", "--squad", "s", "--state", "muted"],
    "transport-history": ["--from-cwd", "/a", "--to-cwd", "/b"],
    "send": ["--to", "x", "--message", "m"],
    "machines": ["list"],
    "workspaces": ["list"],
    "seats": ["list"],
    "placements": ["list"],
    "edge": ["watch"],
    "capsules": ["list"],
    "squads": ["list"],
    "hibernate": ["--thread", "t"],
    "load-meter": ["--hub-db", "h", "--snapshot", "s", "--out", "o",
                   "--baseline-start", "2026-10-01", "--baseline-end", "2026-10-02"],
}


@pytest.fixture(autouse=True)
def _private(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state" / "hub"))
    monkeypatch.delenv("MCP_HUB_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("setup: cli.build_parser() has no top-level verbs")


def _parser_verbs() -> set[str]:
    verbs = set(_subparsers(cli.build_parser()).choices)
    assert verbs, "setup: cli.build_parser() has no top-level verbs"
    return verbs


def _with_verb(monkeypatch, name: str, handler=None) -> None:
    """THE SEAM: cli.build_parser wrapped so its parser has one more verb."""
    original = cli.build_parser

    def _wrapped(*a, **k):
        parser = original(*a, **k)
        p = _subparsers(parser).add_parser(name, help=f"r18 probe verb {name}")
        p.add_argument("--x", action="store_true")
        if handler is not None:
            p.set_defaults(handler=handler)
        return parser

    monkeypatch.setattr(cli, "build_parser", _wrapped)


def _run(fn) -> tuple[object, str]:
    """(exit code, return value or what it raised; all output and messages)."""
    out, err = io.StringIO(), io.StringIO()
    text = ""
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code: object = fn()
        except SystemExit as exc:
            code = exc.code if exc.code is not None else 0
            if isinstance(exc.code, str):
                text = exc.code
        except Exception as exc:  # noqa: BLE001
            code = f"raised {type(exc).__name__}"
            text = str(exc)
    return code, "\n".join((out.getvalue(), err.getvalue(), text))


# =============================================================================
# X1 A VERB WITH NO HANDLER FAILS LOUDLY
# =============================================================================


def test_x1_a_verb_with_no_handler_exits_non_zero_naming_itself(monkeypatch):
    _with_verb(monkeypatch, "probe-verb")
    code, text = _run(lambda: cli.main(["probe-verb"]))
    assert code not in (0, None), f"a verb with no handler exited {code!r}: {text[-400:]}"
    assert "probe-verb" in text and "no handler" in text, text[-600:]


# =============================================================================
# X2 THE CONSOLE PATH SAYS THE SAME
# =============================================================================


def test_x2_the_console_path_names_the_verb_with_no_handler(monkeypatch):
    _with_verb(monkeypatch, "probe-verb")
    monkeypatch.setattr(sys, "argv", ["mcp-hub", "probe-verb"])
    code, text = _run(server.main)
    assert code not in (0, None), f"exit {code!r}: {text[-400:]}"
    assert "unrecognized arguments" not in text, f"the server parser refused it: {text[-400:]}"
    assert "probe-verb" in text and "no handler" in text, text[-600:]


# =============================================================================
# X3 A NEW VERB NEEDS ONE PLACE
# =============================================================================


def test_x3_a_verb_added_only_to_the_parser_runs_from_the_console(monkeypatch):
    calls: list[argparse.Namespace] = []

    def _handler(args):
        calls.append(args)
        return 7

    _with_verb(monkeypatch, "probe-ok", _handler)
    monkeypatch.setattr(sys, "argv", ["mcp-hub", "probe-ok", "--x"])
    code, text = _run(server.main)
    assert len(calls) == 1, f"the handler ran {len(calls)} times: {text[-400:]}"
    assert calls[0].x is True, vars(calls[0])
    assert code == 7, f"exit {code!r}, not the handler's rc: {text[-400:]}"


# =============================================================================
# V1 V2 V3 (regression)
# =============================================================================


def test_v1_the_dispatch_set_equals_the_parsers_verbs():
    verbs = _parser_verbs()
    dispatch = set(server._CLI_SUBCOMMANDS)
    assert not sorted(verbs - dispatch), f"parsed, not dispatched: {sorted(verbs - dispatch)}"
    assert not sorted(dispatch - verbs), f"dispatched, not parsed: {sorted(dispatch - verbs)}"


def test_v2_every_verb_reaches_cli_main_with_argv_unchanged(monkeypatch):
    verbs = sorted(_parser_verbs())
    seen: list[list[str]] = []

    def _recorder(argv=None):
        seen.append(list(argv) if argv is not None else ["<none>"])
        return 0

    monkeypatch.setattr(cli, "main", _recorder)
    wrong = []
    for verb in verbs:
        seen.clear()
        monkeypatch.setattr(sys, "argv", ["mcp-hub", verb, "--x"])
        code, _text = _run(server.main)
        got = seen[0] if seen else None
        if code != 0 or got != [verb, "--x"]:
            wrong.append((verb, code, got))
    assert not wrong, f"(verb, exit, argv cli.main got): {wrong}"


_ENTRY = "import sys; from mcp_hub.server import main; sys.argv[0] = 'mcp-hub'; main()"


def _entry(*argv: str) -> subprocess.CompletedProcess:
    src = str(Path(cli.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    return subprocess.run([sys.executable, "-c", _ENTRY, *argv], stdin=subprocess.DEVNULL,
                          capture_output=True, env=env, timeout=60)


def test_v3_the_console_entry_runs_the_lane_doorbells_status():
    p = _entry("lane-doorbell", "--name", "probe-lane")
    out = p.stdout.decode()
    assert p.returncode == 0, f"rc {p.returncode}: {p.stderr.decode()[-400:]}"
    assert "--transport" not in out, "the server's help, not the lane doorbell"
    assert "probe-lane" in out and "not opted in" in out, out


def test_v3_lane_doorbell_help_is_its_own_usage():
    p = _entry("lane-doorbell", "--help")
    out = p.stdout.decode()
    assert p.returncode == 0, f"rc {p.returncode}: {p.stderr.decode()[-400:]}"
    assert "--transport" not in out, "the server's help, not lane-doorbell's"
    assert "--from-session-start" in out, out[:600]


# =============================================================================
# R4 EACH VERB RUNS TODAY'S CODE (regression)
# =============================================================================


def test_r4_each_verb_runs_exactly_the_function_it_runs_today(monkeypatch):
    from mcp_hub import lane_doorbell, load_meter

    modules = {"cli": cli, "load_meter": load_meter, "lane_doorbell": lane_doorbell}
    calls: list[tuple[str, argparse.Namespace]] = []

    def _recorder(key):
        def _rec(args, *_a, **_k):
            calls.append((key, args))
            return 0
        return _rec

    for mod, fn in set(CHAIN.values()):
        assert hasattr(modules[mod], fn), f"setup: {mod}.{fn} is gone"
        monkeypatch.setattr(modules[mod], fn, _recorder(f"{mod}.{fn}"))
    assert set(CHAIN) == _parser_verbs(), (
        f"the verbs changed: {sorted(set(CHAIN) ^ _parser_verbs())}")
    wrong = []
    for verb, (mod, fn) in sorted(CHAIN.items()):
        calls.clear()
        code, text = _run(lambda: cli.main([verb, *MINIMAL.get(verb, [])]))
        got = [k for k, _a in calls]
        if code != 0 or got != [f"{mod}.{fn}"]:
            wrong.append((verb, code, got, text[-200:] if not got else ""))
        if verb == "board" and got == ["cli.settings_command"]:
            a = calls[0][1]
            if (getattr(a, "tui", None), getattr(a, "cwd", "?"), getattr(a, "json", None)) \
                    != (True, None, False):
                wrong.append(("board", "settings", (getattr(a, "tui", None),
                              getattr(a, "cwd", "?"), getattr(a, "json", None)), ""))
    assert not wrong, f"(verb, exit, functions called): {wrong}"


# =============================================================================
# GUARDS
# =============================================================================


def test_5_the_top_level_help_lists_every_verb_and_is_the_servers(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mcp-hub", "--help"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--transport" in out, "a bare server.main no longer reaches the server's parser"
    missing = sorted(v for v in _parser_verbs() if v not in out)
    assert not missing, missing


def test_6_cli_main_with_no_verb_prints_help_and_returns_0(capsys):
    rc = cli.main([])
    assert rc == 0
    assert "usage" in capsys.readouterr().out.lower()
