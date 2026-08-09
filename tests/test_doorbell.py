"""The doorbell: telling a machine "pull now" instead of waiting for its timer.

Operator, 2026-08-09: *"on the substrate side where we do a similar thing we
have a door bell that notifies when a desired state changes so that we don't
have to wait for the regular check."* Measured cost of not having one: a wake
took 95s and a sleep 96s, almost entirely spent waiting for the next tick.

The design is borrowed in SHAPE from `dreamteam` 0d17942 (read, not guessed)
and from vps-hetzner's independently-arrived-at version — but NOT as a service:
the hub serves its own endpoint and no other estate is in the runtime path.

Two rules carry the whole thing, and both are tested here:

  · **wake-only.** The event carries no state, so a LOST event costs latency
    and never work. Cheaper here than in the design it borrows from, because
    this reconciler is level-triggered — every pass is already a full resync.
  · **the bell is not load-bearing.** A dead stream returns silence and so does
    a quiet one; heartbeats are what let a client tell them apart, and the 30s
    timer is what makes a doorbell failure cost latency rather than work.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub import api_v1
from mcp_hub.server import create_server

OPERATOR_TOKEN = "test-operator-token"
H = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", OPERATOR_TOKEN)
    api_v1._watchers.clear()          # module-level: never leak between tests
    server = create_server(db_path=tmp_path / "hub.db")
    with TestClient(server.streamable_http_app()) as c:
        yield c
    api_v1._watchers.clear()


def _machine(client, name="box-1") -> dict:
    r = client.post("/api/v1/machines", json={"name": name}, headers=H)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _seat(client, identity, machine="box-1") -> None:
    r = client.post("/api/v1/seats", json={
        "identity": identity, "machine": machine, "folder": f"/srv/{identity}",
    }, headers=H)
    assert r.status_code == 201, r.text


def _place(client, seat, machine="box-1") -> str:
    r = client.post("/api/v1/placements", json={
        "seat": seat, "machine": machine, "substrate": "worktree",
    }, headers=H)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ----------------------------------------------------- the notify primitive


def test_a_ring_reaches_only_the_named_machine():
    """Per-machine, not broadcast: an edge must never be woken by another
    box's placement, or the fleet stampedes the hub on every write."""
    mine: asyncio.Queue = asyncio.Queue(maxsize=1)
    theirs: asyncio.Queue = asyncio.Queue(maxsize=1)
    api_v1._watchers["box-1"] = {mine}
    api_v1._watchers["box-2"] = {theirs}
    try:
        assert api_v1.notify_machine("box-1") == 1
        assert mine.qsize() == 1
        assert theirs.qsize() == 0
    finally:
        api_v1._watchers.clear()


def test_ringing_NEVER_raises_even_when_a_watcher_is_broken():
    """🔴 A doorbell that can break a write is worse than no doorbell: the
    write is the thing that matters, and the timer delivers it regardless."""
    class Exploding:
        def put_nowait(self, _):
            raise RuntimeError("boom")

    api_v1._watchers["box-1"] = {Exploding()}
    try:
        assert api_v1.notify_machine("box-1") == 0   # counted honestly
    finally:
        api_v1._watchers.clear()


def test_a_full_queue_is_not_an_error_because_one_bell_is_enough():
    """maxsize=1 on purpose. The message is wake-only and identical every
    time, so a backlog would be N copies of "go look" — one pending bell is
    all the information there is."""
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    api_v1._watchers["box-1"] = {q}
    try:
        assert api_v1.notify_machine("box-1") == 1
        assert api_v1.notify_machine("box-1") == 0   # full, silently fine
        assert q.qsize() == 1
    finally:
        api_v1._watchers.clear()


def test_ringing_a_machine_nobody_watches_is_a_no_op():
    assert api_v1.notify_machine("nobody") == 0
    assert api_v1.notify_machine("") == 0


# --------------------------------------------------- which writes ring it


def _rings(client, fn) -> int:
    """Count bells for box-1 caused by fn(), with a watcher registered."""
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    api_v1._watchers.setdefault("box-1", set()).add(q)
    try:
        fn()
        return q.qsize()
    finally:
        api_v1._watchers.get("box-1", set()).discard(q)


def test_creating_a_placement_rings(client):
    _machine(client)
    _seat(client, "a-box-1")
    assert _rings(client, lambda: _place(client, "a-box-1")) == 1


def test_changing_desired_state_rings(client):
    _machine(client)
    _seat(client, "a-box-1")
    pid = _place(client, "a-box-1")
    assert _rings(client, lambda: client.patch(
        f"/api/v1/placements/{pid}", json={"desired": "stopped"}, headers=H)) == 1


def test_reclaim_rings(client):
    _machine(client)
    _seat(client, "a-box-1")
    pid = _place(client, "a-box-1")
    assert _rings(client, lambda: client.delete(
        f"/api/v1/placements/{pid}", headers=H)) == 1


