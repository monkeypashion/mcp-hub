"""/api/v1 — the hub's management surface (docs/hub-api-v1.md).

REST alongside MCP, one store: the MCP tools remain the conversation surface
for agents; this module is CRUD for operators, CLIs, the future UI and edge
daemons. It shares the hub's SQLite DB (notably `squad_members`, which stays
the single membership truth the MCP tools read) and adds its own tables for
the resources MCP never modelled: machines, seats, workspaces, capsules,
placements.

The edge boundary is a CONTRACT, not code: placements are desired-state
records served to per-machine edge daemons. Nothing here executes anything on
a machine; `status: pending-edge` is the honest name for that.

Auth: bearer tokens. The operator token comes from $MCP_HUB_API_TOKEN — unset
means the whole surface answers 503 (off, loudly — never open by accident).
Machine tokens are issued once at enrolment and stored hashed; a machine
principal may only pull its own placements and report observed state.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import secrets
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

# Safe at module level: server.py imports THIS module lazily, inside
# create_server, precisely so the http surface stays optional for stdio runs.
# The one-way edge means the loan deadline is enforced by the same function on
# both surfaces — a second implementation here is a second chance to disagree.
from mcp_hub.server import purge_expired_memberships
from mcp_hub.spec_guard import validate_spec

# The seat control plane's closed verb set, at module scope so the edge's own
# copy can be pinned against it by test. The two guards stay INDEPENDENT — the
# hub refuses to write an unknown verb, the edge refuses to execute one — but
# the VOCABULARY is one fact, and letting the two drift apart is what made
# `hold` inert on arrival: the hub wrote it, the edge did not recognise it, so
# it never settled and the mirror it fed could never populate.
SEAT_PHASE1_VERBS = ("interrupt", "prompt", "hold", "release")

SUBSTRATES = ("worktree", "docker")

# ── The doorbell ─────────────────────────────────────────────────────────────
#
# The edge PULLS on a timer, which is what makes it NAT-safe and outage-proof —
# but the interval is then the latency a UI feels, and a placement written from
# a web front end does nothing until the next tick (measured: a wake took 95s,
# 2026-08-09). This lets a machine be told "now" instead of waiting.
#
# WAKE-ONLY, deliberately. The event carries no state: the edge's next act is
# the same full pull it always does. That is not laziness, it is what makes a
# LOST event cost latency and never work — and it is cheaper here than in the
# design this borrows from (`dreamteam` 0d17942), because this reconciler is
# LEVEL-triggered: `pull_placements` returns every row for the machine and the
# planner diffs it against a fresh enumeration, so every pass is already a full
# resync. No cursor, no Last-Event-ID, nothing to miss.
#
# ⚠️ THE TIMER STAYS UNDERNEATH. A dead stream returns silence, and so does a
# quiet one; if those are the same bytes the doorbell cannot be load-bearing —
# not because streams are unreliable but because you cannot TELL. Heartbeats
# below make silence interpretable to the client, and the 30s timer makes a
# doorbell failure cost latency rather than work.
#
# ⚠️ IN-PROCESS ONLY. If the hub is ever run multi-worker, a write served by
# one worker will not ring watchers held by another, and the miss is silent.
# The floor covers it (latency, never work) — but a doorbell that is ever made
# load-bearing must move to a shared bus first.
WATCH_HEARTBEAT_S = 20.0

# machine name -> the queues of everyone currently watching it.
_watchers: dict[str, set[asyncio.Queue]] = {}


async def watch_stream(machine: str, heartbeat: float | None = None):
    """The doorbell's body: register, emit, and always deregister.

    Module-level and self-contained ON PURPOSE. As a closure inside the route
    it could only be exercised through a live HTTP stream, which needs the
    app's lifespan running and a second thread to ring from — and an
    `asyncio.Queue` rung from another thread does not reliably wake its waiter,
    so the test hangs rather than fails. Here it is driven directly, in one
    event loop, which is also exactly how production works.
    """
    hb = WATCH_HEARTBEAT_S if heartbeat is None else heartbeat
    # maxsize=1: the message is wake-only and identical every time, so a
    # backlog would be N copies of "go look". One pending bell is all the
    # information there is.
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    _watchers.setdefault(machine, set()).add(q)
    try:
        yield b": connected\n\n"
        while True:
            try:
                reason = await asyncio.wait_for(q.get(), timeout=hb)
            except (asyncio.TimeoutError, TimeoutError):
                # A COMMENT line, not an event — it must never be mistaken for
                # a doorbell, only for proof of life. Without it a dead stream
                # and a quiet one are the same bytes, and a client cannot tell
                # whether to reconnect.
                yield b": heartbeat\n\n"
                continue
            body = json.dumps({"machine": machine, "reason": reason})
            yield f"event: wake\ndata: {body}\n\n".encode()
    finally:
        # Deregister on ANY exit — hang-up, cancellation, generator close.
        # A leaked queue is a slow memory leak AND a watcher count that lies
        # about how many edges are listening.
        live = _watchers.get(machine)
        if live is not None:
            live.discard(q)
            if not live:
                _watchers.pop(machine, None)


def notify_machine(machine: str, reason: str = "placement") -> int:
    """Ring the doorbell for one machine. Returns how many watchers were told.

    Best-effort and NEVER raises: a doorbell that can break a write is worse
    than no doorbell, since the write is the thing that actually matters and
    the timer will deliver it regardless.
    """
    if not machine:
        return 0
    rung = 0
    for q in list(_watchers.get(machine, ())):
        try:
            q.put_nowait(reason)
            rung += 1
        except Exception:  # noqa: BLE001 — a full/closed queue must not fail a write
            pass
    return rung

# Default settings block for rendered .code-workspace files — mirrors what
# `squad ws-new` writes, so an API workspace opens identically to a manual one.
WS_SETTINGS = {
    "terminal.integrated.tabs.title": "${sequence}",
    "terminal.integrated.tabs.description": "${progress}",
    "terminal.integrated.enablePersistentSessions": False,
    "terminal.integrated.hideOnStartup": "whenEmpty",
}


def _now() -> float:
    return time.time()


def _sha(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def _err(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _sanitize_label(label: str) -> str:
    """A suffix that is safe as a hub identity, a container name and a tmux
    session all at once.

    `.` and `:` are excluded deliberately, not incidentally: tmux reads them as
    its pane and window separators, so a dotted name produces an agent that
    RUNS and cannot be addressed — measured on the fleet. The same rule is
    enforced at the far end by `seat._tmux_safe`; this stops a bad label
    reaching a container in the first place.
    """
    kept = "".join(
        c if (c.isalnum() or c in "-_") else "-" for c in label.strip().lower()
    ).strip("-")
    return kept[:32]


def init_api_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS api_machines (
            name         TEXT PRIMARY KEY,
            os           TEXT NOT NULL DEFAULT '',
            capabilities TEXT NOT NULL DEFAULT '{}',
            token_hash   TEXT NOT NULL,
            last_seen    REAL,
            archived     INTEGER NOT NULL DEFAULT 0,
            created      REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_seats (
            identity    TEXT PRIMARY KEY,
            repo        TEXT NOT NULL,
            machine     TEXT NOT NULL,
            folder      TEXT NOT NULL,
            launch_args TEXT NOT NULL DEFAULT '',
            class       TEXT NOT NULL DEFAULT 'squad',
            cloned_from TEXT NOT NULL DEFAULT '',
            archived    INTEGER NOT NULL DEFAULT 0,
            created     REAL NOT NULL,
            -- Substrate-specific, as JSON: image/env/ports/volumes/command for
            -- docker, nothing for worktree (which uses repo+folder). A column
            -- per substrate would make every new substrate a migration; this
            -- makes it a key. The unit being managed is a CONTAINER; an agent
            -- seat is one that additionally has memory and a harvest step.
            spec        TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS api_squads (
            name             TEXT PRIMARY KEY,
            description      TEXT NOT NULL DEFAULT '',
            board_visibility TEXT NOT NULL DEFAULT 'shown',
            archived         INTEGER NOT NULL DEFAULT 0,
            created          REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_workspaces (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            machine  TEXT NOT NULL DEFAULT '',
            squad    TEXT NOT NULL DEFAULT '',
            listings TEXT NOT NULL DEFAULT '[]',
            created  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_capsules (
            id       TEXT PRIMARY KEY,
            squad    TEXT NOT NULL,
            manifest TEXT NOT NULL,
            tarball  BLOB NOT NULL,
            created  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_discovered_workspaces (
            machine        TEXT NOT NULL,
            path           TEXT NOT NULL,
            folders        INTEGER,
            error          TEXT NOT NULL DEFAULT '',
            reported_at    REAL NOT NULL,
            last_open_ping REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (machine, path)
        );
        -- What each machine's squad roster says lives where. A REMOTE agent
        -- row carries no worktree — the fleet snapshot is parsed from rendered
        -- presence text — so the board matched it to a workspace by repo
        -- BASENAME, and a box with several clones of one repo had every clone
        -- claimed by every workspace listing any of them. One row in every
        -- board was false. Only the machine itself can say which folder an
        -- agent sits in, so the machine reports it and the board reads it.
        CREATE TABLE IF NOT EXISTS api_machine_agents (
            machine     TEXT NOT NULL,
            agent       TEXT NOT NULL,
            worktree    TEXT NOT NULL DEFAULT '',
            -- Whether its launch args carry the hub channels flag, and whether
            -- it has a live pane. Together they separate "should be on the hub
            -- and isn't" from "was never going to be" — without them, every
            -- enrolled scratch folder on a box became a warning.
            comms       INTEGER NOT NULL DEFAULT 0,
            -- TRI-STATE: NULL is "liveness unreadable", NOT "down".
            running     INTEGER,
            reported_at REAL NOT NULL,
            PRIMARY KEY (machine, agent)
        );
        CREATE TABLE IF NOT EXISTS api_placements (
            id             TEXT PRIMARY KEY,
            seat           TEXT NOT NULL,
            machine        TEXT NOT NULL,
            substrate      TEXT NOT NULL,
            desired        TEXT NOT NULL DEFAULT 'running',
            observed_state TEXT,
            observed_at    REAL,
            observed_enum  TEXT NOT NULL DEFAULT '{}',
            reclaim        TEXT NOT NULL DEFAULT '',
            created        REAL NOT NULL
        );
        """
    )
    # Membership provenance: API-set rows say so, workspace-seeded rows will
    # say which file — the fix-by-design for the board's union-attribution
    # defect. ALTER is idempotent-by-exception, matching server.py migrations.
    try:
        conn.execute(
            "ALTER TABLE squad_members ADD COLUMN source TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        pass
    # Roster rows gained two fields after the table shipped, and the live hub
    # already holds the old shape — CREATE TABLE IF NOT EXISTS silently does
    # nothing for it, so they arrive by ALTER or not at all.
    #
    # `running` has NO DEFAULT on purpose: NULL is "the edge could not read
    # tmux", which every pre-existing row honestly is. Defaulting it to 0
    # would mark the whole fleet down on the deploy that adds the column.
    for _sql in (
        "ALTER TABLE api_machine_agents ADD COLUMN comms INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE api_machine_agents ADD COLUMN running INTEGER",
    ):
        try:
            conn.execute(_sql)
        except sqlite3.OperationalError:
            pass
    # Substrate-specific seat fields. The live hub's api_seats predates this,
    # and CREATE TABLE IF NOT EXISTS silently does nothing for it — so the
    # column has to arrive by ALTER or every deployed hub keeps the old shape.
    try:
        conn.execute(
            "ALTER TABLE api_seats ADD COLUMN spec TEXT NOT NULL DEFAULT '{}'"
        )
    except sqlite3.OperationalError:
        pass
    # The edge's self-report (W1.2): when did a pass last run HERE, and what
    # was its own verdict. Nullable on purpose — NULL is "no edge has ever
    # reported", which must stay distinguishable from both ok and failed.
    for _sql in (
        "ALTER TABLE api_machines ADD COLUMN edge_last_run REAL",
        "ALTER TABLE api_machines ADD COLUMN edge_result TEXT",
    ):
        try:
            conn.execute(_sql)
        except sqlite3.OperationalError:
            pass
    # Seat lifecycle events — APPEND-ONLY provenance for every existence
    # transition (W1.1). A bare `archived` flag answers "is it archived now"
    # and destroys when/by-whom/why — FDM's estate marked a live healthy
    # backend `failed` from a buggy poll and the terminal flag made it
    # unhealable by design. And purge must leave a DEATH-FACT that survives
    # the row: "did this ever exist, and what happened to it" is the question
    # people ask during the incident, when the row is already gone.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_seat_events (
            identity TEXT NOT NULL,
            event    TEXT NOT NULL,   -- archived | restored | purged
            ts       REAL NOT NULL,
            actor    TEXT NOT NULL DEFAULT '',
            reason   TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # Seat CONTROL plane (cards #144/#152). The console records INTENT; the
    # machine that owns the seat carries it out and reports what it OBSERVED
    # — the placements pattern, applied to keystrokes. The console never
    # holds a shell: it can only ask, and one compromised console therefore
    # cannot reach a single machine directly.
    #
    # `status` starts `pending` and is terminal thereafter (done | failed |
    # refused | expired). `refused` is a real OUTCOME, not an error: a
    # fail-closed edge must be able to say "I would not do that" and have it
    # recorded, or the honest refusal looks identical to a crash.
    #
    # `observed` and `pane_after` are NULLABLE on purpose — an action the
    # edge has not reached yet has no observation, and an empty string there
    # would read as "the pane was blank", which is a measurement.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_seat_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            seat         TEXT NOT NULL,
            kind         TEXT NOT NULL,   -- interrupt | prompt  (phase 1)
            args         TEXT NOT NULL DEFAULT '{}',
            requested_at REAL NOT NULL,
            requested_by TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'pending',
            observed     TEXT,
            pane_after   TEXT,
            settled_at   REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seat_actions_seat "
        "ON api_seat_actions (seat, status)"
    )
    # Watch leg. A viewer DECLARATION with the open-now 180s window, so a
    # console tab left open in a closed laptop stops the stream by itself.
    # One row per seat: "is anyone looking", not "who is looking".
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_seat_viewers (
            seat       TEXT PRIMARY KEY,
            declared_at REAL NOT NULL,
            declared_by TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # Pane captures — a RING BUFFER, never a transcript. Durable history is
    # the memory volume's job; this exists so the console can show what the
    # seat looked like, and storing it forever would make every pane's
    # contents a permanent record nobody asked for.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_seat_panes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            seat        TEXT NOT NULL,
            pane        TEXT NOT NULL,
            captured_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seat_panes_seat "
        "ON api_seat_panes (seat, id)"
    )
    conn.commit()


