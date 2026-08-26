"""Card #205/#790 — the send receipt names whether anyone is actually there.

"Message queued for 'X'" was byte-for-byte identical whether X was a live
lane about to read it or a name nobody had used for weeks. Queued proves the
name matched a ROW, never that anything will read it. vps measured that the
deployed factory-runner-worker was 128 commits behind, sent the finding to
`dreamteam-lead` — a retired name — got "queued", filed it as delivered, and
the lane owning the gate never heard. Their words: "that one would have
rotted quietly."

Written BEFORE the implementation, per the standing tests-first law.

The operator attached two design constraints when approving, and both are
the fossil lesson one layer down:

  C1 liveness is read at SEND time, never cached — a stale liveness is the
     same defect wearing a different hat;
  C2 an UNREADABLE liveness renders as unknown, NEVER as "online". A receipt
     that claims presence because it could not check is precisely the bug
     being repaired.

And the honest bound, which the tests hold the wording to: this makes a dead
name VISIBLE. It does not make delivery certain — a live idle agent can
still never read the thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_hub.server import create_server


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "test.db")


class _Stream:
    _write_stream = object()

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_ping(self):
        return None

    async def send_notification(self, notification):
        self.sent.append(notification)


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    return result if isinstance(result, str) else str(result)


def _db(server):
    import sqlite3

    conn = sqlite3.connect(server._hub_db_path)
    conn.row_factory = sqlite3.Row
    return conn


async def _sender(server) -> None:
    await _call(server, "register", {"name": "alice", "project": "p"})


# ---- the specimen: a retired name ------------------------------------------


async def test_offline_recipient_receipt_says_so_and_dates_it(server):
    """vps's exact case. The row exists, nobody is behind it, and the
    receipt must not read like a live lane's."""
    await _sender(server)
    await _call(server, "register", {"name": "dreamteam-lead", "project": "p"})
    await _call(server, "unregister", {"name": "dreamteam-lead"})

    out = await _call(server, "send", {
        "from_agent": "alice", "to": "dreamteam-lead", "message": "128 behind",
    })

    low = out.lower()
    assert "offline" in low, f"a retired name read as live: {out!r}"
    assert "last seen" in low, (
        "the receipt must DATE the absence — 'offline' alone doesn't tell a "
        f"sender whether to re-route: {out!r}"
    )


async def test_the_age_is_real_and_not_a_constant(server):
    """Caught by mutation: a `_ago` hard-wired to "never" passed every other
    test here. Dating the absence is the POINT — quiet for two minutes and
    quiet for three weeks call for different actions — so a receipt that
    misdates every absence is no better than one that omits it."""
    await _sender(server)
    await _call(server, "register", {"name": "justgone", "project": "p"})
    await _call(server, "unregister", {"name": "justgone"})

    out = await _call(server, "send", {
        "from_agent": "alice", "to": "justgone", "message": "x"})

    low = out.lower()
    assert "last seen never" not in low, (
        "an agent seen seconds ago was dated 'never' — the age is a "
        f"constant, not a measurement: {out!r}"
    )
    assert "ago" in low, f"no age rendered at all: {out!r}"


async def test_offline_receipt_differs_from_live_receipt(server):
    """The whole defect in one assertion: the two must not be identical."""
    await _sender(server)
    await _call(server, "register", {"name": "dead-name", "project": "p"})
    await _call(server, "unregister", {"name": "dead-name"})
    await _call(server, "register", {"name": "bob", "project": "p"})
    server._hub_registry.bind("bob", _Stream())

    dead = await _call(server, "send", {
        "from_agent": "alice", "to": "dead-name", "message": "x"})
    live = await _call(server, "send", {
        "from_agent": "alice", "to": "bob", "message": "x"})

    # Normalise the RECIPIENT NAME out before comparing. The receipt has
    # always interpolated the name, so a raw `dead != live` is satisfied by
    # the name alone and would pass against the unrepaired code — a test that
    # cannot fail for the reason it was written is not coverage.
    assert dead.replace("dead-name", "N") != live.replace("bob", "N"), (
        "receipt is identical-but-for-the-name for a dead name and a live "
        f"lane — this is the reported defect, unrepaired: {dead!r}"
    )


async def test_live_bound_recipient_reads_as_online(server):
    await _sender(server)
    await _call(server, "register", {"name": "bob", "project": "p"})
    server._hub_registry.bind("bob", _Stream())

    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "hi"})

    assert "online" in out.lower()
    assert "offline" not in out.lower(), f"a live lane read as offline: {out!r}"


# ---- C2: unreadable liveness must never read as presence -------------------


