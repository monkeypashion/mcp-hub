"""v2-fleet r19 (F61, T76; j6-a-bad-key-file-is-no-key): a key file that is
not UTF-8 is no key, for every ephemeral client.

_agent_key_headers read the lane's key file as UTF-8 and caught only
OSError, so a key file that is not UTF-8 raised UnicodeDecodeError out of
every ephemeral client (send, the Stop hook's query, the heartbeat daemon,
...). THE RULE: such a file is malformed like any other: _agent_key_headers
returns {} and the client connects without a key, as with no key file.

Off the seat: temporary HOME and MCP_HUB_STATE_DIR; K3 replaces
mcp.client.streamable_http.streamablehttp_client with a recorder that raises
a sentinel, so no connection is made. Each case catches any exception and
fails by assertion naming it.
FALSIFIERS (red on the parent): K1, K2, K3. K4 and GUARDS (5) to (7) are
green on the parent. The carried tests are (8), in the same command.
"""

from __future__ import annotations

import io
import json
import os
import secrets
from pathlib import Path

import pytest

from mcp_hub import cli

HEADER = "x-mcp-hub-agent-key"
LANE = "lane-bin"
KEY = secrets.token_hex(32)


class _Sentinel(Exception):
    pass


@pytest.fixture(autouse=True)
def _private(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state" / "hub"))
    monkeypatch.delenv("MCP_HUB_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MCP_SERVER_NAME", raising=False)
    monkeypatch.setattr(cli, "_git_remote_url", lambda _cwd: None)   # no git
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))


def _key_file(name: str = LANE) -> Path:
    path = Path(os.environ["MCP_HUB_STATE_DIR"]) / "agent-keys" / f"{name}.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _bad() -> None:
    _key_file().write_bytes(b"\xff\xfe" + KEY.encode())


def _call(fn, *a):
    try:
        return fn(*a), ""
    except Exception as exc:  # noqa: BLE001
        return f"raised {type(exc).__name__}", f"{type(exc).__name__}: {exc}"


def _no_run(text: str) -> None:
    for i in range(len(KEY) - 7):
        assert KEY[i:i + 8] not in text, "an 8-character run of the key leaked"


# =============================================================================
# K1 K2 K3 (falsifiers) and K4
# =============================================================================


def test_k1_the_headers_are_empty_and_nothing_raises():
    _bad()
    got, exc = _call(cli._agent_key_headers, LANE)
    assert got == {}, f"_agent_key_headers gave {got!r}"
    _no_run(repr(got) + exc)


def test_k2_the_client_kwargs_have_no_headers():
    _bad()
    got, exc = _call(cli._with_agent_key, LANE)
    assert got == {}, f"_with_agent_key gave {got!r}"
    _no_run(repr(got) + exc)


def test_k3_send_connects_keyless_and_names_no_codec(monkeypatch, capsys):
    _bad()
    import mcp.client.streamable_http as sh

    seen: list[dict] = []

    def _recorder(*a, **kw):
        seen.append(dict(kw))
        raise _Sentinel("no hub here")

    monkeypatch.setattr(sh, "streamablehttp_client", _recorder)
    capsys.readouterr()
    rc, exc = _call(cli.main, ["send", "--from", LANE, "--to", "x", "--message", "m",
                               "--hub-url", "http://127.0.0.1:9/mcp"])
    out = capsys.readouterr()
    assert seen, f"the client was never reached (rc {rc!r}): {out.err[-300:]}{exc}"
    assert "headers" not in seen[0], "a key header was sent from a bad key file"
    text = out.out + out.err + exc
    assert "codec" not in text and "UnicodeDecodeError" not in text, text[-300:]
    _no_run(text)


# =============================================================================
# GUARDS
# =============================================================================


def test_5_a_well_formed_key_still_gives_its_header():
    _key_file().write_text(KEY + "\n", encoding="utf-8")
    assert cli._agent_key_headers(LANE) == {HEADER: KEY}
    assert cli._with_agent_key(LANE) == {"headers": {HEADER: KEY}}


@pytest.mark.parametrize("content", [None, KEY[:63], KEY.upper()],
                         ids=["no-file", "63-hex", "64-uppercase"])
def test_6_no_or_malformed_key_gives_empty(content):
    if content is not None:
        _key_file().write_text(content + "\n", encoding="utf-8")
    assert cli._agent_key_headers(LANE) == {}
    assert cli._with_agent_key(LANE) == {}


def test_7_the_header_action_prints_empty_and_logs_malformed(tmp_path, monkeypatch, capsys):
    _bad()
    folder = tmp_path / "work" / "lane"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "hub-agent.json").write_text(json.dumps({"name": LANE}))
    monkeypatch.chdir(folder)
    capsys.readouterr()
    rc, exc = _call(cli.main, ["agent-key", "header"])
    out = capsys.readouterr()
    assert rc == 0 and out.out == "{}\n", (rc, out.out, exc)
    log = Path(os.environ["MCP_HUB_STATE_DIR"]) / "agent-key-header.jsonl"
    lines = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    assert lines and lines[-1]["reason"] == "malformed", lines
    _no_run(log.read_text() + out.err)
