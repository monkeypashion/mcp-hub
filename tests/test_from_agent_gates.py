"""W1.3 — from_agent hardening (docs/verification/wave-1.md, C0-C4).

C0, the instrument: these tests drive REAL MCP sessions over a real socket —
the in-process harness cannot inject a Context, which left 8 of the
_attribution call sites with no wiring test and one declared-VACUOUS notice
(test_url_rebind.py). Every session here presents
client_info.name="claude-code" DELIBERATELY: the is_interactive_client gate
already refuses a stock test client (name='mcp'), so without the spoof a
"cannot bind" assertion passes against unfixed code for the wrong reason —
the recorded vacuous shape (pressure-test F1).

The design being tested is verify-when-BOUND, stated honestly: an UNBOUND
session's assertion stays accepted BY DESIGN (stop-hook drains, daemons,
reconnects; register() is deliberately ungated — parked #17). The gate
catches the one provable mis-attribution: a session that OWNS a name
asserting a different one.
"""

import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from mcp_hub.server import create_server

CLAUDE = Implementation(name="claude-code", version="1.0.0")


@pytest.fixture
async def hub_url(tmp_path: Path):
    """A real streamable-http hub on localhost (the live_hub pattern,
    test_cli.py:437) — real sessions produce real server-side Contexts."""
    import time as _time
    import urllib.error
    import urllib.request

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = create_server(db_path=tmp_path / "gates.db",
                           host="127.0.0.1", port=port)

    def _serve():
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.run_streamable_http_async())
        except Exception:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
        except urllib.error.HTTPError:
            break
        except (urllib.error.URLError, ConnectionError, OSError):
            _time.sleep(0.1)
            continue
        else:
            break
    yield f"http://127.0.0.1:{port}/mcp"


@asynccontextmanager
async def _session(url: str, client: Implementation = CLAUDE):
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write, client_info=client) as s:
            await s.initialize()
            yield s


async def _call(s: ClientSession, tool: str, **args) -> str:
    res = await s.call_tool(tool, args)
    return "".join(c.text for c in res.content if hasattr(c, "text"))


# ---------------------------------------------------------------------------
# C0 — the instrument proves itself before any refusal is trusted
# ---------------------------------------------------------------------------


class TestPositiveControls:
    async def test_the_harness_can_bind_and_speak(self, hub_url):
        """Positive control: register binds this real session, and a
        verified own-name call succeeds. If this fails, every REFUSED
        assertion below is an instrument failure, not a contract verdict."""
        async with _session(hub_url) as s:
            out = await _call(s, "register", name="alice", project="p")
            assert "Registered as 'alice'" in out
            assert "pong" in await _call(s, "ping", from_agent="alice")

    async def test_the_spoof_is_load_bearing(self, hub_url):
        """A stock client (name != claude-code) is refused a TOUCH-binding
        by the is_interactive_client gate — the reason every other test here
        must spoof. This is the F1 control: if this ever starts binding,
        the vacuous-shape risk the spoof guards against is gone and the
        suite should be revisited."""
        stock = Implementation(name="mcp", version="1.0.0")
        async with _session(hub_url, client=stock) as s:
            await _call(s, "register", name="reg-bound", project="p")
            # register() binds regardless of clientInfo (deliberately
            # ungated) — but ping's touch_session path must not bind a
            # DIFFERENT existing name for a non-claude-code client, so the
            # cross-name refusal here comes from _attribution (bound via
            # register), proving the gate is reachable even for stock
            # clients once bound.
            out = await _call(s, "ping", from_agent="reg-bound")
            assert "pong" in out


# ---------------------------------------------------------------------------
# C1 — a bound session cannot speak as a name it does not own
# ---------------------------------------------------------------------------