def mount_api(mcp: Any, db_path: Path, registry: Any) -> None:
    """Register every /api/v1 route on the hub's streamable-http app."""
    from mcp_hub.server import _get_db  # shared thread-local connection pool

    def db() -> sqlite3.Connection:
        return _get_db(db_path)

    # -- auth ---------------------------------------------------------------

    def auth(request: Request) -> tuple[str, str] | JSONResponse:
        """Return (principal, machine_name) or an error response.

        principal is "operator" (machine_name "") or "machine" (its name).
        """
        op_token = os.environ.get("MCP_HUB_API_TOKEN", "")
        if not op_token:
            return _err(503, "management API disabled: MCP_HUB_API_TOKEN not set")
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return _err(401, "missing bearer token")
        token = header[len("Bearer "):]
        if secrets.compare_digest(token, op_token):
            return ("operator", "")
        row = db().execute(
            "SELECT name FROM api_machines WHERE token_hash = ? AND archived = 0",
            (_sha(token.encode()),),
        ).fetchone()
        if row:
            return ("machine", row["name"])
        return _err(401, "unrecognised token")

    def operator_only(request: Request) -> tuple[str, str] | JSONResponse:
        got = auth(request)
        if isinstance(got, JSONResponse):
            return got
        principal, name = got
        if principal != "operator":
            return _err(403, "operator token required")
        return got

    async def body_of(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001 — absent/invalid body is just {}
            return {}

    def route(path: str, methods: list[str]) -> Callable:
        return mcp.custom_route(path, methods=methods)

    # -- serializers --------------------------------------------------------

    def machine_json(row: sqlite3.Row, token: str | None = None) -> dict:
        out = {
            "name": row["name"],
            "os": row["os"],
            "capabilities": json.loads(row["capabilities"]),
            "last_seen": row["last_seen"],
            # ABSENT-AS-NULL, never defaulted: a machine whose edge has
            # never reported is "no instrument", not "healthy" and not
            # "failed" — the reader renders the distinction (W1.2).
            "edge_last_run": row["edge_last_run"],
            "edge_result": (
                json.loads(row["edge_result"]) if row["edge_result"] else None
            ),
        }
        if token is not None:
            out["token"] = token
        return out

    def seat_json(row: sqlite3.Row, presence: bool = False) -> dict:
        out = {
            "identity": row["identity"],
            "repo": row["repo"],
            "machine": row["machine"],
            "folder": row["folder"],
            "launch_args": row["launch_args"],
            "class": row["class"],
            "cloned_from": row["cloned_from"],
            "spec": json.loads(row["spec"] or "{}"),
        }
        if presence:
            agent = db().execute(
                "SELECT status FROM agents WHERE name = ?", (row["identity"],)
            ).fetchone()
            out["presence"] = {
                "online": bool(agent and agent["status"] == "online"),
                "bound": row["identity"] in set(registry.names()),
            }
        return out

    def squad_json(row: sqlite3.Row) -> dict:
        purge_expired_memberships(db())
        count = db().execute(
            "SELECT COUNT(*) AS n FROM squad_members WHERE squad = ?",
            (row["name"],),
        ).fetchone()["n"]
        return {
            "name": row["name"],
            "description": row["description"],
            "board_visibility": row["board_visibility"],
            "archived": bool(row["archived"]),
            "member_count": count,
        }

    def workspace_json(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "machine": row["machine"],
            "squad": row["squad"],
            "listings": json.loads(row["listings"]),
        }

    def placement_status(row: sqlite3.Row) -> str:
        if row["observed_state"] is None:
            return "pending-edge"
        # `ran` is a headless placement's TERMINAL desired state, and no
        # enumeration ever literally reads "ran" — the edge reports what it
        # SAW: `completed` (exit 0) satisfies the ask; `running` is the
        # errand in flight, which is a delay, not a disagreement, and
        # calling it diverged would page someone about a job doing exactly
        # what was asked. Everything else — `failed`, `stopped` (created
        # but never ran) — genuinely diverges, loudly.
        if row["desired"] == "ran":
            if row["observed_state"] == "completed":
                return "converged"
            if row["observed_state"] == "running":
                return "in-flight"
            return "diverged"
        if row["observed_state"] == row["desired"]:
            return "converged"
        return "diverged"

    def placement_json(row: sqlite3.Row) -> dict:
        out = {
            "id": row["id"],
            "seat": row["seat"],
            "machine": row["machine"],
            "substrate": row["substrate"],
            "desired": row["desired"],
            "observed": {
                "state": row["observed_state"],
                "at": row["observed_at"],
                "enumeration": json.loads(row["observed_enum"]),
            },
            "status": placement_status(row),
        }
        if row["reclaim"]:
            out["reclaim"] = json.loads(row["reclaim"])
        return out

    def active_placements(where: str, args: tuple) -> int:
        return db().execute(
            f"SELECT COUNT(*) AS n FROM api_placements WHERE {where} "
            "AND desired != 'reclaimed'",
            args,
        ).fetchone()["n"]

    def seat_event(identity: str, event: str, actor: str, reason: str = "") -> None:
        """Append one existence-transition to the seat's provenance trail.

        APPEND, never update: current state is the flag, but the HISTORY is
        the trail — and for a purged seat the trail is all that survives
        (the death-fact). Committed by the caller alongside its own write, so
        a transition and its record cannot be split by a crash between them.
        """
        db().execute(
            "INSERT INTO api_seat_events (identity, event, ts, actor, reason)"
            " VALUES (?, ?, ?, ?, ?)",
            (identity, event, _now(), actor, reason),
        )

    def seat_collision(identity: str) -> JSONResponse | None:
        """The one honest answer to "may this identity be created?".

        Pre-W1.1, three creation paths each ran an unfiltered existence
        check and said "already exists" — a lie for an ARCHIVED row, which
        404s on GET and cannot be inspected. The tombstone was undiagnosable
        from the message that reported it (dev-vm-1 recovered one by hand
        UPDATE on prod, 2026-08-10). One helper so a fourth creation path
        cannot re-introduce the split.
        """
        row = db().execute(
            "SELECT archived FROM api_seats WHERE identity = ?", (identity,)
        ).fetchone()
        if not row:
            return None
        if row["archived"]:
            return _err(
                409,
                f"archived seat '{identity}' holds this name — restore it "
                f"with `mcp-hub seats restore {identity}` or free the name "
                f"with `mcp-hub seats rm {identity} --purge --yes`",
            )
        return _err(409, f"seat '{identity}' already exists")

    # -- machines -----------------------------------------------------------

    @route("/api/v1/machines", methods=["GET", "POST"])
    async def machines(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        if request.method == "GET":
            rows = db().execute(
                "SELECT * FROM api_machines WHERE archived = 0"
            ).fetchall()
            # Each machine's reported roster rides along: the board needs
            # agent → worktree to attribute a REMOTE row to a workspace
            # exactly, and it already fetches machines on the poll that builds
            # the tree. A machine whose edge has not reported one is ABSENT
            # here rather than present-and-empty, so the board can tell "no
            # roster reported" from "no agents" and fall back accordingly.
            reported: dict[str, list[dict[str, Any]]] = {}
            for a in db().execute(
                "SELECT machine, agent, worktree, comms, running"
                " FROM api_machine_agents ORDER BY machine, agent"
            ).fetchall():
                row: dict[str, Any] = {
                    "agent": a["agent"],
                    "worktree": a["worktree"],
                    "comms": bool(a["comms"]),
                }
                # Omitted when NULL, so the wire shape says "unknown" the same
                # way the edge said it — a `false` here would be a claim.
                if a["running"] is not None:
                    row["running"] = bool(a["running"])
                reported.setdefault(a["machine"], []).append(row)
            return JSONResponse({
                "machines": [machine_json(r) for r in rows],
                "agents": reported,
            })
        body = await body_of(request)
        name = body.get("name", "")
        if not name:
            return _err(422, "name required")
        if db().execute(
            "SELECT 1 FROM api_machines WHERE name = ?", (name,)
        ).fetchone():
            return _err(409, f"machine '{name}' already enrolled")
        token = secrets.token_hex(24)
        db().execute(
            "INSERT INTO api_machines (name, os, capabilities, token_hash, created)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                name,
                body.get("os", ""),
                json.dumps(body.get("capabilities", {})),
                _sha(token.encode()),
                _now(),
            ),
        )
        db().commit()
        row = db().execute(
            "SELECT * FROM api_machines WHERE name = ?", (name,)
        ).fetchone()
        return JSONResponse(machine_json(row, token=token), status_code=201)

    @route("/api/v1/machines/{name}", methods=["GET", "PATCH", "DELETE"])
    async def machine_one(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        name = request.path_params["name"]
        row = db().execute(
            "SELECT * FROM api_machines WHERE name = ? AND archived = 0", (name,)
        ).fetchone()
        if not row:
            return _err(404, f"no machine '{name}'")
        if request.method == "GET":
            return JSONResponse(machine_json(row))
        if request.method == "PATCH":
            body = await body_of(request)
            caps = json.loads(row["capabilities"])
            caps.update(body.get("capabilities", {}))
            db().execute(
                "UPDATE api_machines SET capabilities = ?, os = ? WHERE name = ?",
                (json.dumps(caps), body.get("os", row["os"]), name),
            )
            db().commit()
            row = db().execute(
                "SELECT * FROM api_machines WHERE name = ?", (name,)
            ).fetchone()
            return JSONResponse(machine_json(row))
        # DELETE — refcount rule: a machine with live placements cannot retire.
        if active_placements("machine = ?", (name,)):
            return _err(409, "machine has active placements; reclaim them first")
        db().execute("UPDATE api_machines SET archived = 1 WHERE name = ?", (name,))
        db().commit()
        return JSONResponse({"name": name, "archived": True})

    @route("/api/v1/machines/{name}/rotate-token", methods=["POST"])
    async def machine_rotate_token(request: Request) -> Response:
        """Issue a NEW machine token, invalidating the old one.

        Enrolment returns a token exactly once and the hub keeps only a hash,
        so a caller that drops it has destroyed it — with no way back. Both
        machines in this fleet lost theirs that way (2026-07-30), which left
        `edge apply` authenticating with the OPERATOR token: one credential
        that drives every machine, on every box, indefinitely.

        Operator-only on purpose. A machine rotating its own credential is a
        machine that can lock the operator out of it, and the recovery path
        for that is the one that was already missing.
        """
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        name = request.path_params["name"]
        row = db().execute(
            "SELECT * FROM api_machines WHERE name = ? AND archived = 0", (name,)
        ).fetchone()
        if not row:
            return _err(404, f"no machine '{name}'")
        token = secrets.token_hex(24)
        db().execute(
            "UPDATE api_machines SET token_hash = ? WHERE name = ?",
            (_sha(token.encode()), name),
        )
        db().commit()
        row = db().execute(
            "SELECT * FROM api_machines WHERE name = ?", (name,)
        ).fetchone()
        # Same contract as enrolment: returned once, never retrievable. The
        # client persists before printing.
        return JSONResponse(machine_json(row, token=token))

    @route("/api/v1/machines/{name}/placements", methods=["GET"])
    async def machine_placements(request: Request) -> Response:
        got = auth(request)
        if isinstance(got, JSONResponse):
            return got
        principal, mname = got
        name = request.path_params["name"]
        if principal == "machine" and mname != name:
            return _err(403, "machine token may only pull its own placements")
        rows = db().execute(
            "SELECT * FROM api_placements WHERE machine = ?", (name,)
        ).fetchall()
        out = []
        for r in rows:
            p = placement_json(r)
            # Full desired state: the machine token can't read /seats/*, so
            # everything materialization needs rides in the pull itself.
            seat_row = db().execute(
                "SELECT * FROM api_seats WHERE identity = ?", (r["seat"],)
            ).fetchone()
            # An archived seat's spec is withheld — the edge must stop
            # materializing a seat the hub considers gone — EXCEPT for
            # reclaimed placements. The exception is for HARVEST, not
            # destroy: destroy is by name, but docker harvest reads
            # spec.memory_volume and worktree harvest reads the folder;
            # withholding the spec would turn harvest into a clean-looking
            # skip, i.e. silent memory loss. Do not "simplify" this away.
            if seat_row and (
                not seat_row["archived"] or r["desired"] == "reclaimed"
            ):
                p["seat_spec"] = seat_json(seat_row)
            out.append(p)
        return JSONResponse({"placements": out})

    @route("/api/v1/machines/{name}/seats", methods=["GET"])
    async def machine_seats(request: Request) -> Response:
        """A machine's own seat declarations, spec included.

        Exists for the edge's LANE leg: /seats is operator-only, and a lane
        seat (spec.substrate == "lane" — an interactive squad lane enrolled
        so console verbs can reach it) never has a placement, so
        pull_placements cannot carry its spec either. Without this door the
        edge cannot discover which lanes it should drive, and a console
        interrupt into a lane stores an action nothing ever realizes — a
        button that fires into a plane the lane is not on (measured
        2026-08-28: 'stop everyone' interrupted 0 of 7 frozen lanes).
        """
        got = auth(request)
        if isinstance(got, JSONResponse):
            return got
        principal, mname = got
        name = request.path_params["name"]
        if principal == "machine" and mname != name:
            return _err(403, "machine token may only list its own seats")
        rows = db().execute(
            "SELECT * FROM api_seats WHERE machine = ? AND archived = 0",
            (name,),
        ).fetchall()
        return JSONResponse({"seats": [seat_json(r) for r in rows]})

    @route("/api/v1/machines/{name}/watch", methods=["GET"])
    async def machine_watch(request: Request) -> Response:
        """SSE doorbell: 'something changed for you, pull now'.

        The machine opens this OUTBOUND, with the bearer it already uses for
        the pull — the hub never reaches a machine, which is the property the
        whole edge design rests on.

        Heartbeats matter as much as events: without them a dead stream and a
        quiet one look identical, and a client cannot tell whether to reconnect.
        """
        got = auth(request)
        if isinstance(got, JSONResponse):
            return got
        principal, mname = got
        name = request.path_params["name"]
        if principal == "machine" and mname != name:
            return _err(403, "machine token may only watch its own placements")

        # The body lives in watch_stream() rather than here, so it can be
        # driven directly by a test instead of only through a live HTTP stream.
        return StreamingResponse(
            watch_stream(name),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # Proxies that buffer would defeat the entire point.
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @route("/api/v1/machines/{name}/status", methods=["POST"])
    async def machine_status(request: Request) -> Response:
        got = auth(request)
        if isinstance(got, JSONResponse):
            return got
        principal, mname = got
        name = request.path_params["name"]
        if principal == "machine" and mname != name:
            return _err(403, "machine token may only report its own status")
        if not db().execute(
            "SELECT 1 FROM api_machines WHERE name = ? AND archived = 0", (name,)
        ).fetchone():
            return _err(404, f"no machine '{name}'")
        body = await body_of(request)
        now = _now()
        # Discovered workspaces: SNAPSHOT semantics — the report replaces the
        # machine's set (an accreting registry lies by staleness), but board
        # presence pings survive for paths that still exist.
        if "workspaces" in body:
            pings = {
                r["path"]: r["last_open_ping"]
                for r in db().execute(
                    "SELECT path, last_open_ping FROM api_discovered_workspaces"
                    " WHERE machine = ?",
                    (name,),
                ).fetchall()
            }
            db().execute(
                "DELETE FROM api_discovered_workspaces WHERE machine = ?", (name,)
            )
            for ws in body["workspaces"]:
                db().execute(
                    "INSERT INTO api_discovered_workspaces"
                    " (machine, path, folders, error, reported_at, last_open_ping)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        ws["path"],
                        ws.get("folders"),
                        ws.get("error", ""),
                        now,
                        pings.get(ws["path"], 0),
                    ),
                )
        # The machine's roster: agent → worktree. SNAPSHOT semantics like the
        # workspaces above — a retired agent must leave, and an accreting
        # roster would keep attributing rows to folders nobody has any more.
        # Absent key means "this edge does not report rosters yet", which is
        # NOT the same as "this machine has no agents": the board falls back to
        # its old basename matching for such a machine rather than emptying it.
        if isinstance(body.get("agents"), list):
            db().execute(
                "DELETE FROM api_machine_agents WHERE machine = ?", (name,)
            )
            for a in body["agents"]:
                if not isinstance(a, dict) or not a.get("agent"):
                    continue
                # `running` is TRI-STATE and stays that way through the
                # column: NULL means the edge could not read tmux, which is
                # not the same as "down". Storing 0 for unknown would let a
                # board draw every agent on a box as stopped and clear every
                # warning on it — the false calm this whole field exists to
                # prevent.
                running = a.get("running")
                db().execute(
                    "INSERT INTO api_machine_agents"
                    " (machine, agent, worktree, comms, running, reported_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (name, a["agent"], a.get("worktree", ""),
                     1 if a.get("comms") else 0,
                     None if running is None else (1 if running else 0),
                     now),
                )
        # Board presence: "this workspace is open, operator eyes on" — a fact
        # only the board can know. Upserts, because the board may report a
        # workspace no edge has discovered yet (presence itself discovers).
        if body.get("workspace_open"):
            db().execute(
                "INSERT INTO api_discovered_workspaces"
                " (machine, path, reported_at, last_open_ping)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(machine, path)"
                " DO UPDATE SET last_open_ping = excluded.last_open_ping",
                (name, body["workspace_open"], now, now),
            )
        # The edge's report on ITSELF (W1.2). Until this key existed the only
        # machine fact was last_seen — a machine whose edge died 203/EXEC for
        # five days read exactly like a healthy quiet one. `result` is the
        # edge's own verdict; the failure path posts it from the except
        # branch, since a raise before push_status otherwise means no report.
        edge = body.get("edge")
        if isinstance(edge, dict) and edge.get("ts"):
            db().execute(
                "UPDATE api_machines SET edge_last_run = ?, edge_result = ?"
                " WHERE name = ?",
                (float(edge["ts"]), json.dumps(edge), name),
            )
        db().execute(
            "UPDATE api_machines SET last_seen = ? WHERE name = ?", (now, name)
        )
        db().commit()
        # Keys received but not stored are NAMED in the response — the
        # "seats" key was silently dropped here for a month, which is how a
        # working reporting channel stayed invisible (W1.2 B4). A payload key
        # must be handled, or its drop must be observable; never neither.
        handled = {"workspaces", "agents", "workspace_open", "edge"}
        ignored = sorted(k for k in body if k not in handled)
        return JSONResponse({"ok": True, "ignored": ignored})

    OPEN_NOW_WINDOW = 180.0  # seconds; board polls far faster than this

    @route("/api/v1/workspace-registry", methods=["GET"])
    async def workspace_registry(request: Request) -> Response:
        """The manager's one view: registered, on-disk, open-now — drift
        visible in every direction, nothing lost track of."""
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        now = _now()
        defs = db().execute("SELECT * FROM api_workspaces").fetchall()
        disc = db().execute(
            "SELECT * FROM api_discovered_workspaces ORDER BY machine, path"
        ).fetchall()

        def basename(path: str) -> str:
            # NOTE (dev review, cosmetics): machine-less definitions match
            # any machine's discovered basename, so same-named workspaces on
            # two machines cross-satisfy registered/on_disk. Acceptable for
            # the drift view; revisit if definitions ever bind actions.
            return path.rsplit("/", 1)[-1].removesuffix(".code-workspace")

        disc_names = {(d["machine"], basename(d["path"])) for d in disc}
        def_names = set()
        definitions = []
        for w in defs:
            key_machine = w["machine"]
            on_disk = (key_machine, w["name"]) in disc_names or (
                not key_machine
                and any(n == w["name"] for _, n in disc_names)
            )
            def_names.add((key_machine, w["name"]))
            definitions.append({**workspace_json(w), "on_disk": on_disk})
        discovered = [
            {
                "machine": d["machine"],
                "path": d["path"],
                "folders": d["folders"],
                "error": d["error"],
                "reported_at": d["reported_at"],
                "open_now": (now - d["last_open_ping"]) < OPEN_NOW_WINDOW,
                "registered": (d["machine"], basename(d["path"])) in def_names
                or ("", basename(d["path"])) in def_names,
            }
            for d in disc
        ]
        return JSONResponse(
            {"definitions": definitions, "discovered": discovered}
        )

    # -- seats --------------------------------------------------------------

    @route("/api/v1/seats", methods=["GET", "POST"])
    async def seats(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        if request.method == "GET":
            rows = db().execute(
                "SELECT * FROM api_seats WHERE archived = 0"
            ).fetchall()
            return JSONResponse({"seats": [seat_json(r) for r in rows]})
        body = await body_of(request)
        spec = body.get("spec") or {}
        if not isinstance(spec, dict):
            return _err(422, "spec must be an object")
        # W2.3/W2.5 — the control plane holds no secrets, and the seat's
        # permission mode is only sound while the container really contains
        # it. Both are spec-write concerns, so one guard covers both.
        bad = validate_spec(spec)
        if bad:
            return _err(422, bad)
        # A docker unit is named by its IMAGE, not by a folder on a host — an
        # nginx container has no worktree and never will. Requiring one would
        # make every non-agent unit lie about itself.
        # ...and it has no git remote either, for the same reason. An image
        # unit needs only a machine and, when its identity is not given, a
        # repo to build a name from. Demanding a repo of nginx forces an
        # operator to invent a field the roster then carries forever.
        # ...and a WORKTREE unit need not have a remote either. Most of the
        # on-demand roster is plain folders — `squad add-folder` exists
        # precisely because "a folder that already exists becomes an agent",
        # git optional. Demanding a repo excluded 13 of dev-vm-1's 15 faculty
        # agents from ever being placed, i.e. the API could start almost none
        # of the agents worth starting from a UI (measured 2026-08-09).
        #
        # So `repo` is now ONE thing in both branches: the source of a derived
        # name. Give an identity and it is not needed at all.
        required = ["machine"] if spec.get("image") else ["machine", "folder"]
        if not body.get("identity"):
            required.append("repo")  # only as the source of a derived name
        for field in required:
            if not body.get(field):
                return _err(422, f"{field} required")
        # Identity is ASSIGNED here, never derived downstream — a container's
        # hostname must never name a seat (runtime design, identity section).
        identity = body.get("identity") or (
            f"{body['repo'].rsplit('/', 1)[-1]}-{body['machine']}"
        )
        collided = seat_collision(identity)
        if collided is not None:
            return collided
        db().execute(
            "INSERT INTO api_seats (identity, repo, machine, folder, launch_args,"
            " class, created, spec) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identity,
                # .get for BOTH, and for the same reason twice over: each
                # time a field stops being required above, an INSERT that
                # still subscripts it turns a relaxed rule into a 500.
                # `folder` did it once (container seats), `repo` did it
                # again the moment an image unit could be named explicitly.
                # The validation block above is the only place that decides
                # what is mandatory; this one must never re-assert it.
                body.get("repo", ""),
                body["machine"],
                body.get("folder", ""),
                body.get("launch_args", ""),
                body.get("class", "squad"),
                _now(),
                json.dumps(spec),
            ),
        )
        db().commit()
        row = db().execute(
            "SELECT * FROM api_seats WHERE identity = ?", (identity,)
        ).fetchone()
        return JSONResponse(seat_json(row), status_code=201)

    @route("/api/v1/seats/{identity}", methods=["GET", "PATCH", "DELETE"])
    async def seat_one(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        identity = request.path_params["identity"]
        row = db().execute(
            "SELECT * FROM api_seats WHERE identity = ? AND archived = 0",
            (identity,),
        ).fetchone()
        if (
            not row
            and request.method == "DELETE"
            and request.query_params.get("purge") == "true"
        ):
            # Purge is precisely for rows the live-only lookup cannot see —
            # an archived seat owning a name forever is the tombstone.
            row = db().execute(
                "SELECT * FROM api_seats WHERE identity = ?", (identity,)
            ).fetchone()
        if not row:
            return _err(404, f"no seat '{identity}'")
        if request.method == "GET":
            return JSONResponse(seat_json(row, presence=True))
        if request.method == "PATCH":
            body = await body_of(request)
            # 🔴 THE SPEC WAS UNEDITABLE, which made this route unable to
            # change anything that matters about a container seat: image, env,
            # volumes — and, once briefs existed, the BRIEF. Re-briefing a
            # seat meant reclaiming its placement, archiving it and declaring
            # a new one, which is a teardown dressed as an edit.
            #
            # MERGED, not replaced: a PATCH that sent only `{"brief": ...}`
            # and silently dropped the image would produce a seat that could
            # never be materialized again, and nothing would say why. Callers
            # patch what they mean to change.
            spec = json.loads(row["spec"] or "{}")
            incoming = body.get("spec")
            if incoming is not None:
                if not isinstance(incoming, dict):
                    return _err(422, "spec must be an object")
                # Only what the caller SENT is validated: a rule added today
                # must not make an existing seat un-patchable because of a
                # key it is not touching.
                bad = validate_spec(
                    incoming,
                    keys={k for k, v in incoming.items() if v is not None},
                )
                if bad:
                    return _err(422, bad)
                for k, v in incoming.items():
                    if v is None:
                        spec.pop(k, None)   # explicit null REMOVES a key
                    else:
                        spec[k] = v
            db().execute(
                "UPDATE api_seats SET launch_args = ?, class = ?, spec = ?"
                " WHERE identity = ?",
                (
                    body.get("launch_args", row["launch_args"]),
                    body.get("class", row["class"]),
                    json.dumps(spec),
                    identity,
                ),
            )
            db().commit()
            row = db().execute(
                "SELECT * FROM api_seats WHERE identity = ?", (identity,)
            ).fetchone()
            return JSONResponse(seat_json(row))
        if request.query_params.get("purge") == "true":
            # Raw count, deliberately NOT active_placements: that helper
            # excludes desired='reclaimed' rows, and a purge gated on it
            # would delete a seat whose reclaimed placement row still
            # references it — a dangling pointer minted by the delete verb.
            n = db().execute(
                "SELECT COUNT(*) AS n FROM api_placements WHERE seat = ?",
                (identity,),
            ).fetchone()["n"]
            if n:
                return _err(
                    409,
                    f"{n} placement row(s) still reference '{identity}' — "
                    "nothing dies unnamed; drop them first with "
                    "`mcp-hub placements unplace <id> --yes`",
                )
            db().execute(
                "DELETE FROM api_seats WHERE identity = ?", (identity,)
            )
            # The death-fact outlives the row it describes.
            seat_event(identity, "purged", "operator-api")
            db().commit()
            return JSONResponse({"identity": identity, "purged": True})
        if active_placements("seat = ?", (identity,)):
            return _err(409, "seat has active placements; reclaim them first")
        db().execute(
            "UPDATE api_seats SET archived = 1 WHERE identity = ?", (identity,)
        )
        seat_event(identity, "archived", "operator-api")
        db().commit()
        # After the commit: a doorbell that can break a write is worse than
        # no doorbell. The edge reconciles the disappearance promptly instead
        # of at the next timer tick.
        notify_machine(row["machine"], "seat-archived")
        return JSONResponse({"identity": identity, "archived": True})

    # -- seat control plane (cards #144 design / #152 build, phase 1) --------
    #
    # Three verbs: watch, prompt, interrupt. Everything here is a RECORD —
    # the console asks, the seat's own machine acts under its own machine
    # token, and the outcome carries what was OBSERVED rather than what was
    # assumed. See docs/seat-control-plane.md.

    # Phase 1's closed verb set. `answer` and `restart` are phase 2 and are
    # refused BY NAME below: a refusal that does not say "not yet" gets read
    # as "never", and the whole category then looks forbidden (the
    # headless-pod lesson — a refusal justified by one mechanism outlived
    # the mechanism).
    _PHASE1_VERBS = SEAT_PHASE1_VERBS
    _PHASE2_VERBS = ("answer", "restart")
    # A hold's expiry is MANDATORY and bounded (bar 14/42, 2026-09-01). The
    # stored value is a release TIME, never a held flag — the same safety
    # design as focus_until, for the same reason: a stop that can be left on
    # forever is a silent-drop bug, and a lane nobody remembers holding is
    # indistinguishable from a lane that died. 12h is long enough to span a
    # usage window and short enough that forgetting is survivable.
    _HOLD_MAX_SECONDS = 12 * 3600.0
    # An interrupt written during a stall must not land minutes later in the
    # middle of healthy work. The stored value is a REQUEST TIME and expiry
    # is derived, so a pending action cannot outlive this by being missed.
    _ACTION_TTL_SECONDS = 120.0
    # Same window as the workspace manager's `open now` column, for the same
    # reason: two consecutive missed refreshes are survivable, a closed
    # laptop is not a viewer.
    _WATCH_WINDOW_SECONDS = 180.0
    _PANE_RING = 20

    def _seat_row(identity: str):
        return db().execute(
            "SELECT * FROM api_seats WHERE identity = ? AND archived = 0",
            (identity,),
        ).fetchone()

    def _expire_stale_actions(seat: str) -> None:
        """Age out pending intent. A PURGE would be wrong here — a vanished
        action is indistinguishable from one never written — so this marks
        `expired` and leaves the row as the record that it lapsed.

        Guarded by a cheap SELECT for the same reason the loan purge is: an
        unconditional UPDATE takes a write lock on every read path, which
        surfaced immediately as `database is locked` the first time.
        """
        cutoff = _now() - _ACTION_TTL_SECONDS
        stale = db().execute(
            "SELECT 1 FROM api_seat_actions WHERE seat = ? AND status = "
            "'pending' AND requested_at < ? LIMIT 1", (seat, cutoff),
        ).fetchone()
        if not stale:
            return
        db().execute(
            "UPDATE api_seat_actions SET status = 'expired', settled_at = ? "
            "WHERE seat = ? AND status = 'pending' AND requested_at < ?",
            (_now(), seat, cutoff),
        )
        db().commit()

    def _action_json(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "seat": row["seat"],
            "kind": row["kind"],
            "args": json.loads(row["args"] or "{}"),
            "requested_at": row["requested_at"],
            "requested_by": row["requested_by"],
            "status": row["status"],
            # NULL stays NULL: "not observed yet" is not an empty pane.
            "observed": json.loads(row["observed"]) if row["observed"] else None,
            "pane_after": row["pane_after"],
            "settled_at": row["settled_at"],
        }

    def _owning_machine_only(request: Request, identity: str):
        """Operator, or the machine this seat is placed on — nobody else.

        The console may only ASK (it writes intent through operator_only);
        this gate is for the READ/REPORT side, where a machine acts on its
        own seats. Letting box-2 report an outcome for box-1's seat would
        hand one compromised machine the fleet, which is the entire reason
        this design records intent instead of opening a shell.
        """
        got = auth(request)
        if isinstance(got, JSONResponse):
            return got
        principal, machine = got
        row = _seat_row(identity)
        if row is None:
            return _err(404, f"no such seat '{identity}'")
        if principal == "operator":
            return got
        if machine != row["machine"]:
            return _err(
                403,
                f"seat '{identity}' is placed on '{row['machine']}', not "
                f"'{machine}' — a machine may only act on its own seats",
            )
        return got

    @route("/api/v1/seats/{identity}/actions", methods=["GET", "POST"])
    async def seat_actions(request: Request) -> Response:
        identity = request.path_params["identity"]

        if request.method == "POST":
            # Operator token ONLY. Seats must not be able to drive each
            # other; same stance as feature-set registration. A machine
            # token here would make the edge both asker and actor.
            got = operator_only(request)
            if isinstance(got, JSONResponse):
                return got
            row = _seat_row(identity)
            if row is None:
                return _err(404, f"no such seat '{identity}'")
            body = await body_of(request)
            kind = str(body.get("kind") or "")
            args = body.get("args") or {}
            if kind in _PHASE2_VERBS:
                return _err(
                    400,
                    f"'{kind}' is PHASE 2 of the seat control plane and is "
                    f"not built yet — phase 1 is {', '.join(_PHASE1_VERBS)}. "
                    f"({kind} needs the fail-closed dialog parser / the "
                    f"seat image's --continue leg; see "
                    f"docs/seat-control-plane.md)",
                )
            if kind not in _PHASE1_VERBS:
                return _err(
                    400,
                    f"unknown seat action '{kind}'. The verb set is CLOSED "
                    f"by design — no arbitrary keystroke pass-through, no "
                    f"shell. Phase 1 verbs: {', '.join(_PHASE1_VERBS)}",
                )
            if kind == "prompt" and not str(args.get("text") or "").strip():
                return _err(
                    400,
                    "a prompt needs `args.text` — a prompt that types "
                    "nothing is a no-op wearing the word prompt",
                )
            if kind == "hold":
                # `until` is required, not defaulted. A default would let a
                # caller hold a lane open-endedly by omission, which is the
                # one shape this verb must not have.
                try:
                    until = float(args.get("until"))
                except (TypeError, ValueError):
                    return _err(
                        400,
                        "a hold needs `args.until` — a unix timestamp when "
                        "it releases ITSELF. It is required and not "
                        "defaulted: a hold with no end is a stopped lane "
                        "nobody remembers stopping, which is indis"
                        "tinguishable from a dead one.",
                    )
                horizon = until - _now()
                if horizon <= 0:
                    return _err(
                        400,
                        f"`until` is in the past ({until}) — that would "
                        f"release the moment it lands. Pass the window "
                        f"reset time.",
                    )
                if horizon > _HOLD_MAX_SECONDS:
                    return _err(
                        400,
                        f"`until` is {horizon / 3600:.1f}h out, over the "
                        f"{_HOLD_MAX_SECONDS / 3600:.0f}h cap. Re-hold if "
                        f"it is still needed — the cap is what makes a "
                        f"forgotten hold survivable.",
                    )
                if not str(args.get("release_condition") or "").strip():
                    return _err(
                        400,
                        "a hold needs `args.release_condition` — the words "
                        "the held lane and the operator are both told. A "
                        "stop whose end nobody can state reads as an "
                        "ignoring lane, not a held one.",
                    )
            # UPSERT, never queue: mashing the button re-states the ask.
            # Superseded intent is recorded as such rather than deleted, so
            # the trail shows what was asked and overtaken.
            db().execute(
                "UPDATE api_seat_actions SET status = 'superseded', "
                "settled_at = ? WHERE seat = ? AND status = 'pending'",
                (_now(), identity),
            )
            cur = db().execute(
                "INSERT INTO api_seat_actions (seat, kind, args, "
                "requested_at, requested_by, status) VALUES (?,?,?,?,?, "
                "'pending')",
                (identity, kind, json.dumps(args), _now(), "operator"),
            )
            db().commit()
            # After the commit — a doorbell that can break a write is worse
            # than no doorbell. This is what turns ~30s into ~1s.
            notify_machine(row["machine"], "seat-action")
            new = db().execute(
                "SELECT * FROM api_seat_actions WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return JSONResponse(_action_json(new), status_code=201)

        got = _owning_machine_only(request, identity)
        if isinstance(got, JSONResponse):
            return got
        _expire_stale_actions(identity)
        rows = db().execute(
            "SELECT * FROM api_seat_actions WHERE seat = ? ORDER BY id DESC "
            "LIMIT 50", (identity,),
        ).fetchall()
        return JSONResponse({"actions": [_action_json(r) for r in rows]})

    @route("/api/v1/seats/{identity}/actions/{action_id}", methods=["PATCH"])
    async def seat_action_report(request: Request) -> Response:
        """The edge reports what it OBSERVED. Not 'we sent Escape' but 'here
        is the pane afterwards' — observed, never assumed."""
        identity = request.path_params["identity"]
        got = _owning_machine_only(request, identity)
        if isinstance(got, JSONResponse):
            return got
        action_id = request.path_params["action_id"]
        row = db().execute(
            "SELECT * FROM api_seat_actions WHERE id = ? AND seat = ?",
            (action_id, identity),
        ).fetchone()
        if row is None:
            return _err(404, f"no action {action_id} on seat '{identity}'")
        body = await body_of(request)
        status = str(body.get("status") or "")
        if status not in ("done", "failed", "refused"):
            return _err(
                400,
                "status must be one of done | failed | refused — `refused` "
                "is a real outcome (a fail-closed edge declining to act), "
                "not an error",
            )
        observed = body.get("observed")
        db().execute(
            "UPDATE api_seat_actions SET status = ?, observed = ?, "
            "pane_after = ?, settled_at = ? WHERE id = ?",
            (status,
             json.dumps(observed) if observed is not None else None,
             body.get("pane_after"), _now(), action_id),
        )
        db().commit()
        new = db().execute(
            "SELECT * FROM api_seat_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return JSONResponse(_action_json(new))

    @route("/api/v1/seats/{identity}/watch", methods=["GET", "POST"])
    async def seat_watch(request: Request) -> Response:
        """View ON DEMAND. No viewer declared → the edge streams nothing; a
        fleet permanently streaming every pane is cost and exposure with no
        reader."""
        identity = request.path_params["identity"]

        if request.method == "POST":
            got = operator_only(request)
            if isinstance(got, JSONResponse):
                return got
            if _seat_row(identity) is None:
                return _err(404, f"no such seat '{identity}'")
            db().execute(
                "INSERT INTO api_seat_viewers (seat, declared_at, declared_by)"
                " VALUES (?,?,?) ON CONFLICT(seat) DO UPDATE SET "
                "declared_at = excluded.declared_at",
                (identity, _now(), "operator"),
            )
            db().commit()
            row = _seat_row(identity)
            notify_machine(row["machine"], "seat-watch")
            return JSONResponse({"seat": identity, "watching": True})

        got = _owning_machine_only(request, identity)
        if isinstance(got, JSONResponse):
            return got
        row = db().execute(
            "SELECT declared_at FROM api_seat_viewers WHERE seat = ?",
            (identity,),
        ).fetchone()
        watching = bool(
            row and (_now() - row["declared_at"]) <= _WATCH_WINDOW_SECONDS
        )
        return JSONResponse({
            "seat": identity,
            "watching": watching,
            "window_seconds": _WATCH_WINDOW_SECONDS,
        })

    @route("/api/v1/seats/{identity}/view", methods=["GET", "POST"])
    async def seat_view(request: Request) -> Response:
        identity = request.path_params["identity"]
        got = _owning_machine_only(request, identity)
        if isinstance(got, JSONResponse):
            return got

        if request.method == "POST":
            body = await body_of(request)
            pane = body.get("pane")
            if pane is None:
                return _err(400, "a capture needs `pane`")
            db().execute(
                "INSERT INTO api_seat_panes (seat, pane, captured_at) "
                "VALUES (?,?,?)", (identity, str(pane), _now()),
            )
            # Ring buffer, trimmed on write: this is a LIVE VIEW, and a
            # durable pane history is a transcript nobody asked for.
            db().execute(
                "DELETE FROM api_seat_panes WHERE seat = ? AND id NOT IN ("
                "SELECT id FROM api_seat_panes WHERE seat = ? "
                "ORDER BY id DESC LIMIT ?)",
                (identity, identity, _PANE_RING),
            )
            db().commit()
            return JSONResponse({"seat": identity, "captured": True})

        row = db().execute(
            "SELECT pane, captured_at FROM api_seat_panes WHERE seat = ? "
            "ORDER BY id DESC LIMIT 1", (identity,),
        ).fetchone()
        agg = db().execute(
            "SELECT COUNT(*) AS n, MIN(captured_at) AS oldest "
            "FROM api_seat_panes WHERE seat = ?", (identity,),
        ).fetchone()
        # ⚠️ THE RING IS COUNT-BOUNDED, AND A COUNT BOUND HAS NO DURATION.
        # Its horizon is `_PANE_RING ÷ write rate`, so it SHRINKS as the seat
        # gets busier — weeks on a quiet seat, minutes on a thrashing one,
        # i.e. shortest during an incident, which is exactly when someone
        # opens it (vps, 2026-08-23). A time bound degrades gracefully; a
        # count bound degrades ADVERSARIALLY.
        #
        # So the response reports the reach it OBSERVED rather than leaving
        # the reader to assume one: `kept` counts captures and
        # `oldest_captured_at` dates the far end. Never quote this as
        # "the last N minutes" — it does not have an N.
        #
        # Corollary for anyone investigating with it: watching a busy seat
        # GENERATES writes, so your own looking consumes the evidence.
        # Snapshot before poking.
        return JSONResponse({
            "seat": identity,
            # `pane: null` when nothing has been captured — absence of
            # measurement must not render as a measurement of emptiness.
            "pane": row["pane"] if row else None,
            "captured_at": row["captured_at"] if row else None,
            "kept": agg["n"],
            "oldest_captured_at": agg["oldest"],
            "ring": _PANE_RING,
        })

    @route("/api/v1/seats/{identity}/restore", methods=["POST"])
    async def seat_restore(request: Request) -> Response:
        """The inverse of archive — the verb whose absence made archived
        identities unrecoverable (the only restore path was a hand UPDATE on
        prod, 2026-08-10). Archive FREEZES (one axis, nothing else mutated),
        so restore reconstructs nothing — but it must RE-RUN create
        validation: the invariant that held at archive time may not hold in
        the changed world (FDM's scoped-uniqueness lesson)."""
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        identity = request.path_params["identity"]
        row = db().execute(
            "SELECT * FROM api_seats WHERE identity = ?", (identity,)
        ).fetchone()
        if not row:
            return _err(404, f"no seat '{identity}'")
        if not row["archived"]:
            return _err(
                409, f"seat '{identity}' is not archived — nothing to restore"
            )
        if not db().execute(
            "SELECT 1 FROM api_machines WHERE name = ? AND archived = 0",
            (row["machine"],),
        ).fetchone():
            return _err(
                409,
                f"machine '{row['machine']}' is gone or archived — a seat "
                "cannot return to a machine that no longer exists; declare "
                "it afresh with `mcp-hub seats add`",
            )
        db().execute(
            "UPDATE api_seats SET archived = 0 WHERE identity = ?", (identity,)
        )
        seat_event(identity, "restored", "operator-api")
        db().commit()
        # Symmetric with archive: an asymmetric doorbell is how observers
        # drift out of sync with the record.
        notify_machine(row["machine"], "seat-restored")
        row = db().execute(
            "SELECT * FROM api_seats WHERE identity = ?", (identity,)
        ).fetchone()
        return JSONResponse(seat_json(row))

    @route("/api/v1/seats/{identity}/clone", methods=["POST"])
    async def seat_clone(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        identity = request.path_params["identity"]
        row = db().execute(
            "SELECT * FROM api_seats WHERE identity = ? AND archived = 0",
            (identity,),
        ).fetchone()
        if not row:
            return _err(404, f"no seat '{identity}'")
        body = await body_of(request)
        suffix = body.get("suffix", "")
        machine = body.get("machine", row["machine"])
        if not suffix:
            return _err(422, "suffix required")
        new_identity = f"{identity}-{suffix}"
        collided = seat_collision(new_identity)
        if collided is not None:
            return collided
        # 🔴 THE SPEC MUST TRAVEL. This INSERT omitted `spec` entirely, so
        # cloning a DOCKER seat produced a row with no image, no volumes, no
        # env and no brief — a worktree seat wearing the original's name. It
        # would have been declared successfully and then failed to materialize
        # with a message about the wrong thing.
        #
        # Pod inhabitants are re-identified with the same suffix, for the
        # reason capsule minting learned the hard way: suffixing only the
        # container leaves two containers holding agents with IDENTICAL hub
        # names, which moves the collision somewhere nothing can see it.
        spec = json.loads(row["spec"] or "{}")
        if spec.get("agents"):
            spec["agents"] = [
                {**a, "identity": f"{a.get('identity', '')}-{suffix}"}
                for a in spec["agents"]
            ]
            if spec.get("squad"):
                spec["squad"] = f"{spec['squad']}-{suffix}"
        if spec.get("memory_volume"):
            # A clone sharing the original's memory volume would have the two
            # seats writing each other's memory and results — and a reclaim of
            # either would harvest a volume the other still uses.
            spec["memory_volume"] = f"{spec['memory_volume']}-{suffix}"
        # DELIBERATELY NOT re-validated (W2.3): the brief and inputs here are
        # the SOURCE seat's, already accepted once. A guard added today must
        # not make a seat declared yesterday uncloneable. Volumes ARE checked,
        # because the sandbox premise is a property of the container this call
        # is about to create, not of the one it copied from.
        bad = validate_spec(spec, keys={"volumes"})
        if bad:
            return _err(422, bad)
        db().execute(
            "INSERT INTO api_seats (identity, repo, machine, folder, launch_args,"
            " class, cloned_from, created, spec)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_identity,
                row["repo"],
                machine,
                row["folder"],
                row["launch_args"],
                row["class"],
                identity,
                _now(),
                json.dumps(spec),
            ),
        )
        db().commit()
        new_row = db().execute(
            "SELECT * FROM api_seats WHERE identity = ?", (new_identity,)
        ).fetchone()
        return JSONResponse(seat_json(new_row), status_code=201)

    # -- squads -------------------------------------------------------------

    @route("/api/v1/feature-sets", methods=["GET", "POST"])
    async def feature_sets(request: Request) -> Response:
        """W3.3 — the ra.feature/1 identity store. Operator-only and
        REST-only on purpose: registering a feature set is a deliberate act,
        and no agent-writable surface is added by this wave."""
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        from mcp_hub import ra_feature
        from mcp_hub.refs import RefError

        ra_feature.ensure_schema(db())
        if request.method == "GET":
            return JSONResponse(
                {"feature_sets": ra_feature.list_feature_sets(db())}
            )
        body = await body_of(request)
        try:
            out = ra_feature.register_feature_set(
                db(), body.get("key", ""), body.get("document") or {},
                registered_by="operator",
            )
        except RefError as e:
            return _err(422, str(e))
        return JSONResponse(out, status_code=201)

    @route("/api/v1/lineage", methods=["GET"])
    async def lineage_walk(request: Request) -> Response:
        """W3.1 — the bounded subgraph walk, operator plane. Query params:
        ref (required), depth, direction, predicate. A whole-graph dump is
        deliberately not offered."""
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        from mcp_hub import lineage as _lineage
        from mcp_hub.refs import RefError

        ref = request.query_params.get("ref", "")
        if not ref:
            return _err(422, "ref required, e.g. ?ref=hub.msg/1%3Fid%3D123")
        try:
            out = _lineage.walk(
                db(), ref,
                depth=int(request.query_params.get("depth", "2")),
                direction=request.query_params.get("direction", "both"),
                predicate=request.query_params.get("predicate") or None,
            )
        except RefError as e:
            return _err(422, str(e))
        return JSONResponse(out)

    @route("/api/v1/lineage/coverage", methods=["GET"])
    async def lineage_coverage(request: Request) -> Response:
        """A9 — what fraction of artifacts carry lineage at all, so a thin
        graph reads as thinly POPULATED, never as thinly connected."""
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        from mcp_hub import lineage as _lineage
        return JSONResponse(_lineage.coverage(db()))

    @route("/api/v1/squads", methods=["GET", "POST"])
    async def squads(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        if request.method == "GET":
            include_archived = (
                request.query_params.get("include_archived") == "true"
            )
            where = "" if include_archived else "WHERE archived = 0"
            rows = db().execute(f"SELECT * FROM api_squads {where}").fetchall()
            return JSONResponse({"squads": [squad_json(r) for r in rows]})
        body = await body_of(request)
        name = body.get("name", "")
        if not name:
            return _err(422, "name required")
        visibility = body.get("board_visibility", "shown")
        if visibility not in ("shown", "hidden"):
            return _err(422, "board_visibility must be shown|hidden")
        # Archived squads keep their name reserved: history stays attributable.
        if db().execute(
            "SELECT 1 FROM api_squads WHERE name = ?", (name,)
        ).fetchone():
            return _err(409, f"squad '{name}' exists (or is archived; names are reserved)")
        db().execute(
            "INSERT INTO api_squads (name, description, board_visibility, created)"
            " VALUES (?, ?, ?, ?)",
            (name, body.get("description", ""), visibility, _now()),
        )
        db().commit()
        row = db().execute(
            "SELECT * FROM api_squads WHERE name = ?", (name,)
        ).fetchone()
        return JSONResponse(squad_json(row), status_code=201)

    @route("/api/v1/squads/{name}", methods=["GET", "PATCH", "DELETE"])
    async def squad_one(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        name = request.path_params["name"]
        row = db().execute(
            "SELECT * FROM api_squads WHERE name = ? AND archived = 0", (name,)
        ).fetchone()
        # RETIREMENT MUST BE COMPLETABLE. A bare archive leaves the comms
        # memberships behind, and the name is then reserved (POST 409) while
        # every route here 404s on `archived = 0` — so the operator who
        # archived a dead squad had no way to finish the job, and it kept
        # appearing in the console's Squads tab forever (found live retiring
        # `capsule`, console #168). Purge stays reachable on an archived row;
        # a bare DELETE does not, or "archive" would stop meaning anything.
        purge_wanted = (request.method == "DELETE"
                        and request.query_params.get("purge") == "true")
        if not row and purge_wanted:
            row = db().execute(
                "SELECT * FROM api_squads WHERE name = ?", (name,)
            ).fetchone()
        # TWO REGISTRIES, ONE INTENT. A squad can exist ONLY in comms
        # membership — agents named it in register()/set_squads and nothing
        # ever registered it with the runtime (`duo`, retired 2026-08-21).
        # It shows in the console's Squads tab like any other and every route
        # here 404s for want of an api_squads row, so it is unretireable by
        # construction. "Retire this squad" must reach whichever registries
        # the name is actually in.
        comms_only = False
        if not row and purge_wanted:
            comms_only = bool(db().execute(
                "SELECT 1 FROM squad_members WHERE squad = ? LIMIT 1", (name,)
            ).fetchone())
        if not row and not comms_only:
            return _err(404, f"no squad '{name}'")
        if request.method == "GET":
            return JSONResponse(squad_json(row))
        if request.method == "PATCH":
            body = await body_of(request)
            new_name = body.get("name", name)
            if new_name != name and db().execute(
                "SELECT 1 FROM api_squads WHERE name = ?", (new_name,)
            ).fetchone():
                return _err(409, f"squad '{new_name}' already exists")
            visibility = body.get("board_visibility", row["board_visibility"])
            if visibility not in ("shown", "hidden"):
                return _err(422, "board_visibility must be shown|hidden")
            # Rename cascades atomically: the record and every membership move
            # in one transaction or not at all.
            db().execute(
                "UPDATE api_squads SET name = ?, description = ?,"
                " board_visibility = ? WHERE name = ?",
                (new_name, body.get("description", row["description"]),
                 visibility, name),
            )
            db().execute(
                "UPDATE squad_members SET squad = ? WHERE squad = ?",
                (new_name, name),
            )
            # Queued squad-scoped broadcasts move too, or a member with
            # unread items at rename time loses them silently — the drain
            # matches audience against the NEW name (dev's review find,
            # silent-loss family). History stays attributable under the
            # surviving name, consistent with history-is-immortal.
            db().execute(
                "UPDATE messages SET audience = ? WHERE audience = ?",
                (new_name, name),
            )
            db().commit()
            row = db().execute(
                "SELECT * FROM api_squads WHERE name = ?", (new_name,)
            ).fetchone()
            return JSONResponse(squad_json(row))
        # DELETE — archive always; purge removes STRUCTURE only. Message and
        # broadcast history is immortal by decision (2026-07-29): the fleet
        # treats history as the record.
        purge = request.query_params.get("purge") == "true"
        out: dict[str, Any] = {"name": name, "archived": True}
        if purge:
            n = db().execute(
                "SELECT COUNT(*) AS n FROM squad_members WHERE squad = ?", (name,)
            ).fetchone()["n"]
            db().execute("DELETE FROM squad_members WHERE squad = ?", (name,))
            out["purged"] = {"memberships": n}
            out["messages_retained"] = True
        db().execute("UPDATE api_squads SET archived = 1 WHERE name = ?", (name,))
        db().commit()
        return JSONResponse(out)

    @route("/api/v1/squads/{name}/members", methods=["GET"])
    async def squad_members_list(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        name = request.path_params["name"]
        if not db().execute(
            "SELECT 1 FROM api_squads WHERE name = ? AND archived = 0", (name,)
        ).fetchone():
            return _err(404, f"no squad '{name}'")
        purge_expired_memberships(db())
        rows = db().execute(
            "SELECT * FROM squad_members WHERE squad = ? ORDER BY joined", (name,)
        ).fetchall()
        return JSONResponse(
            {
                "members": [
                    {
                        "seat": r["agent"],
                        "muted": bool(r["muted"]),
                        "source": r["source"],
                        "joined": r["joined"],
                        # 0 = permanent. Reported for every member so a reader
                        # never has to infer "no key means forever" — a loan
                        # and an ordinary membership must be distinguishable
                        # without knowing the convention.
                        "expires": r["expires"],
                    }
                    for r in rows
                ]
            }
        )

    @route(
        "/api/v1/squads/{name}/members/{seat}",
        methods=["PUT", "PATCH", "DELETE"],
    )
    async def squad_member_one(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        name = request.path_params["name"]
        seat = request.path_params["seat"]
        if not db().execute(
            "SELECT 1 FROM api_squads WHERE name = ? AND archived = 0", (name,)
        ).fetchone():
            return _err(404, f"no squad '{name}'")
        if request.method == "PUT":
            body = await body_of(request)
            try:
                expires = float(body.get("expires", 0) or 0)
            except (TypeError, ValueError):
                return _err(422, "expires must be a unix timestamp (0 = permanent)")
            if expires and expires <= _now():
                # Accepting it would create a membership that the very next
                # read deletes — the caller would see success and then an
                # absent member, and reasonably conclude the add had failed.
                return _err(422, "expires is in the past — that loan is already over")
            db().execute(
                "INSERT INTO squad_members"
                " (agent, squad, muted, joined, source, expires)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(agent, squad)"
                " DO UPDATE SET muted = excluded.muted,"
                "               source = excluded.source,"
                "               expires = excluded.expires",
                (seat, name, int(bool(body.get("muted", False))), _now(),
                 str(body.get("source") or "api"), expires),
            )
            db().commit()
            return JSONResponse(
                {"seat": seat, "squad": name, "expires": expires}
            )
        member = db().execute(
            "SELECT 1 FROM squad_members WHERE agent = ? AND squad = ?",
            (seat, name),
        ).fetchone()
        if not member:
            return _err(404, f"'{seat}' is not a member of '{name}'")
        if request.method == "PATCH":
            body = await body_of(request)
            if "muted" in body:
                db().execute(
                    "UPDATE squad_members SET muted = ? WHERE agent = ? AND squad = ?",
                    (int(bool(body["muted"])), seat, name),
                )
                db().commit()
            return JSONResponse({"seat": seat, "squad": name})
        db().execute(
            "DELETE FROM squad_members WHERE agent = ? AND squad = ?", (seat, name)
        )
        db().commit()
        return JSONResponse({"seat": seat, "squad": name, "removed": True})

    @route("/api/v1/squads/{name}/broadcasts", methods=["GET"])
    async def squad_broadcasts(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        name = request.path_params["name"]
        if not db().execute(
            "SELECT 1 FROM api_squads WHERE name = ? AND archived = 0", (name,)
        ).fetchone():
            return _err(404, f"no squad '{name}'")
        rows = db().execute(
            "SELECT id, from_agent, content, timestamp FROM messages"
            " WHERE to_agent = '*' AND audience = ? ORDER BY id DESC LIMIT 100",
            (name,),
        ).fetchall()
        return JSONResponse(
            {
                "broadcasts": [
                    {
                        "id": r["id"],
                        "from": r["from_agent"],
                        "body": r["content"],
                        "ts": r["timestamp"],
                    }
                    for r in rows
                ]
            }
        )

    # -- workspaces ---------------------------------------------------------

    @route("/api/v1/workspaces", methods=["GET", "POST"])
    async def workspaces(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        if request.method == "GET":
            rows = db().execute("SELECT * FROM api_workspaces").fetchall()
            return JSONResponse(
                {"workspaces": [workspace_json(r) for r in rows]}
            )
        body = await body_of(request)
        if not body.get("name"):
            return _err(422, "name required")
        squad = body.get("squad", "")
        if squad and not db().execute(
            "SELECT 1 FROM api_squads WHERE name = ? AND archived = 0", (squad,)
        ).fetchone():
            return _err(404, f"no squad '{squad}' to type this workspace with")
        cur = db().execute(
            "INSERT INTO api_workspaces (name, machine, squad, listings, created)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                body["name"],
                body.get("machine", ""),
                squad,
                json.dumps(body.get("listings", [])),
                _now(),
            ),
        )
        db().commit()
        row = db().execute(
            "SELECT * FROM api_workspaces WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return JSONResponse(workspace_json(row), status_code=201)

    @route("/api/v1/workspaces/{wid}", methods=["GET", "PATCH", "DELETE"])
    async def workspace_one(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        wid = request.path_params["wid"]
        row = db().execute(
            "SELECT * FROM api_workspaces WHERE id = ?", (wid,)
        ).fetchone()
        if not row:
            return _err(404, f"no workspace {wid}")
        if request.method == "GET":
            return JSONResponse(workspace_json(row))
        if request.method == "PATCH":
            body = await body_of(request)
            listings = json.loads(row["listings"])
            listings.extend(body.get("add_listings", []))
            remove = {json.dumps(x, sort_keys=True) for x in body.get("remove_listings", [])}
            listings = [
                x for x in listings if json.dumps(x, sort_keys=True) not in remove
            ]
            db().execute(
                "UPDATE api_workspaces SET listings = ? WHERE id = ?",
                (json.dumps(listings), wid),
            )
            db().commit()
            row = db().execute(
                "SELECT * FROM api_workspaces WHERE id = ?", (wid,)
            ).fetchone()
            return JSONResponse(workspace_json(row))
        db().execute("DELETE FROM api_workspaces WHERE id = ?", (wid,))
        db().commit()
        return JSONResponse({"id": row["id"], "removed": True})

    def render_workspace_file(row: sqlite3.Row) -> dict:
        listings = json.loads(row["listings"])
        return {
            "folders": [
                {"path": x["path"]} for x in listings if "path" in x
            ],
            "settings": dict(WS_SETTINGS),
        }

    @route("/api/v1/workspaces/{wid}/file", methods=["GET"])
    async def workspace_file(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        wid = request.path_params["wid"]
        row = db().execute(
            "SELECT * FROM api_workspaces WHERE id = ?", (wid,)
        ).fetchone()
        if not row:
            return _err(404, f"no workspace {wid}")
        return JSONResponse(render_workspace_file(row))

    # -- capsules -----------------------------------------------------------

    def compose_capsule(squad_row: sqlite3.Row) -> tuple[dict, bytes]:
        """Render every artifact a destination needs, hash each, tar the lot.

        The capsule is FROZEN (a snapshot of squad state at compose time),
        INERT (a tarball does nothing), SELF-CONTAINED (no source-machine
        references) and VERIFIABLE (the manifest hashes every entry).
        """
        squad = squad_row["name"]
        # A capsule is meant to REPRODUCE the squad. Freezing a lapsed loan
        # into it would resurrect the borrowed agent on every future place —
        # the expiry would hold on the live squad and be permanently undone by
        # its own snapshot.
        purge_expired_memberships(db())
        member_rows = db().execute(
            "SELECT agent FROM squad_members WHERE squad = ? ORDER BY joined",
            (squad,),
        ).fetchall()
        seats = []
        for m in member_rows:
            seat_row = db().execute(
                "SELECT * FROM api_seats WHERE identity = ? AND archived = 0",
                (m["agent"],),
            ).fetchone()
            if seat_row:
                seats.append(seat_json(seat_row))
            else:
                seats.append({"identity": m["agent"]})

        hub_url = os.environ.get("MCP_HUB_URL", "http://100.109.6.114:8090/mcp")
        artifacts: dict[str, bytes] = {}
        ws = {
            "folders": [
                {"path": s.get("folder", s["identity"])} for s in seats
            ],
            "settings": dict(WS_SETTINGS),
        }
        artifacts[f"{squad}.code-workspace"] = json.dumps(ws, indent=2).encode()
        artifacts["mcp.json.template"] = json.dumps(
            {"mcpServers": {"hub": {"type": "http", "url": f"{hub_url}?agent=__IDENTITY__"}}},
            indent=2,
        ).encode()
        compose = {
            "services": {
                s["identity"]: {
                    "image": "mcp-hub-seat:latest",
                    "environment": {
                        "SEAT_IDENTITY": s["identity"],
                        "SEAT_REPO": s.get("repo", ""),
                        "MCP_HUB_URL": hub_url,
                    },
                }
                for s in seats
            }
        }
        artifacts["docker-compose.yml"] = json.dumps(compose, indent=2).encode()
        artifacts["bootstrap.sh"] = (
            "#!/bin/sh\n# Realize this capsule on the local machine.\n"
            "# Generated by the hub; identities are ASSIGNED in manifest.json —\n"
            "# never derive a seat name from a container hostname.\n"
        ).encode()

        entries = [
            {"path": path, "sha256": _sha(data)}
            for path, data in sorted(artifacts.items())
        ]
        manifest = {"squad": squad, "seats": seats, "entries": entries}
        manifest_bytes = json.dumps(manifest, indent=2).encode()

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for path, data in {**artifacts, "manifest.json": manifest_bytes}.items():
                info = tarfile.TarInfo(name=path)
                info.size = len(data)
                if path.endswith(".sh"):
                    info.mode = 0o755  # a bootstrap that can't run isn't one
                tf.addfile(info, io.BytesIO(data))
        return manifest, buf.getvalue()

    @route("/api/v1/capsules", methods=["GET", "POST"])
    async def capsules(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        if request.method == "GET":
            rows = db().execute(
                "SELECT id, squad, manifest, created FROM api_capsules"
            ).fetchall()
            return JSONResponse(
                {
                    "capsules": [
                        {
                            "id": r["id"],
                            "squad": r["squad"],
                            "manifest": json.loads(r["manifest"]),
                            "created": r["created"],
                        }
                        for r in rows
                    ]
                }
            )
        body = await body_of(request)
        squad = body.get("squad", "")
        squad_row = db().execute(
            "SELECT * FROM api_squads WHERE name = ? AND archived = 0", (squad,)
        ).fetchone()
        if not squad_row:
            return _err(404, f"no squad '{squad}'")
        manifest, tarball = compose_capsule(squad_row)
        cid = f"cap-{secrets.token_hex(8)}"
        db().execute(
            "INSERT INTO api_capsules (id, squad, manifest, tarball, created)"
            " VALUES (?, ?, ?, ?, ?)",
            (cid, squad, json.dumps(manifest), tarball, _now()),
        )
        db().commit()
        return JSONResponse(
            {"id": cid, "squad": squad, "manifest": manifest}, status_code=201
        )

    @route("/api/v1/capsules/{cid}", methods=["GET", "DELETE"])
    async def capsule_one(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        cid = request.path_params["cid"]
        row = db().execute(
            "SELECT id, squad, manifest, created FROM api_capsules WHERE id = ?",
            (cid,),
        ).fetchone()
        if not row:
            return _err(404, f"no capsule '{cid}'")
        if request.method == "GET":
            return JSONResponse(
                {
                    "id": row["id"],
                    "squad": row["squad"],
                    "manifest": json.loads(row["manifest"]),
                    "created": row["created"],
                }
            )
        db().execute("DELETE FROM api_capsules WHERE id = ?", (cid,))
        db().commit()
        return JSONResponse({"id": cid, "removed": True})

    @route("/api/v1/capsules/{cid}/download", methods=["GET"])
    async def capsule_download(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        cid = request.path_params["cid"]
        row = db().execute(
            "SELECT tarball FROM api_capsules WHERE id = ?", (cid,)
        ).fetchone()
        if not row:
            return _err(404, f"no capsule '{cid}'")
        return Response(
            content=row["tarball"],
            media_type="application/gzip",
            headers={"content-disposition": f'attachment; filename="{cid}.tar.gz"'},
        )

    def _mint_capsule_seats(
        frozen: list[dict], label: str, machine: str,
    ) -> tuple[list[str], JSONResponse | None]:
        """Clone a capsule's seats under `<identity>-<label>`. New rows, so the
        second copy is a genuinely separate squad rather than the same one
        described twice.

        🔴 THE POD CASE, which is easy to miss and reintroduces the exact bug
        one level down. A pod seat's spec carries `agents[].identity` — the
        inhabitants' OWN hub names. Suffixing only the container's identity
        would put two containers up with different names holding agents with
        IDENTICAL names, so the collision moves from the thing you can see
        (docker ps) to the thing you cannot. Every inhabitant is re-identified
        with the same label.
        """
        rows: list[tuple] = []
        for s in frozen:
            src = s["identity"]
            new_id = f"{src}-{label}"
            if not s.get("spec") and not s.get("repo"):
                # A member with no seat row froze as a bare {"identity": ...}.
                # There is nothing to clone, and inventing a spec would
                # materialize a container the squad never actually had.
                return [], _err(
                    422,
                    f"'{src}' is a squad member with no seat declaration, so "
                    "there is nothing to copy. Declare it with `mcp-hub seats "
                    "add` and re-compose, or place this capsule as-is.",
                )
            collided = seat_collision(new_id)
            if collided is not None:
                # The archived branch keeps seat_collision's message (naming
                # restore/purge — pre-W1.1 this path claimed "that label has
                # been used for this capsule before", equally a lie for a
                # tombstone); a LIVE collision keeps the capsule-specific
                # advice, which is genuinely better here.
                body = json.loads(bytes(collided.body))
                if "archived" in body.get("detail", ""):
                    return [], collided
                return [], _err(
                    409,
                    f"seat '{new_id}' already exists — that label has been "
                    "used for this capsule before. Pick another `as` label.",
                )
            spec = dict(s.get("spec") or {})
            if spec.get("agents"):
                spec["agents"] = [
                    {**a, "identity": f"{a.get('identity', '')}-{label}"}
                    for a in spec["agents"]
                ]
                if spec.get("squad"):
                    spec["squad"] = f"{spec['squad']}-{label}"
            # The fourth seat-writing route (W2.3) — the one the first draft
            # of this plan missed. Same rule as clone: frozen capsule content
            # is not re-validated, but the volumes of the container about to
            # be created are.
            bad = validate_spec(spec, keys={"volumes"})
            if bad:
                return [], _err(422, bad)
            rows.append((
                new_id, s.get("repo", ""), machine, s.get("folder", ""),
                s.get("launch_args", ""), s.get("class", "squad"),
                src, _now(), json.dumps(spec),
            ))
        for r in rows:
            db().execute(
                "INSERT INTO api_seats (identity, repo, machine, folder,"
                " launch_args, class, cloned_from, created, spec)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", r,
            )
        return [r[0] for r in rows], None

    @route("/api/v1/capsules/{cid}/place", methods=["POST"])
    async def capsule_place(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        cid = request.path_params["cid"]
        row = db().execute(
            "SELECT manifest FROM api_capsules WHERE id = ?", (cid,)
        ).fetchone()
        if not row:
            return _err(404, f"no capsule '{cid}'")
        body = await body_of(request)
        machine = body.get("machine", "")
        if not db().execute(
            "SELECT 1 FROM api_machines WHERE name = ? AND archived = 0",
            (machine,),
        ).fetchone():
            return _err(404, f"no machine '{machine}'")
        manifest = json.loads(row["manifest"])
        label = _sanitize_label(str(body.get("as") or ""))
        if body.get("as") and not label:
            return _err(422, "`as` must contain letters, digits, - or _")

        if label:
            minted, err = _mint_capsule_seats(manifest["seats"], label, machine)
            if err:
                return err
            targets = minted
        else:
            # 🔴 THE COLLISION. A capsule freezes IDENTITIES, so placing one
            # twice used to write a second placement for the same seat on
            # another machine — two containers, one hub identity, both
            # registering, last one silently owning the wake binding. That is
            # the duplicate-agent failure the derived-identity work exists to
            # prevent, and it arrived through the back door: nothing here
            # looked at what was already placed.
            #
            # Running the same squad twice is a REAL need (two takes on one
            # design), so this refuses and names the flag that does it properly
            # rather than refusing and leaving the operator stuck.
            clash = [
                s["identity"] for s in manifest["seats"]
                if db().execute(
                    "SELECT 1 FROM api_placements WHERE seat = ?"
                    " AND desired != 'reclaimed'", (s["identity"],),
                ).fetchone()
            ]
            if clash:
                return _err(
                    409,
                    "already placed: " + ", ".join(sorted(clash))
                    + " — placing this capsule again would give one identity "
                    "two containers, and whichever registered last would "
                    "silently own the name. Reclaim those placements to MOVE "
                    "the squad, or pass `as` to place a SECOND copy under "
                    "fresh identities.",
                )
            targets = [s["identity"] for s in manifest["seats"]]

        ids = []
        for identity in targets:
            pid = f"pl-{secrets.token_hex(8)}"
            db().execute(
                "INSERT INTO api_placements (id, seat, machine, substrate,"
                " desired, created) VALUES (?, ?, ?, 'docker', 'running', ?)",
                (pid, identity, machine, _now()),
            )
            ids.append(pid)
        db().commit()
        notify_machine(machine)
        return JSONResponse(
            {"placements": ids, "seats": targets}, status_code=201
        )

    # -- placements ---------------------------------------------------------

    @route("/api/v1/placements", methods=["GET", "POST"])
    async def placements(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        if request.method == "GET":
            machine = request.query_params.get("machine")
            if machine:
                rows = db().execute(
                    "SELECT * FROM api_placements WHERE machine = ?", (machine,)
                ).fetchall()
            else:
                rows = db().execute("SELECT * FROM api_placements").fetchall()
            return JSONResponse({"placements": [placement_json(r) for r in rows]})
        body = await body_of(request)
        substrate = body.get("substrate", "")
        if substrate not in SUBSTRATES:
            return _err(422, f"substrate must be one of {SUBSTRATES}")
        seat = body.get("seat", "")
        if not db().execute(
            "SELECT 1 FROM api_seats WHERE identity = ? AND archived = 0", (seat,)
        ).fetchone():
            return _err(404, f"no seat '{seat}'")
        machine = body.get("machine", "")
        if not db().execute(
            "SELECT 1 FROM api_machines WHERE name = ? AND archived = 0",
            (machine,),
        ).fetchone():
            return _err(404, f"no machine '{machine}'")
        pid = f"pl-{secrets.token_hex(8)}"
        db().execute(
            "INSERT INTO api_placements (id, seat, machine, substrate, desired,"
            " created) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, seat, machine, substrate, body.get("desired", "running"), _now()),
        )
        db().commit()
        notify_machine(machine)
        row = db().execute(
            "SELECT * FROM api_placements WHERE id = ?", (pid,)
        ).fetchone()
        return JSONResponse(placement_json(row), status_code=201)

    @route("/api/v1/placements/{pid}", methods=["GET", "PATCH", "DELETE"])
    async def placement_one(request: Request) -> Response:
        got = operator_only(request)
        if isinstance(got, JSONResponse):
            return got
        pid = request.path_params["pid"]
        row = db().execute(
            "SELECT * FROM api_placements WHERE id = ?", (pid,)
        ).fetchone()
        if not row:
            return _err(404, f"no placement '{pid}'")
        if request.method == "GET":
            return JSONResponse(placement_json(row))
        if request.method == "PATCH":
            body = await body_of(request)
            desired = body.get("desired", row["desired"])
            # `ran` — run once, ever — is how a HEADLESS seat is asked for:
            # `running` would make the reconciler restart the finished
            # container and re-run the errand. `reclaimed` is still not a
            # value here: destroy stays behind its own verb (DELETE).
            if desired not in ("running", "stopped", "ran"):
                return _err(422, "desired must be running|stopped|ran")
            db().execute(
                "UPDATE api_placements SET desired = ? WHERE id = ?",
                (desired, pid),
            )
            db().commit()
            notify_machine(row["machine"])
            row = db().execute(
                "SELECT * FROM api_placements WHERE id = ?", (pid,)
            ).fetchone()
            return JSONResponse(placement_json(row))
        # ?purge=true = UNPLACE: forget the row, ask the edge for nothing.
        # Reclaim and unplace are different intents that were sharing one
        # verb — DELETE meant "destroy the substrate", so the only way to stop
        # scheduling a seat was to demolish the agent behind it (for a
        # worktree seat, `squad rm`). A row written against a real roster
        # agent could therefore never be tidied away (2026-08-09).
        #
        # Nothing is asked of the edge because nothing needs to be: the row is
        # the whole of what the hub contributes, and `plan()` only ever acts
        # on placements it is served. A deleted row is served to nobody, so
        # the machine simply stops hearing about that seat — the substrate
        # keeps whatever state it was in, which is precisely the point.
        if request.query_params.get("purge") == "true":
            db().execute("DELETE FROM api_placements WHERE id = ?", (pid,))
            db().commit()
            # Rung even though the machine has no work to do: the row it was
            # reconciling is gone, and a spurious pull is idempotent + cheap.
            notify_machine(row["machine"])
            return JSONResponse({"id": pid, "purged": True})
        # DELETE = reclaim request: harvest + verify + destroy, each pending
        # until an edge executes and reports it. Never marked done here — the
        # hub asserting completion would be the exact self-assertion the
        # evidence contract forbids.
        reclaim = {"harvest": "pending", "verify": "pending", "destroy": "pending"}
        db().execute(
            "UPDATE api_placements SET desired = 'reclaimed', reclaim = ?"
            " WHERE id = ?",
            (json.dumps(reclaim), pid),
        )
        db().commit()
        notify_machine(row["machine"])
        return JSONResponse({"id": pid, "reclaim": reclaim}, status_code=202)

    # 🔴 NO DOORBELL BELOW THIS LINE, AND IT IS NOT AN OVERSIGHT.
    #
    # This endpoint and the reclaim-phase update inside it are the machine
    # REPORTING to the hub. Ringing the bell on a report would be a feedback
    # loop with no brake: edge reports observed -> hub rings -> edge pulls and
    # reconciles -> reports again -> rings again, as fast as the network
    # allows. The doorbell exists for changes the OPERATOR makes; a machine
    # never needs telling about its own news.
    @route("/api/v1/placements/{pid}/observed", methods=["POST"])
    async def placement_observed(request: Request) -> Response:
        got = auth(request)
        if isinstance(got, JSONResponse):
            return got
        principal, mname = got
        pid = request.path_params["pid"]
        row = db().execute(
            "SELECT * FROM api_placements WHERE id = ?", (pid,)
        ).fetchone()
        if not row:
            return _err(404, f"no placement '{pid}'")
        if principal == "machine" and mname != row["machine"]:
            return _err(403, "machine token may only report its own placements")
        body = await body_of(request)
        state = body.get("state", "")
        db().execute(
            "UPDATE api_placements SET observed_state = ?, observed_at = ?,"
            " observed_enum = ? WHERE id = ?",
            (
                state,
                _now(),
                json.dumps(body.get("enumeration", {})),
                pid,
            ),
        )
        # A reclaim REPORTED COMPLETE is complete. The three steps were written
        # `pending` when the DELETE arrived and nothing ever wrote them again,
        # so a finished reclaim went on describing itself as unstarted forever
        # — `mcp-hub placements list` showed two seats mid-harvest whose
        # containers had not existed for a day (2026-08-06).
        #
        # Only the edge's own `reclaimed` verdict flips them, and that verdict
        # is built from ABSENCE it actually enumerated (edge.observed_report) —
        # never from `desired`, so this cannot mark itself done by wanting to.
        if state == "reclaimed" and row["reclaim"]:
            db().execute(
                "UPDATE api_placements SET reclaim = ? WHERE id = ?",
                (json.dumps({"harvest": "done", "verify": "done",
                             "destroy": "done"}), pid),
            )
        db().commit()
        row = db().execute(
            "SELECT * FROM api_placements WHERE id = ?", (pid,)
        ).fetchone()
        return JSONResponse(placement_json(row))