def test_unplace_rings(client):
    """Rung even though the machine has no work to do — the row it was
    reconciling is gone, and a spurious pull is idempotent and cheap."""
    _machine(client)
    _seat(client, "a-box-1")
    pid = _place(client, "a-box-1")
    assert _rings(client, lambda: client.delete(
        f"/api/v1/placements/{pid}?purge=true", headers=H)) == 1


def test_an_OBSERVED_REPORT_MUST_NOT_RING(client):
    """🔴 THE FEEDBACK LOOP. `/observed` is the machine reporting to the hub.
    Ringing on a report has no brake: edge reports -> hub rings -> edge pulls
    and reconciles -> reports again -> rings again, as fast as the network
    allows. The doorbell is for changes the OPERATOR makes; a machine never
    needs telling about its own news.

    Mutation: add notify_machine() to the observed handler -> this fails.
    """
    token = _machine(client)["token"]
    _seat(client, "a-box-1")
    pid = _place(client, "a-box-1")
    assert _rings(client, lambda: client.post(
        f"/api/v1/placements/{pid}/observed",
        json={"state": "running", "enumeration": {}},
        headers={"Authorization": f"Bearer {token}"})) == 0


def test_a_write_for_ANOTHER_machine_does_not_ring_this_one(client):
    _machine(client)
    _machine(client, "box-2")
    _seat(client, "b-box-2", machine="box-2")
    assert _rings(client, lambda: _place(client, "b-box-2", machine="box-2")) == 0


# ------------------------------------------------------------- the endpoint


def test_a_machine_token_may_not_watch_ANOTHER_machine(client):
    """Same rule the pull endpoint enforces — an edge sees its own work only."""
    _machine(client)
    token = _machine(client, "box-other")["token"]
    r = client.get("/api/v1/machines/box-1/watch",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_watching_requires_a_token(client):
    _machine(client)
    assert client.get("/api/v1/machines/box-1/watch").status_code == 401


# ------------------------------------------------------------- the stream
#
# ⚠️ These drive `watch_stream()` DIRECTLY rather than over HTTP, and that is
# not a shortcut. An `asyncio.Queue` rung from another thread does not reliably
# wake the coroutine waiting on it, so a TestClient version HANGS instead of
# failing — and driving the ASGI app with httpx skips the app's lifespan.
# Production rings from a request handler in the SAME event loop, which is
# precisely the arrangement here.


async def _take(gen, n, timeout=5.0):
    out = []
    async with asyncio.timeout(timeout):
        async for chunk in gen:
            out.append(chunk.decode())
            if len(out) >= n:
                break
    return out


async def test_the_stream_opens_and_then_HEARTBEATS_while_nothing_happens():
    """Heartbeats are what make silence interpretable: without them a dead
    stream and a quiet one are the same bytes, and a client cannot tell
    whether to reconnect."""
    api_v1._watchers.clear()
    gen = api_v1.watch_stream("box-1", heartbeat=0.01)
    try:
        out = await _take(gen, 3)
    finally:
        await gen.aclose()
    assert out[0] == ": connected\n\n"
    assert out[1:] == [": heartbeat\n\n", ": heartbeat\n\n"]


async def test_a_ring_produces_a_WAKE_ONLY_event():
    """🔴 The property the whole design rests on. If placement state appeared
    in the payload a client could act on the MESSAGE instead of pulling — and
    then a LOST message would cost WORK, not merely latency."""
    api_v1._watchers.clear()
    gen = api_v1.watch_stream("box-1", heartbeat=5.0)
    try:
        first = await _take(gen, 1)
        assert first == [": connected\n\n"]

        async def ring():
            await asyncio.sleep(0.01)
            assert api_v1.notify_machine("box-1") == 1

        task = asyncio.create_task(ring())
        got = await _take(gen, 1)
        await task
    finally:
        await gen.aclose()

    assert got[0].startswith("event: wake\n")
    payload = json.loads(got[0].split("data: ", 1)[1].strip())
    assert set(payload) == {"machine", "reason"}, payload
    assert payload["machine"] == "box-1"


async def test_the_watcher_registers_and_is_ALWAYS_deregistered():
    """A leaked queue is a slow memory leak AND a watcher count that lies
    about how many edges are listening. The finally must run on close, not
    only on a tidy end-of-iteration."""
    api_v1._watchers.clear()
    gen = api_v1.watch_stream("box-1", heartbeat=0.01)
    await _take(gen, 1)
    assert len(api_v1._watchers.get("box-1", ())) == 1
    await gen.aclose()
    assert not api_v1._watchers.get("box-1"), "watcher leaked on close"


async def test_two_edges_on_one_machine_are_BOTH_rung():
    """A machine can legitimately have two watchers mid-restart (old listener
    not yet reaped, new one connected). Waking only one would leave whichever
    is real waiting for the timer."""
    api_v1._watchers.clear()
    a = api_v1.watch_stream("box-1", heartbeat=5.0)
    b = api_v1.watch_stream("box-1", heartbeat=5.0)
    try:
        await _take(a, 1)
        await _take(b, 1)
        assert api_v1.notify_machine("box-1") == 2
    finally:
        await a.aclose()
        await b.aclose()