async def test_unreadable_presence_never_renders_as_online(server, monkeypatch):
    """C2 proper: when the PRESENCE read itself fails, nothing may claim the
    recipient is there. This is the fossil lesson one layer down — a receipt
    that says 'online' because the check failed is the defect being
    repaired."""
    await _sender(server)
    await _call(server, "register", {"name": "bob", "project": "p"})
    server._hub_registry.bind("bob", _Stream())

    import sqlite3

    from mcp_hub import server as srv

    # `_get_db` is the module-level seam. Two earlier attempts here proved
    # nothing and are worth naming: patching `sqlite3.connect` arrives too
    # late (the hub caches its handle), and `sqlite3.Connection` is an
    # immutable type so its methods cannot be patched at all.
    real_get_db = srv._get_db

    class _BoomConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if "status, last_seen FROM agents" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._inner, n)

    monkeypatch.setattr(
        srv, "_get_db", lambda *a, **k: _BoomConn(real_get_db(*a, **k))
    )

    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "hi"})

    low = out.lower()
    assert "unknown" in low, (
        f"an unreadable presence must SAY it is unknown: {out!r}")
    assert "online" not in low, (
        "an unreadable PRESENCE rendered as presence — C2 violated: "
        f"{out!r}"
    )
    assert "queued" in low or "sent" in low, (
        "a failed liveness read must not swallow the send itself")


async def test_unreadable_wakeability_does_not_claim_reachability(server, monkeypatch):
    """The narrower half, and the reason the two probes are guarded
    separately: `status` is a DB fact that WAS read, while push-deliverability
    is a transport probe that failed. Reporting the known half is honest;
    what it must not do is imply the unknown half — 'online' must not be
    allowed to read as 'reachable'."""
    await _sender(server)
    await _call(server, "register", {"name": "bob", "project": "p"})
    server._hub_registry.bind("bob", _Stream())

    def _boom(*a, **k):
        raise RuntimeError("registry unavailable")

    # The registry is the real seam — `_can_deliver_push` is a closure inside
    # create_server and cannot be patched at module level (an earlier version
    # of this test patched it there and proved nothing).
    monkeypatch.setattr(server._hub_registry, "sessions", _boom)

    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "hi"})

    low = out.lower()
    assert "unknown" in low, (
        f"a failed wakeability probe must say so: {out!r}")
    assert "not push-bound" not in low, (
        "a FAILED probe was reported as a definite negative — unknown and "
        f"unreachable are different findings: {out!r}"
    )
    assert "queued" in low or "sent" in low


async def test_never_registered_name_is_not_reported_as_merely_offline(server):
    """A name that never existed and a name that went quiet are different
    facts, and only one of them is a typo."""
    await _sender(server)

    out = await _call(server, "send", {
        "from_agent": "alice", "to": "nobody-ever", "message": "x"})

    low = out.lower()
    assert "never registered" in low or "no agent" in low or "unknown" in low, (
        f"a never-seen name read as an ordinary offline agent: {out!r}")


# ---- C1: read at send time, not cached -------------------------------------


async def test_liveness_is_read_at_send_time_not_cached(server):
    """Same sender, same recipient, two sends across a state change. The
    second receipt must reflect the NEW state — a cached liveness would
    repeat the first."""
    await _sender(server)
    await _call(server, "register", {"name": "bob", "project": "p"})
    server._hub_registry.bind("bob", _Stream())

    first = await _call(server, "send",
                        {"from_agent": "alice", "to": "bob", "message": "1"})
    assert "online" in first.lower()

    await _call(server, "unregister", {"name": "bob"})

    second = await _call(server, "send",
                         {"from_agent": "alice", "to": "bob", "message": "2"})
    assert "offline" in second.lower(), (
        "the receipt repeated a stale liveness across a state change — C1 "
        f"violated: {second!r}"
    )


# ---- the honest bound ------------------------------------------------------


async def test_online_receipt_does_not_promise_delivery(server):
    """Approved on the claim 'the receipt stops lying about whether anyone
    is there' — NOT 'messages are guaranteed read'. A live idle agent can
    still never read it, so the wording must not say delivered/read."""
    await _sender(server)
    await _call(server, "register", {"name": "bob", "project": "p"})
    server._hub_registry.bind("bob", _Stream())

    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "hi"})

    low = out.lower()
    for overclaim in ("will read", "guaranteed", "confirmed read", "has read"):
        assert overclaim not in low, (
            f"receipt overclaims delivery ({overclaim!r}): {out!r}")


# ---- it rides every batched path, not just one -----------------------------


@pytest.mark.parametrize("priority", ["low", "normal"])
async def test_every_batched_priority_carries_liveness(server, priority):
    """A liveness that covered one priority and not the other would be
    trusted exactly where it is absent — the same reason the focus gate
    lives in push_channel rather than at four call sites."""
    await _sender(server)
    await _call(server, "register", {"name": "gone", "project": "p"})
    await _call(server, "unregister", {"name": "gone"})

    out = await _call(server, "send", {
        "from_agent": "alice", "to": "gone", "message": "x",
        "priority": priority,
    })

    assert "offline" in out.lower(), (
        f"priority={priority} receipt carries no liveness: {out!r}")