class TestBoundRefusals:
    """Per-writer wiring tests through the real socket. The four newly
    gated writers (ping, create_channel, subscribe_channel, memory_put)
    fail these pre-fix. Mutation, per writer: remove that writer's
    _attribution call → its test fails."""

    async def _bound(self, url):
        s_ctx = _session(url)
        s = await s_ctx.__aenter__()
        await _call(s, "register", name="alice", project="p")
        return s_ctx, s

    async def test_send_refuses(self, hub_url):
        async with _session(hub_url) as s:
            await _call(s, "register", name="alice", project="p")
            out = await _call(s, "send", from_agent="bob", to="alice",
                              message="hi")
            assert "REFUSED" in out and "bound to alice" in out

    async def test_ping_refuses_and_does_not_bind(self, hub_url):
        """THE new gate that matters most: pre-fix, ping(from_agent=X) from
        a session bound to A rebound X's wake target to A's session — the
        quietest impersonation primitive. Fails pre-fix."""
        async with _session(hub_url) as s1:
            await _call(s1, "register", name="victim", project="p")
            async with _session(hub_url) as s2:
                await _call(s2, "register", name="alice", project="p")
                out = await _call(s2, "ping", from_agent="victim")
                assert "REFUSED" in out and "bound to alice" in out
                # The victim's binding survived: its own session still
                # verifies as itself (a rebind would have evicted it).
                assert "pong" in await _call(s1, "ping", from_agent="victim")

    async def test_create_channel_refuses(self, hub_url):
        async with _session(hub_url) as s:
            await _call(s, "register", name="alice", project="p")
            out = await _call(s, "create_channel", name="deploys",
                              created_by="bob")
            assert "REFUSED" in out
            # Deliberate negative held: the channel was NOT created.
            listing = await _call(s, "list_channels")
            assert "deploys" not in listing

    async def test_subscribe_channel_refuses(self, hub_url):
        async with _session(hub_url) as s:
            await _call(s, "register", name="alice", project="p")
            await _call(s, "create_channel", name="qa", created_by="alice")
            out = await _call(s, "subscribe_channel", name="bob",
                              channel="qa", subscribed=False)
            assert "REFUSED" in out

    async def test_memory_put_refuses_forged_provenance(self, hub_url):
        async with _session(hub_url) as s:
            await _call(s, "register", name="alice", project="p")
            out = await _call(s, "memory_put", project="o/r",
                              filename="f.md", content="x", from_agent="bob")
            assert "REFUSED" in out
            assert "staged" not in out


# ---------------------------------------------------------------------------
# C2/C3 — the honest boundary: unbound assertion stays accepted BY DESIGN
# ---------------------------------------------------------------------------


class TestHonestBoundary:
    async def test_unbound_assertion_is_accepted_by_design(self, hub_url):
        """An UNBOUND session may assert any from_agent — reconnects,
        stop-hook drains and daemons depend on it (parked #17; full
        prevention = per-agent credentials, a named deferred decision).
        This test EXISTS so the boundary is stated, not implied."""
        async with _session(hub_url) as s:
            # No register — this session owns nothing.
            out = await _call(s, "memory_put", project="o/r",
                              filename="f.md", content="x",
                              from_agent="anyone")
            assert "staged" in out

    async def test_empty_provenance_from_a_bound_session_still_works(
        self, hub_url
    ):
        """The F5 regression guard: memory_put's from_agent defaults to ''.
        Gating the empty assertion would refuse every provenance-less export
        from a bound agent. Mutation: gate unconditionally → this fails."""
        async with _session(hub_url) as s:
            await _call(s, "register", name="alice", project="p")
            out = await _call(s, "memory_put", project="o/r",
                              filename="f.md", content="x")
            assert "staged" in out

    async def test_own_name_reregister_and_multi_name(self, hub_url):
        # carol must EXIST first: the orphan gate refuses a session that
        # already owns a name registering a NEVER-SEEN second one
        # (server.py's documented refusal) — multi-name is for names the
        # hub already knows.
        async with _session(hub_url) as s0:
            await _call(s0, "register", name="carol", project="p")
        async with _session(hub_url) as s:
            await _call(s, "register", name="alice", project="p")
            assert "Registered as 'alice'" in await _call(
                s, "register", name="alice", project="p")
            await _call(s, "register", name="carol", project="p")
            # A session legitimately bound to several names speaks as any.
            assert "pong" in await _call(s, "ping", from_agent="alice")
            assert "pong" in await _call(s, "ping", from_agent="carol")


# ---------------------------------------------------------------------------
# C4 — a register that displaces a LIVE binding leaves a visible trace
# ---------------------------------------------------------------------------


class TestDisplacementNotice:
    async def test_reconnect_after_death_is_silent(self, hub_url):
        """The ordinary case must NOT nag: the first session is closed
        (dead) before the second registers. Mutation: drop the
        deliverability condition → this fails (every reconnect would
        produce the notice)."""
        async with _session(hub_url) as s1:
            await _call(s1, "register", name="alice", project="p")
        await anyio.sleep(0.2)  # let the transport teardown land
        async with _session(hub_url) as s2:
            await _call(s2, "register", name="alice", project="p")
            inbox = await _call(s2, "get_messages", agent_name="alice")
            assert "DISPLACED" not in inbox

    async def test_displacing_a_live_binding_leaves_the_notice(self, hub_url):
        """The suspicious shape: session1 is still live and deliverable when
        session2 registers the same name. Fails pre-fix (no notice existed).
        Mutation: remove the notice INSERT → this fails."""
        async with _session(hub_url) as s1:
            await _call(s1, "register", name="alice", project="p")
            async with _session(hub_url) as s2:
                await _call(s2, "register", name="alice", project="p")
                inbox = await _call(s2, "get_messages", agent_name="alice")
                assert "DISPLACED" in inbox
