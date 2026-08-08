"""The seven team-assembly scenarios, as executable claims.

Operator, 2026-08-08, having listed what they need as an operator building the
factory: *"for each item, can you give me the CLI arguments needed to
instigate, manage and tear down"* — the goal being to find gaps at the basic
level. Six were found. This file is the gate for closing them.

⭐ WHY THESE TESTS AND NOT OTHERS. Every gap was a thing that EXISTED and did
not RUN, or ran and was unreachable:

  - the squads REST routes were complete and had no CLI door
  - `capsule place` wrote placements happily and would give one identity two
    containers
  - a brief could be written to a file nobody would ever open
  - `SEAT_MODE=headless` parsed, validated, and was refused at the entrypoint

So each test asserts REACHABILITY or CONSEQUENCE, never mere presence. A test
that checks a flag exists would have passed against every one of these bugs.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub import cli
from mcp_hub.server import create_server, purge_expired_memberships

OPERATOR_TOKEN = "test-operator-token"
H = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", OPERATOR_TOKEN)
    server = create_server(db_path=tmp_path / "hub.db")
    with TestClient(server.streamable_http_app()) as c:
        yield c


# --------------------------------------------------------------- gap 5: loans


class TestLoansExpire:
    """Scenario 4 — borrow a specialist. The deadline must END DELIVERY, not
    merely be recorded next to a membership that keeps working."""

    def _db(self, tmp_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE squad_members (agent TEXT, squad TEXT, muted INT "
            "DEFAULT 0, joined REAL DEFAULT 0, expires REAL NOT NULL DEFAULT 0,"
            " PRIMARY KEY (agent, squad))"
        )
        return conn

    def test_a_lapsed_loan_is_removed_and_a_permanent_member_is_not(
            self, tmp_path):
        conn = self._db(tmp_path)
        now = 1000.0
        conn.executemany(
            "INSERT INTO squad_members (agent, squad, expires) VALUES (?,?,?)",
            [("alice", "spike", now - 1), ("bob", "spike", 0),
             ("carol", "spike", now + 500)],
        )
        conn.commit()
        assert purge_expired_memberships(conn, now) == 1
        left = {r["agent"] for r in
                conn.execute("SELECT agent FROM squad_members")}
        # bob is the positive control: without him, an implementation that
        # deleted EVERYTHING would pass the first assertion.
        assert left == {"bob", "carol"}

    def test_it_ISSUES_NO_WRITE_when_nothing_has_expired(self, tmp_path):
        """🔴 REGRESSION. The first version issued the DELETE unconditionally,
        so every read path — list_squads, catch-up, broadcast fan-out — took a
        write lock. It surfaced as `database is locked` across the API suite.

        ⚠️ THE OBVIOUS TEST FOR THIS IS VACUOUS, and the first draft here was.
        Holding a write transaction on a second connection and asserting the
        purge returns 0 passes EITHER WAY: the guarded version returns 0
        because it never tries, and the unguarded one returns 0 because its
        OperationalError is caught and swallowed. Two opposite implementations,
        one green result — proven by mutation, not by reading.

        So the assertion is on the SQL actually issued, which is the property
        being claimed: a read path emits no write.
        """
        class Recording:
            def __init__(self, conn):
                self._c, self.sql = conn, []

            def execute(self, sql, *a):
                self.sql.append(sql)
                return self._c.execute(sql, *a)

            def commit(self):
                self._c.commit()

        conn = self._db(tmp_path)
        conn.execute("INSERT INTO squad_members (agent, squad, expires) "
                     "VALUES ('bob', 'spike', 0)")
        conn.commit()

        rec = Recording(conn)
        assert purge_expired_memberships(rec, 1000.0) == 0
        assert not any("DELETE" in s for s in rec.sql), (
            f"took a write lock on a pure read path: {rec.sql}")

        # The positive control: when something HAS expired, the write happens.
        # Without it, a function that never deleted anything would pass above.
        conn.execute("INSERT INTO squad_members (agent, squad, expires) "
                     "VALUES ('alice', 'spike', 500)")
        conn.commit()
        rec2 = Recording(conn)
        assert purge_expired_memberships(rec2, 1000.0) == 1
        assert any("DELETE" in s for s in rec2.sql)

    def test_a_lapsed_loan_stops_reaching_the_agent_on_the_DELIVERY_path(
            self, client):
        """The claim that actually matters, asserted through the API rather
        than against the helper: an expired member disappears from the squad
        the hub will fan a broadcast out to."""
        client.post("/api/v1/squads", json={"name": "spike"}, headers=H)
        r = client.put("/api/v1/squads/spike/members/alice",
                       json={"expires": time.time() + 0.6}, headers=H)
        assert r.status_code == 200, r.text
        seats = [m["seat"] for m in
                 client.get("/api/v1/squads/spike/members",
                            headers=H).json()["members"]]
        assert seats == ["alice"], "the loan never took effect at all"

        time.sleep(0.8)
        after = client.get("/api/v1/squads/spike/members",
                           headers=H).json()["members"]
        assert after == [], (
            "the deadline passed and the membership survived — the loan was "
            "recorded, not enforced")

    def test_a_deadline_already_in_the_past_is_REFUSED(self, client):
        """Accepting it would report success and then vanish on the next read,
        which reads as 'the add failed' rather than 'the loan was over'."""
        client.post("/api/v1/squads", json={"name": "spike"}, headers=H)
        r = client.put("/api/v1/squads/spike/members/alice",
                       json={"expires": time.time() - 10}, headers=H)
        assert r.status_code == 422, r.text


class TestParseUntil:
    """`--until` is the operator-facing half. A misread duration that returned
    0 would silently make a loan PERMANENT — the exact thing it prevents."""

    def test_relative_forms(self):
        now = 1_000_000.0
        assert cli.parse_until("+90m", now) == now + 5400
        assert cli.parse_until("+12h", now) == now + 43200
        assert cli.parse_until("+7d", now) == now + 604800
        assert cli.parse_until("+2w", now) == now + 1209600

    def test_empty_means_permanent(self):
        assert cli.parse_until("", 1000.0) == 0.0

    @pytest.mark.parametrize("bad", ["7d", "+d", "+xd", "+7y", "next tuesday",
                                     "+0d", "+-3d", "2026/09/01"])
    def test_a_malformed_deadline_RAISES_rather_than_defaulting(self, bad):
        """The load-bearing one. Returning 0 for anything unparseable turns a
        typo into a membership that never ends, and nothing would ever say so.
        """
        with pytest.raises(ValueError):
            cli.parse_until(bad, 1000.0)

    def test_an_absolute_date_INCLUDES_that_day(self):
        """`--until 2026-09-01` must not expire at 00:00 on the 1st — the
        other reading shortens every loan by a day and the operator finds out
        when someone stops hearing a squad."""
        import datetime as dt
        got = cli.parse_until("2026-09-01", 0.0)
        midnight = dt.datetime(2026, 9, 1).timestamp()
        assert got == midnight + 86400


# ---------------------------------------------------- gap 3: identity minting


class TestCapsulePlacementIdentity:
    """Scenarios 5 and 7 — two teams on one problem, and cloning a shape that
    worked. Both were silently broken in the same way."""

    def _squad_with_seat(self, client, spec=None):
        client.post("/api/v1/machines",
                    json={"name": "box-1", "os": "linux",
                          "capabilities": {"docker": True}}, headers=H)
        client.post("/api/v1/machines",
                    json={"name": "box-2", "os": "linux",
                          "capabilities": {"docker": True}}, headers=H)
        body = {"repo": "acme/widget", "machine": "box-1",
                "folder": "/w", "identity": "widget-1",
                "spec": spec or {"image": "seat:latest"}}
        assert client.post("/api/v1/seats", json=body,
                           headers=H).status_code == 201
        client.post("/api/v1/squads", json={"name": "team"}, headers=H)
        client.put("/api/v1/squads/team/members/widget-1", json={}, headers=H)
        cid = client.post("/api/v1/capsules", json={"squad": "team"},
                          headers=H).json()["id"]
        return cid

    def test_placing_the_same_capsule_twice_is_REFUSED(self, client):
        """🔴 THE BUG. Two containers, one hub identity, both registering —
        whichever registered last silently owned the wake binding, and nothing
        anywhere reported a problem."""
        cid = self._squad_with_seat(client)
        first = client.post(f"/api/v1/capsules/{cid}/place",
                            json={"machine": "box-1"}, headers=H)
        assert first.status_code == 201, first.text

        second = client.post(f"/api/v1/capsules/{cid}/place",
                             json={"machine": "box-2"}, headers=H)
        assert second.status_code == 409, (
            "placed the same identity on a second machine — that is two "
            "containers claiming one agent name")
        assert "widget-1" in second.json()["detail"]
        # The refusal must NAME the way forward, or it just blocks a real need.
        assert "as" in second.json()["detail"]

    def test_as_label_mints_a_genuinely_separate_squad(self, client):
        cid = self._squad_with_seat(client)
        client.post(f"/api/v1/capsules/{cid}/place",
                    json={"machine": "box-1"}, headers=H)
        r = client.post(f"/api/v1/capsules/{cid}/place",
                        json={"machine": "box-2", "as": "takeB"}, headers=H)
        assert r.status_code == 201, r.text
        assert r.json()["seats"] == ["widget-1-takeb"]

        identities = {s["identity"] for s in
                      client.get("/api/v1/seats", headers=H).json()["seats"]}
        assert {"widget-1", "widget-1-takeb"} <= identities
        # A real second seat, not a placement pointing at the first.
        clone = next(s for s in
                     client.get("/api/v1/seats", headers=H).json()["seats"]
                     if s["identity"] == "widget-1-takeb")
        assert clone["cloned_from"] == "widget-1"

    def test_POD_INHABITANTS_are_re_identified_too(self, client):
        """⭐ The half that is easy to miss. A pod seat's spec carries its
        agents' OWN hub names. Suffixing only the container would put up two
        containers with different names holding agents with IDENTICAL names —
        the same collision, moved from `docker ps` to somewhere invisible.
        """
        cid = self._squad_with_seat(client, spec={
            "image": "seat:latest", "squad": "team",
            "agents": [{"identity": "alice"}, {"identity": "bob"}],
        })
        client.post(f"/api/v1/capsules/{cid}/place",
                    json={"machine": "box-1"}, headers=H)
        r = client.post(f"/api/v1/capsules/{cid}/place",
                        json={"machine": "box-2", "as": "takeB"}, headers=H)
        assert r.status_code == 201, r.text
        clone = next(s for s in
                     client.get("/api/v1/seats", headers=H).json()["seats"]
                     if s["identity"] == "widget-1-takeb")
        inhabitants = [a["identity"] for a in clone["spec"]["agents"]]
        assert inhabitants == ["alice-takeb", "bob-takeb"], (
            "the pod's agents kept their original names — two containers, "
            "same agent identities, and only the hub would ever notice")
        assert clone["spec"]["squad"] == "team-takeb"

    def test_reusing_a_label_is_refused_rather_than_colliding(self, client):
        cid = self._squad_with_seat(client)
        client.post(f"/api/v1/capsules/{cid}/place",
                    json={"machine": "box-1", "as": "x"}, headers=H)
        again = client.post(f"/api/v1/capsules/{cid}/place",
                            json={"machine": "box-2", "as": "x"}, headers=H)
        assert again.status_code == 409

    @pytest.mark.parametrize("label", ["a.b", "a:b", "..", "  "])
    def test_a_label_that_would_produce_an_unaddressable_agent_is_rejected(
            self, label):
        """tmux reads `.` and `:` as pane and window separators, so a dotted
        identity produces an agent that RUNS and cannot be addressed —
        measured on the live fleet."""
        from mcp_hub.api_v1 import _sanitize_label
        out = _sanitize_label(label)
        assert "." not in out and ":" not in out


# ------------------------------------------------------- gap 1: brief + input


class TestBriefReachesTheAgent:
    """Scenario 1 — the spike team. A brief written to disk that nothing
    points at is a file nobody opens."""

    def _contract(self, **kw):
        from mcp_hub.seat import SeatContract
        base = dict(identity="spike-1", project="acme/widget",
                    hub_url="http://h/mcp", mode="interactive", prompt="",
                    squads="", repo="")
        base.update(kw)
        return SeatContract(**base)

    def test_the_first_turn_TELLS_the_agent_to_read_the_brief(self):
        """🔴 The one that matters. A seat's first turn is generated, not
        typed — so a brief nothing mentions is invisible and the agent stands
        by exactly as if it had no job."""
        from mcp_hub.seat import BRIEF_FILE, first_turn_prompt
        out = first_turn_prompt(self._contract(brief="Spike the thing."))
        assert BRIEF_FILE in out
        assert "stand by" not in out, (
            "still told to stand by while holding a brief — it will do nothing")

    def test_a_seat_with_no_brief_is_unchanged(self):
        """The compatibility control: every seat placed before briefs existed
        must get byte-identical instructions."""
        from mcp_hub.seat import first_turn_prompt
        out = first_turn_prompt(self._contract())
        assert out.endswith("stand by for instructions.")
        assert "BRIEF" not in out

    def test_the_prompt_names_the_inputs_when_there_are_some(self):
        from mcp_hub.seat import first_turn_prompt
        out = first_turn_prompt(self._contract(
            brief="Go", inputs=(("spec.md", "x"), ("data.csv", "y"))))
        assert "inputs/" in out and "2 file(s)" in out

    def test_brief_files_renders_brief_and_inputs(self):
        from mcp_hub.seat import BRIEF_FILE, brief_files
        out = brief_files(self._contract(
            brief="Do the thing", inputs=(("spec.md", "CONTENT"),)))
        assert out[BRIEF_FILE] == "Do the thing\n"   # trailing newline added
        assert out["inputs/spec.md"] == "CONTENT"

    def test_no_brief_renders_nothing(self):
        from mcp_hub.seat import brief_files
        assert brief_files(self._contract()) == {}


class TestPodsCanBeBriefed:
    """The case the code explicitly refused. A PROMPT is single-valued and
    stays refused for a pod; a BRIEF is a file every inhabitant reads."""

    def _env(self, **kw):
        env = {"MCP_HUB_URL": "http://h/mcp",
               "SEAT_MANIFEST": json.dumps(
                   {"squad": "spike",
                    "agents": [{"identity": "alice"}, {"identity": "bob"}]})}
        env.update(kw)
        return env

    def test_a_pod_wide_brief_reaches_every_inhabitant(self):
        from mcp_hub.seat import agent_contract, parse_pod_manifest
        pod = parse_pod_manifest(self._env(SEAT_BRIEF="Spike the idea."))
        briefs = {a.identity: agent_contract(pod, a).brief for a in pod.agents}
        assert briefs == {"alice": "Spike the idea.",
                          "bob": "Spike the idea."}

    def test_a_per_agent_brief_REPLACES_the_pod_brief_for_that_agent(self):
        from mcp_hub.seat import agent_contract, parse_pod_manifest
        env = self._env(
            SEAT_BRIEF="Team brief.",
            SEAT_MANIFEST=json.dumps({"squad": "spike", "agents": [
                {"identity": "alice", "brief": "You do the maths."},
                {"identity": "bob"}]}))
        pod = parse_pod_manifest(env)
        briefs = {a.identity: agent_contract(pod, a).brief for a in pod.agents}
        assert briefs["alice"] == "You do the maths."
        assert briefs["bob"] == "Team brief.", (
            "an unqualified member lost the team brief — singling one member "
            "out silently stripped everyone else's instructions")

    def test_headless_is_still_refused_for_a_pod(self):
        """Unchanged, and the refusal must now DISTINGUISH the two so the
        reader is not left thinking pods cannot be briefed at all."""
        from mcp_hub.seat import SeatContractError, parse_pod_manifest
        with pytest.raises(SeatContractError) as e:
            parse_pod_manifest(self._env(SEAT_MODE="headless",
                                         SEAT_PROMPT="x"))
        assert "BRIEF" in str(e.value)


class TestInputsCannotEscape:
    """SEAT_INPUTS is written by the edge from a spec held in the hub's
    database. A filename is therefore untrusted input."""

    @pytest.mark.parametrize("name", [
        "../escape", "a/b", "/abs", ".claude", "..", "x\\y"])
    def test_a_filename_that_leaves_the_inputs_directory_is_REFUSED(self, name):
        """Anything able to write a spec could otherwise drop a file into
        ~/.claude/settings.json — arbitrary hook execution at next launch."""
        from mcp_hub.seat import SeatContractError, _parse_inputs
        with pytest.raises(SeatContractError):
            _parse_inputs(json.dumps({name: "payload"}))

    def test_ordinary_filenames_are_accepted(self):
        """The positive control — without it, a function that refused
        everything would pass every case above."""
        from mcp_hub.seat import _parse_inputs
        assert _parse_inputs(json.dumps({"spec.md": "x"})) == (("spec.md", "x"),)


class TestHeadlessRuns:
    """Scenario 3 — the solo errand. It parsed, validated, and was refused at
    the entrypoint as 'reserved but not yet shipped'."""

    def test_a_brief_can_stand_in_for_a_prompt(self):
        from mcp_hub.seat import parse_seat_contract
        c = parse_seat_contract({
            "SEAT_IDENTITY": "errand-1", "MCP_HUB_URL": "http://h/mcp",
            "SEAT_MODE": "headless", "SEAT_BRIEF": "Answer this."})
        assert c.mode == "headless"

    def test_headless_with_NEITHER_prompt_nor_brief_is_still_refused(self):
        """A one-shot claude with no instruction exits immediately, which the
        edge reads as a crash."""
        from mcp_hub.seat import SeatContractError, parse_seat_contract
        with pytest.raises(SeatContractError):
            parse_seat_contract({
                "SEAT_IDENTITY": "errand-1", "MCP_HUB_URL": "http://h/mcp",
                "SEAT_MODE": "headless"})

    def test_the_launch_points_at_the_brief_file(self):
        from mcp_hub.seat import BRIEF_FILE, SeatContract, launch_argv
        c = SeatContract(identity="e", project="p", hub_url="h",
                         mode="headless", prompt="", squads="", repo="",
                         brief="Do it")
        argv = launch_argv(c, "/w")
        assert argv[:2] == ["claude", "-p"]
        assert BRIEF_FILE in argv[2]

    def test_an_explicit_prompt_still_wins(self):
        from mcp_hub.seat import SeatContract, launch_argv
        c = SeatContract(identity="e", project="p", hub_url="h",
                         mode="headless", prompt="EXPLICIT", squads="",
                         repo="", brief="Do it")
        assert launch_argv(c, "/w")[2] == "EXPLICIT"


def test_the_edge_actually_INJECTS_the_brief_into_the_container():
    """🔴 The join nobody would notice missing. Every test above could pass
    with the edge never passing SEAT_BRIEF, and the brief would exist in the
    hub, in the spec, in the tests — and never in a container."""
    from mcp_hub.edge import DockerExecutor
    argv = DockerExecutor.create_argv("spike-1", {
        "image": "seat:latest", "brief": "Spike the thing.",
        "inputs": {"spec.md": "CONTENT"},
    })
    joined = " ".join(argv)
    assert "SEAT_BRIEF=Spike the thing." in joined
    assert "SEAT_INPUTS=" in joined
    payload = next(a for a in argv if a.startswith("SEAT_INPUTS="))
    assert json.loads(payload.split("=", 1)[1]) == {"spec.md": "CONTENT"}


def test_a_seat_with_no_brief_gets_no_brief_env():
    """The control: an ordinary seat's argv must be untouched, or every
    existing placement changes shape."""
    from mcp_hub.edge import DockerExecutor
    argv = DockerExecutor.create_argv("plain-1", {"image": "seat:latest"})
    assert not any("SEAT_BRIEF" in a or "SEAT_INPUTS" in a for a in argv)
