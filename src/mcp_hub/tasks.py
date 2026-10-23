"""Hub tasks — a task is its own record, never a kind of message (v2-fleet r6/r7).

A message is something SAID; a task is something TO BE DONE. The difference is
not cosmetic, and it is why this is a new store rather than a flag on
`messages`: a task has one requester, at most one owner at a time, a state, an
end, and its own identity (`hub.task/1?id=N`). None of those five things fits
on a row whose whole contract is "somebody said this, once".

Two tables, and the split between them is the point:

- `tasks` is the CURRENT truth — one row per task, mutated by transitions.
- `task_events` is APPEND-ONLY — every act anyone took on the task, won or
  refused, with the actor and the attribution grade it was taken under. The
  shape is modelled on `api_v1.api_seat_actions`: a declaration and its
  recorded outcome, never one row that quietly becomes the other.

The binding rules this module exists to enforce (mcp-hub's proposal, r6):

- **State changes only by a recorded transition from a named actor.** Every
  write here appends an event, so there is no path from one state to another
  that leaves no trace of who moved it.
- **The hub never infers "done" from text.** `resolve_status` already refuses
  to read completion off an authored document, for the reason that a store
  which copies the claim agrees with the claim exactly when it is wrong. The
  same rule, one layer down: a DM saying "done ✅" moves nothing. Only
  `task_done`, by the owner, does — and what it records is the owner's CLAIM,
  carrying the owner's grade, not the hub's verdict.
- **Nothing is delivered by typing into a pane.** A transition writes a row
  and a pointer DM. No process is launched, no keystrokes are sent anywhere.
- **Task notices are normal priority, never urgent.** The record is the truth
  and it is readable on demand; a pointer at it has not earned an interrupt.

TEXT IS DATA, twice over. Every operator- or agent-supplied string (`title`,
`body`, a `result`, a `reason`, a note) goes through `spec_guard.scan_secret`
before it is stored, because this store is the same plaintext SQLite as
everything else — and it is rendered through `_render_body`, under a header
that says it is data, because a task body containing a header-shaped line
would otherwise forge a message in any drain that read it back.

r7 completes the life r6 began (open → claimed → done):

- the OWNER hands work back (`release`) or says it cannot be done (`failed`);
- the REQUESTER agrees it is finished (`accept`), stops it (`cancel`) or puts
  it on somebody else's desk (`reassign`);
- anyone with standing — the requester or the current owner — records a
  `note`, which moves NOTHING and tells nobody;
- terminal is `accepted`, `failed`, `cancelled`. `done` is still only the
  owner's claim and waits for the requester: `list_tasks(requester=me,
  state=done)` is the to-accept list, and nothing auto-accepts.

THE LOCK RULE (F18), carried from r6's claim race to every act and held by one
helper, `_transition`: everything that may COMMIT — squad membership, the key
mode, a note's blocker check — happens BEFORE `BEGIN IMMEDIATE`; inside the
transaction only `tasks`, `task_events` and the notice insert are touched;
lineage is written only AFTER the COMMIT. A commit inside the transaction
drops the write lock, and two actors then take the same event number.

Deliberately NOT here: `deadline_at`, `lease_seconds` and `lease_until`. They
arrive in r8 with the clocks that enforce them. Until then a non-empty
`deadline` or `lease` argument is REFUSED rather than stored, because a stored
deadline nothing sweeps is a promise the record makes and no code keeps — the
"declaration nothing enforces" shape that has already cost this fleet three
seats' memory.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from mcp_hub import identity, lineage, refs, spec_guard
from mcp_hub.linebreaks import to_newlines

logger = logging.getLogger(__name__)

TASK_SCHEME = "hub.task/1"
AGENT_SCHEME = "hub.agent/1"
SQUAD_SCHEME = "hub.squad/1"

# The lifecycle. r6 stopped at `done` so that "the owner says it is finished"
# and "the requester agrees" could not collapse into one word; r7 adds the
# requester's half and the two ways work ends without being finished.
OPEN = "open"
CLAIMED = "claimed"
DONE = "done"
ACCEPTED = "accepted"
FAILED = "failed"
CANCELLED = "cancelled"
STATES = (OPEN, CLAIMED, DONE, ACCEPTED, FAILED, CANCELLED)
# Terminal means terminal: every act on one of these is refused and recorded.
# A task that ended badly is still a record of what happened, not a draft.
TERMINAL = (ACCEPTED, FAILED, CANCELLED)

ACT_CREATE = "create"
ACT_CLAIM = "claim"
ACT_DONE = "done"
ACT_RELEASE = "release"
ACT_FAILED = "failed"
ACT_ACCEPT = "accept"
ACT_CANCEL = "cancel"
ACT_REASSIGN = "reassign"
ACT_NOTE = "note"

# Which states each act may be taken from.
_ALLOWED_FROM = {
    ACT_CLAIM: (OPEN,),
    ACT_DONE: (CLAIMED,),
    ACT_RELEASE: (CLAIMED,),
    ACT_FAILED: (CLAIMED,),
    ACT_ACCEPT: (DONE,),
    ACT_CANCEL: (OPEN, CLAIMED),
    ACT_REASSIGN: (OPEN, CLAIMED),
    ACT_NOTE: (OPEN, CLAIMED, DONE),
}

# Every act except a note MOVES the task: it bumps the version, so a claimer's
# stale version stays stale after a release or a reassign. A note moves
# nothing at all, which is why `updated_ts` is read off the last MOVING act
# and not off the last event (r7's declared change: a note — and a refusal,
# which was always a non-event for the row — no longer touches it).
MOVING_ACTS = (
    ACT_CREATE, ACT_CLAIM, ACT_DONE, ACT_RELEASE, ACT_FAILED, ACT_ACCEPT,
    ACT_CANCEL, ACT_REASSIGN,
)

# r6's refusal convention, as b-89 built it: one event, act "<act>-refused",
# from_state = to_state, and a non-empty reason. Kept as names because two
# call sites already use them.
ACT_CLAIM_REFUSED = "claim-refused"
ACT_DONE_REFUSED = "done-refused"

OPERATOR_VERIFIED = "operator-verified"

# The acts an OWNER takes (the claimer is the prospective owner). Under
# MCP_HUB_AGENT_KEYS=enforce these need a verified grade on an operator's
# task; a note is an owner act only when the noter is the owner.
_OWNER_ACTS = (ACT_CLAIM, ACT_DONE, ACT_FAILED, ACT_RELEASE, ACT_NOTE)

# `get_task(format="json")` is an explicit ALLOW-LIST (mcp-hub 36509), never
# the stored row. A column added to `tasks` later — r8's lease is the known
# one — is not interface until it is NAMED here. Dumping the row would make
# every future migration a silent interface change, and a consumer that grew
# to depend on the leak would be broken by the next one.
TASK_JSON_FIELDS = (
    "ref", "state", "version", "title", "body", "result", "requester",
    "requester_grade", "addressed_to", "owner", "owner_grade", "created_ts",
    "updated_ts", "blocked", "blockers", "events",
)
EVENT_JSON_FIELDS = (
    "seq", "ts", "act", "from_state", "to_state", "actor", "grade", "reason",
    "notice", "key_note",
)
# The cockpit's list row (GET /api/v1/tasks). Exactly these: a summary that
# grew by accident would become interface the same way a dumped row does.
SUMMARY_FIELDS = (
    "ref", "state", "owner", "requester", "addressed_to", "title",
    "updated_ts", "blocked",
)

# Named columns on every read, for the same reason the json is an allow-list.
_TASK_COLUMNS = (
    "id", "title", "body", "requester", "addressed_to", "owner", "state",
    "created_at", "last_notice_msg_id", "version",
)
_TASK_SELECT = ", ".join(_TASK_COLUMNS)
_EVENT_COLUMNS = (
    "task_id", "seq", "act", "from_state", "to_state", "actor", "grade",
    "reason", "ts", "notice_msg_id", "key_note",
)
_EVENT_SELECT = ", ".join(_EVENT_COLUMNS)

# The header that says the lines under it are DATA, not hub voice.
DATA_HEADER = "task text (data)"

# Every line of task text is rendered behind this MARK: four spaces, a bar,
# two spaces. No hub-authored line in a task render contains a bar, and none
# starts at the column the mark ends at, so the hub's own lines stay
# recognisable by their start (mcp-hub finding 1, r8 — see `_data_lines`).
DATA_MARK = "    │  "

# A task id is ASCII 0-9, naming an integer sqlite can hold. Both halves
# matter (mcp-hub finding 5, r8): `str.isdigit()` is true for '²', where
# `int()` then raises ValueError, and for Arabic-Indic '٣', where it
# returns 3 — so a second spelling named task 3. And an id above this ceiling
# raised OverflowError inside sqlite: a tool error, or an HTTP 500, where the
# honest answer is "no task with that id".
MAX_TASK_ID = 2 ** 63 - 1
_ASCII_DIGITS = frozenset("0123456789")


def task_id_from_text(raw: Any) -> int | None:
    """The task id `raw` names, or None when NO task can have it.

    Public because the HTTP read uses it too: one rule decides both surfaces,
    so an id the tool refuses is a 404 there rather than a 500.
    """
    text = str(raw if raw is not None else "")
    if not text or not _ASCII_DIGITS.issuperset(text):
        return None
    value = int(text)
    return value if value <= MAX_TASK_ID else None


def _add_column(
    conn: sqlite3.Connection, statement: str, column: str
) -> None:
    """Run one ADD COLUMN, passing over ONLY "the column is already there".

    The migration has to be idempotent — `ensure_schema` runs at every start,
    on a store that may already carry the column — and r7 got that by
    swallowing every `sqlite3.OperationalError`. It therefore also swallowed
    `database is locked` and every other failure, after which the hub came UP
    on a store with no `key_note` and every task act failed on the missing
    column until somebody restarted it (mcp-hub finding 2, r8).

    So a store that could not be upgraded stops the hub HERE, at start,
    naming the statement that failed. Nothing then runs against a store
    missing the column: a task act that dies on it one turn later looks like
    a bug in the act, and the start that let it happen looks healthy.
    """
    try:
        conn.execute(statement)
    except sqlite3.OperationalError as e:
        if f"duplicate column name: {column}" in str(e).lower():
            return  # already upgraded — the one error that means "done"
        raise RuntimeError(
            f"the task store could not be upgraded: {statement!r} failed with "
            f"{type(e).__name__}: {e}. The hub does not start on a store "
            f"missing {column!r} — every task act would fail on it until a "
            f"restart, and a start that swallowed this reports success for a "
            f"store it knows is broken."
        ) from e


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the two tables. Called from `init_db`, as `evidence.ensure_schema`
    is — the store owns its own schema, so nothing else has to know its shape."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            title              TEXT NOT NULL DEFAULT '',
            body               TEXT NOT NULL DEFAULT '',
            requester          TEXT NOT NULL,
            -- An agent as hub.agent/1?name=X, or a squad as hub.squad/1?name=X.
            -- One column, two kinds of addressee, told apart by their scheme
            -- rather than by a second column nothing keeps in step with it.
            addressed_to       TEXT NOT NULL,
            -- NULL while open, and NULL is the honest value: "" would read as
            -- an owner whose name is blank.
            owner              TEXT,
            state              TEXT NOT NULL DEFAULT 'open',
            created_at         REAL NOT NULL,
            last_notice_msg_id INTEGER,
            -- The compare-and-set handle. Every transition moves it, so a
            -- claim built on a stale read is refused rather than silently
            -- applied to a task that has already moved.
            version            INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            task_id       INTEGER NOT NULL,
            seq           INTEGER NOT NULL,
            act           TEXT NOT NULL,
            from_state    TEXT NOT NULL DEFAULT '',
            to_state      TEXT NOT NULL DEFAULT '',
            actor         TEXT NOT NULL,
            grade         TEXT NOT NULL,
            reason        TEXT NOT NULL DEFAULT '',
            ts            REAL NOT NULL,
            notice_msg_id INTEGER,
            -- The PK is the uniqueness rule, not an index: two acts that
            -- raced to the same event number would make the log lie about
            -- the order things happened in. It cannot be written.
            PRIMARY KEY (task_id, seq)
        )
        """
    )
    # The agent-keys verdict (GG-7, mcp-hub 36836). NULLABLE and HUB-AUTHORED:
    # it is the hub saying what `enforce` would have stopped, which must never
    # be mixed into the actor's own words — a done's `result` stays exactly
    # what the owner passed, so a reader can quote it without quoting us.
    _add_column(
        conn, "ALTER TABLE task_events ADD COLUMN key_note TEXT", "key_note"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks (state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks (owner)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_addressed ON tasks (addressed_to)"
    )
    conn.commit()


@dataclass(frozen=True)
class Hooks:
    """The server-side helpers a task reuses, injected rather than imported.

    `server.py` imports this module, so the arrow cannot point back — and
    re-implementing any of these here is how two renderers of the same thing
    drift apart. `render_body` in particular is a safety rule (every line after
    the first is indented, so a body can never forge a message header): a
    second copy of it is a second place to forget. It is the MESSAGE rule, and
    it is used here for exactly the half that travels — normalising a body's
    newlines — because its indent protects the lines after the first and a
    task render needs the FIRST line protected too (`_data_lines`).

    `check_blocked_by` / `write_blocked_by` are the two halves of server.py's
    `_apply_blocked_by`, split exactly as `lineage.check_declare_blocked` is
    split from `declare_blocked`: the half that can REFUSE runs before
    `BEGIN IMMEDIATE`, the half that WRITES runs after the COMMIT. They are
    optional because a read-only consumer (the HTTP cockpit read) needs
    neither.
    """

    render_body: Callable[[str], str]
    grade_tag: Callable[[str], str]
    msg_ref: Callable[[int], str]
    record_msg_lineage: Callable[..., None]
    check_blocked_by: Callable[..., str] | None = None
    write_blocked_by: Callable[..., str] | None = None


# ---------------------------------------------------------------------------
# Identity — a task's ref, and who a task is addressed to
# ---------------------------------------------------------------------------


def task_ref(task_id: int) -> str:
    return refs.canonical(refs.make_ref(TASK_SCHEME, id=int(task_id)))


def _agent_ref(name: str) -> str:
    return refs.canonical(refs.make_ref(AGENT_SCHEME, name=name))


def _parse_task_id(ref: str) -> tuple[int, str]:
    """(task_id, refusal) for a `hub.task/1?id=N` ref."""
    try:
        parsed = refs.parse_ref(ref)
    except refs.RefError as e:
        return 0, (
            f"REFUSED: {e} — a task is addressed by its own ref, e.g. "
            f"{TASK_SCHEME}?id=7 (the reply to create_task carries it)."
        )
    if parsed.scheme != TASK_SCHEME:
        return 0, (
            f"REFUSED: ref={ref!r} is a {parsed.scheme} ref. A task is not a "
            f"kind of message: address it by {TASK_SCHEME}?id=N."
        )
    task_id = task_id_from_text(parsed.get("id"))
    if task_id is None:
        return 0, (
            f"REFUSED: ref={ref!r} has an id no task can have — a task id is "
            f"ASCII digits 0-9 naming an integer up to {MAX_TASK_ID}. An id "
            f"outside that is REFUSED like any other bad argument, never "
            f"raised: an error where 'no such task' belongs tells a caller "
            f"the hub broke when all they did was mistype."
        )
    return task_id, ""


def parse_addressee(to: str) -> tuple[str, str, str]:
    """(kind, name, refusal) for `create_task`'s `to`.

    An agent is named bare ("bob"); a squad is a REF
    (`hub.squad/1?name=crew`, mcp-hub 36509). Requiring the ref for the squad
    case is what keeps "one agent" and "whoever is in crew" from being told
    apart by guessing at a bare string — an agent and a squad can share a
    name, and resolving that by precedence puts the task on the wrong desk.
    """
    text = (to or "").strip()
    if not text:
        return "", "", (
            "REFUSED: `to` is empty — a task has to be addressed to somebody. "
            f"Name one agent, or a squad as {SQUAD_SCHEME}?name=<squad>. "
            "Nothing was stored."
        )
    if "?" in text or "/" in text:
        try:
            parsed = refs.parse_ref(text)
        except refs.RefError as e:
            return "", "", f"REFUSED: to={to!r} is not a usable ref: {e}"
        if parsed.scheme == SQUAD_SCHEME:
            return "squad", parsed.get("name"), ""
        if parsed.scheme == AGENT_SCHEME:
            return "agent", parsed.get("name"), ""
        return "", "", (
            f"REFUSED: to={to!r} names a {parsed.scheme}. A task is addressed "
            f"to one agent, or to a squad as {SQUAD_SCHEME}?name=<squad>."
        )
    return "agent", text, ""


def _addressed_ref(kind: str, name: str) -> str:
    scheme = SQUAD_SCHEME if kind == "squad" else AGENT_SCHEME
    return refs.canonical(refs.make_ref(scheme, name=name))


def _whom(addressed_to: str) -> str:
    kind, name, _ = parse_addressee(addressed_to)
    return f"squad {name}" if kind == "squad" else name


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _row(conn: sqlite3.Connection, task_id: int) -> Any:
    return conn.execute(
        f"SELECT {_TASK_SELECT} FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


def _event_rows(conn: sqlite3.Connection, task_id: int) -> list[Any]:
    return conn.execute(
        f"SELECT {_EVENT_SELECT} FROM task_events WHERE task_id = ? ORDER BY seq",
        (task_id,),
    ).fetchall()


def _last(events: list[Any], act: str) -> Any:
    for e in reversed(events):
        if e["act"] == act:
            return e
    return None


def _no_such_task(task_id: int) -> str:
    return (
        f"REFUSED: no task {task_ref(task_id)} on this hub. A ref is an "
        "IDENTITY, and an identity that resolves to nothing is not a task "
        "whose state is unknown — it is not a task."
    )


# ---------------------------------------------------------------------------
# Writes — every one of them appends an event
# ---------------------------------------------------------------------------


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """Take the write lock UP FRONT.

    Why IMMEDIATE and not the implicit deferred transaction: a claim reads the
    state, decides, and writes based on what it read. A deferred transaction
    takes its write lock at the first write, so two claimers both read
    `state='open'` and both proceed — and under SQLite one of them then dies
    with `database is locked` instead of being told it lost. IMMEDIATE makes
    the decision and the write one serialized step, so losing a race produces
    a RECORDED refusal naming the winner.
    """
    if conn.in_transaction:
        # Every other writer in this codebase commits before it returns, so an
        # open transaction here is somebody else's unfinished business; flush it
        # rather than fail on "cannot start a transaction within a transaction".
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _append_event(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    act: str,
    from_state: str,
    to_state: str,
    actor: str,
    grade: str,
    reason: str = "",
    ts: float | None = None,
    key_note: str | None = None,
) -> int:
    """Append one event and return its seq. Caller holds the write lock."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS top FROM task_events WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    seq = int(row["top"]) + 1
    conn.execute(
        "INSERT INTO task_events (task_id, seq, act, from_state, to_state,"
        " actor, grade, reason, ts, notice_msg_id, key_note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (task_id, seq, act, from_state, to_state, actor, grade, reason,
         time.time() if ts is None else ts, key_note),
    )
    return seq


def _notice(
    conn: sqlite3.Connection, task_id: int, to: str, body: str
) -> tuple[int, int]:
    """Insert the hub-authored pointer DM for a transition, INSIDE the caller's
    transaction, and return (notice id, the previous notice's id).

    Same insert shape as every other hub-authored DM (`server.py`'s wake-dead
    guidance): from 'hub', `attribution='hub-authored'`, priority NORMAL. It is
    written here and not through `send()` because `send` is a sender's tool —
    it grades a caller, validates a declared reply and decides about waking.
    A notice has none of those: it is the record pointing at itself.

    Delivery is therefore the existing batch and nothing else: the recipient's
    next natural turn, or the hold sweep within HOLD_MAX_SECONDS. No wake is
    fired from here, and a reply to a notice cannot fast-wake either — the
    reply-wake rule fires only when the target's author is the recipient, and
    a notice's author is the hub.
    """
    if not to:
        return 0, 0
    prev = conn.execute(
        "SELECT last_notice_msg_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    prev_id = int(prev["last_notice_msg_id"] or 0) if prev is not None else 0
    cur = conn.execute(
        "INSERT INTO messages (ts, from_agent, to_agent, body, priority,"
        " attribution) VALUES (?, 'hub', ?, ?, 'normal', 'hub-authored')",
        (time.time(), to, body),
    )
    msg_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE tasks SET last_notice_msg_id = ? WHERE id = ?", (msg_id, task_id)
    )
    return msg_id, prev_id


def _stamp_notice(
    conn: sqlite3.Connection, task_id: int, seq: int, msg_id: int
) -> None:
    conn.execute(
        "UPDATE task_events SET notice_msg_id = ? WHERE task_id = ? AND seq = ?",
        (msg_id, task_id, seq),
    )


def _notice_lineage(
    conn: sqlite3.Connection, hooks: Hooks, msg_id: int, prev_id: int, to: str
) -> None:
    """Record the notice's edges — AFTER the transition's COMMIT.

    `lineage.write_edge` commits, so calling it inside the transition would
    drop the write lock mid-claim and let a second claimer take the same event
    number. That is not hypothetical: it is what the trial's first claim race
    did. The notice chain is declared lineage either way — each notice replies
    to the previous one on the same task, so a requester reading one can walk
    back through the task's whole conversation with itself.
    """
    if not msg_id:
        return
    hooks.record_msg_lineage(
        conn, msg_id, "hub", to_agent=to,
        reply_ref=hooks.msg_ref(prev_id) if prev_id else "",
    )


def _record_task_lineage(
    conn: sqlite3.Connection,
    tref: str,
    *,
    requester: str,
    addressed_to: str,
    assigned_to: str = "",
) -> None:
    """The TASK's own edges — after COMMIT, through `lineage.write_edge` read
    as a module attribute so a test (or a future store) can stand in for it.

    Every act re-asserts authored-by and addressed-to. That is not redundancy:
    `write_edge` is an append-only INSERT OR IGNORE, so a re-assertion costs
    one statement and a swallowed failure HEALS at the next act. An edge is a
    description of the record, and a description that can never catch up with
    what it describes is worse than one written twice.

    A failure here NEVER undoes the transition or its notices: both are already
    committed, and lineage must not be able to reach back and unmake them.
    """
    edges = [("authored-by", _agent_ref(requester))]
    if addressed_to:
        edges.append(("addressed-to", addressed_to))
    if assigned_to:
        edges.append(("assigned-to", _agent_ref(assigned_to)))
    for predicate, obj in edges:
        try:
            lineage.write_edge(conn, tref, predicate, obj, "auto")
        except Exception:  # noqa: BLE001 — see docstring
            logger.warning(
                "task lineage write failed: %s -%s-> %s", tref, predicate, obj,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# blocked — DERIVED at read time, stored nowhere
# ---------------------------------------------------------------------------


def _blockers(conn: sqlite3.Connection, tref: str) -> list[dict]:
    """What this task is declared to be waiting on, and whether each has
    cleared. Read from the lineage edges; NOTHING about `blocked` is stored on
    the task, and no text ever sets it.

    A task blocker clears when it reaches `done` or `accepted` — the two ways
    work finishes. A `failed` or `cancelled` blocker does NOT clear it: the
    work it was waiting on ENDED without being done, which is a question for a
    human, not a release. Reading "unblocked" off a cancelled blocker is how a
    stuck task quietly looks healthy.
    """
    try:
        rows = conn.execute(
            "SELECT object, cleared_at, ts FROM lineage_edges WHERE subject = ?"
            " AND predicate = 'blocked-by' ORDER BY ts, object",
            (tref,),
        ).fetchall()
    except sqlite3.Error:  # no lineage store on this connection
        return []
    out: list[dict] = []
    for r in rows:
        obj = r["object"]
        state = None
        task_id, refusal = _parse_task_id(obj)
        if not refusal:
            blocker = _row(conn, task_id)
            state = blocker["state"] if blocker is not None else None
        out.append({
            "ref": obj,
            # NULL for a non-task object (an external work item, a message):
            # the hub has no lifecycle for it, and "" would read as a state.
            "state": state,
            "cleared": r["cleared_at"] is not None or state in (DONE, ACCEPTED),
        })
    return out


def _is_blocked(blockers: list[dict]) -> bool:
    return any(not b["cleared"] for b in blockers)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    requester: str,
    grade: str,
    to: str,
    title: str,
    body: str,
    deadline: str = "",
    lease: str = "",
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """Make a task. The reply carries its ref."""
    for field, value in (("deadline", deadline), ("lease", lease)):
        if str(value or "").strip():
            return (
                f"REFUSED: {field}={value!r} — r7 keeps no clocks. The "
                f"deadline and lease columns, and the sweep that enforces "
                f"them, arrive in r8; storing {field} now would record a "
                f"promise nothing keeps, which reads exactly like a kept one. "
                f"Re-send without it. Nothing was stored."
            )
    for where, text in (("task title", title), ("task body", body)):
        refusal = spec_guard.scan_secret(str(text or ""), where)
        if refusal:
            return refusal
    if not str(title or "").strip():
        return (
            "REFUSED: a task needs a title — it is what every list and every "
            "notice shows. Nothing was stored."
        )
    # Tim's tasks need Tim's SEAT, never his name (card #269, one layer down).
    # The operator name is claimable by anyone who can type it, so a task
    # carrying the operator's authority is created only at the verified grade.
    if requester in frozenset(operator_senders or ()):
        if grade != OPERATOR_VERIFIED:
            return (
                f"REFUSED: from_agent={requester!r} is an operator name and "
                f"this call graded {grade!r}. A task requested in the "
                f"operator's name carries the operator's authority to whoever "
                f"reads it, so it is created only at grade "
                f"{OPERATOR_VERIFIED} — present the operator token "
                f"(x-mcp-hub-operator-token), or ask under your own name. "
                f"Nothing was stored."
            )
    kind, name, refusal = parse_addressee(to)
    if refusal:
        return refusal
    if kind == "agent" and not conn.execute(
        "SELECT 1 FROM agents WHERE name = ?", (name,)
    ).fetchone():
        return (
            f"REFUSED: to={to!r} names no agent on this hub — a task addressed "
            f"to a name nobody answers is a promise nothing keeps. Address an "
            f"agent that has registered (list_agents), or a squad as "
            f"{SQUAD_SCHEME}?name=<squad>. Nothing was stored."
        )

    # A squad is not a PARTY (mcp-hub 36509): it has no inbox and no single
    # reader, so there is nobody for a create notice to tell. Its members see
    # the task by looking — list_tasks(squad=...).
    notice_to = name if kind == "agent" and name != requester else ""
    now = time.time()
    addressed = _addressed_ref(kind, name)
    _begin_immediate(conn)
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, body, requester, addressed_to, owner,"
            " state, created_at, last_notice_msg_id, version)"
            " VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, 1)",
            (title, body, requester, addressed, OPEN, now),
        )
        task_id = int(cur.lastrowid)
        tref = task_ref(task_id)
        seq = _append_event(
            conn, task_id, act=ACT_CREATE, from_state="", to_state=OPEN,
            actor=requester, grade=grade, ts=now,
        )
        msg_id, prev_id = _notice(
            conn, task_id, notice_to,
            f"task ⟨{tref}⟩ — {requester}{hooks.grade_tag(grade)} addressed a "
            f"task to you. It is a RECORD, not a message: read it with "
            f'get_task(ref="{tref}"), take it with '
            f'claim_task(from_agent="{notice_to}", ref="{tref}", version=1), '
            f'and end it with task_done(from_agent="{notice_to}", '
            f'ref="{tref}", result="..."). Saying "done" in a reply moves '
            f"nothing — the hub never reads a task's state from text.",
        )
        if msg_id:
            _stamp_notice(conn, task_id, seq, msg_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _notice_lineage(conn, hooks, msg_id, prev_id, notice_to)
    _record_task_lineage(
        conn, tref, requester=requester, addressed_to=addressed,
    )
    whom = f"squad {name}" if kind == "squad" else name
    if msg_id:
        pointed = (
            f"notice ⟨{hooks.msg_ref(msg_id)}⟩ queued for {notice_to} at "
            f"normal priority — it rides their next turn and does not wake them"
        )
    elif kind == "squad":
        pointed = (
            "no notice sent: a squad is not a party, so there is nobody to "
            f'tell — its members see it with list_tasks(squad="{name}")'
        )
    else:
        pointed = "no notice sent: you addressed it to yourself"
    return (
        f"Task ⟨{tref}⟩ created: {OPEN}, addressed to {whom}, version 1. "
        f"{pointed}."
    )


# ---------------------------------------------------------------------------
# The checks — who may act, from which state, under which grade
# ---------------------------------------------------------------------------


def _may_claim(row: Any, actor: str, member_squads: list[str]) -> tuple[str, str]:
    """(refusal, short reason) — may `actor` take this task at all?

    Squad membership is read as it is NOW, not as it was when the task was
    made: a member who left cannot claim, and a joiner can. Muted members may
    claim (mcp-hub 36482) — mute is about what wakes you, never about what you
    are a member of, and a muted lane that reads the board is as entitled to
    the work as a loud one.
    """
    tref = task_ref(row["id"])
    kind, name, _ = parse_addressee(row["addressed_to"])
    if kind == "agent":
        if actor == name:
            return "", ""
        return (
            f"REFUSED: {tref} is addressed to {name}, not {actor}. Ownership "
            f"moves only by an explicit act from the agent it was addressed "
            f"to — there is no auto-assign, and nobody claims on another "
            f"agent's behalf. Your attempt was recorded.",
            f"not the addressee ({name} is)",
        )
    if name in member_squads:
        return "", ""
    return (
        f"REFUSED: {tref} is addressed to squad {name}, and {actor} is not a "
        f"current member of it. Membership is read now, not when the task was "
        f"made: a former member cannot claim, a muted member can. Your "
        f"attempt was recorded.",
        f"not a current member of {name}",
    )


def _state_refusal(act: str, row: Any) -> tuple[str, str]:
    """(refusal, short reason) when the act does not act on this state.

    The refusal NAMES THE STATE IT FOUND, always: the loser of a race reads
    this reply, and "refused" without the new state leaves them guessing at
    what happened — the one thing a record exists to stop.
    """
    tref = task_ref(row["id"])
    state = row["state"]
    ended = (
        f" It is terminal: a {state} task keeps the shape it ended in."
        if state in TERMINAL else ""
    )
    why = {
        ACT_DONE: (
            f"REFUSED: {tref} is {state}, not {CLAIMED} — done is the OWNER's "
            f"claim, and a task nobody has claimed has no owner to make it. "
            f"Claim it first."
        ),
        ACT_RELEASE: (
            f"REFUSED: {tref} is {state}, not {CLAIMED} — release hands back "
            f"work you hold, and nothing is held here."
        ),
        ACT_FAILED: (
            f"REFUSED: {tref} is {state}, not {CLAIMED} — failed is the "
            f"OWNER's report on work they hold."
        ),
        ACT_ACCEPT: (
            f"REFUSED: {tref} is {state}, not {DONE} — accept is the "
            f"requester's agreement with the owner's done, and there is no "
            f"done to agree with."
        ),
        ACT_CANCEL: (
            f"REFUSED: {tref} is {state} — cancel stops an {OPEN} or "
            f"{CLAIMED} task.{ended}"
        ),
        ACT_REASSIGN: (
            f"REFUSED: {tref} is {state} — reassign moves an {OPEN} or "
            f"{CLAIMED} task to another desk.{ended}"
        ),
        ACT_NOTE: (
            f"REFUSED: {tref} is {state} — a note is recorded on a live task "
            f"({OPEN}, {CLAIMED} or {DONE}); an ended task's log is "
            f"closed.{ended}"
        ),
    }[act]
    return f"{why} Your attempt was recorded.", f"task is {state}"


def _role_refusal(act: str, row: Any, actor: str) -> tuple[str, str]:
    """(refusal, short reason) — "" when `actor` has the standing for `act`."""
    tref = task_ref(row["id"])
    requester, owner = row["requester"], row["owner"]
    if act in (ACT_DONE, ACT_RELEASE, ACT_FAILED):
        if owner == actor:
            return "", ""
        verb = {
            ACT_DONE: "Done is the owner's recorded claim",
            ACT_RELEASE: "Only the owner gives work back",
            ACT_FAILED: "Only the owner reports that it cannot be done",
        }[act]
        tail = (
            " Done is the owner's recorded claim: a requester who thinks it "
            f"is finished says so to {owner}, and accept is where their "
            "agreement gets its own transition."
            if act == ACT_DONE else
            f" {verb}: what {requester} wants stopped or moved is cancel and "
            "reassign, which are theirs."
        )
        return (
            f"REFUSED: {tref} is owned by {owner}, not {actor}.{tail} Your "
            f"attempt was recorded.",
            f"not the owner ({owner} is)",
        )
    if act == ACT_ACCEPT:
        if requester == actor:
            return "", ""
        return (
            f"REFUSED: {tref} was requested by {requester}, not {actor} — "
            f"accept is the REQUESTER's agreement that the work is finished, "
            f"and it has NO operator override: nobody signs off somebody "
            f"else's work for them, because a sign-off that anyone can make "
            f"says nothing. Your attempt was recorded.",
            f"not the requester ({requester} is)",
        )
    if act == ACT_NOTE:
        if actor in (requester, owner):
            return "", ""
        held = owner if owner else "nobody owns it yet"
        return (
            f"REFUSED: a note on {tref} is the requester's ({requester}) or "
            f"the current owner's ({held}), not {actor}'s — the log is the "
            f"task's own record, not a comment thread. Your attempt was "
            f"recorded.",
            f"neither requester nor owner ({requester}/{held})",
        )
    # cancel and reassign: the requester, or the operator at a VERIFIED grade
    # (the caller has already let a verified operator past this check).
    if requester == actor:
        return "", ""
    return (
        f"REFUSED: {tref} was requested by {requester}, not {actor} — "
        f"{act} belongs to the requester, or to the operator at grade "
        f"{OPERATOR_VERIFIED} (the operator NAME is not authority: anyone can "
        f"type it). Your attempt was recorded.",
        f"not the requester ({requester} is) and not a verified operator",
    )


def _authority(
    act: str, row: Any, actor: str, grade: str, ops: frozenset, key_mode: str,
) -> tuple[str, str, str | None]:
    """(refusal, short reason, key_note) for role, operator grade and keys.

    Three gates, in the order a reader would ask them: may this actor act at
    all; if they are acting AS the operator, have they proved it; and if they
    are acting as the OWNER of an operator's task, does the key mode have
    anything to say.
    """
    tref = task_ref(row["id"])
    requester, owner = row["requester"], row["owner"]
    is_operator_task = requester in ops

    # 1. Standing. For cancel/reassign a verified operator stands in for the
    #    requester — and ONLY a verified one. A claim's standing is the
    #    addressee rule, already decided by `_may_claim`.
    override = (
        act in (ACT_CANCEL, ACT_REASSIGN)
        and actor in ops
        and grade == OPERATOR_VERIFIED
    )
    if not override and act != ACT_CLAIM:
        refusal, short = _role_refusal(act, row, actor)
        if refusal:
            return refusal, short, None

    # 2. The requester's own acts on an operator's task need the grade too —
    #    the name alone is refused, and recorded as `asserted` (audit
    #    2026-10-13 H1/H2: a comparison against a TYPED name is not a check).
    acting_as_requester = actor == requester and not override
    if is_operator_task and acting_as_requester and grade != OPERATOR_VERIFIED:
        return (
            f"REFUSED: {tref} was requested by {requester}, an operator name, "
            f"and this hub verifies its operator: {act} on it needs grade "
            f"{OPERATOR_VERIFIED} and this call graded {grade!r}. Present the "
            f"operator token (x-mcp-hub-operator-token). Your attempt was "
            f"recorded.",
            f"operator name at grade {grade}, not {OPERATOR_VERIFIED}",
            None,
        )

    # 3. Agent keys (GG-7), read per act. Only an OWNER's act on an OPERATOR's
    #    task is gated: agent-to-agent tasks keep r6's grades in every mode,
    #    and tightening those is a later, declared change. `VERIFIED_GRADES` is
    #    read off the module on every call — one verified set, no local copy
    #    that can drift when key-verified joins it.
    owner_act = act in _OWNER_ACTS and (
        act == ACT_CLAIM or actor == owner
    )
    if (key_mode == "off" or not owner_act or not is_operator_task
            or grade in identity.VERIFIED_GRADES):
        return "", "", None
    verified = ", ".join(identity.VERIFIED_GRADES)
    if key_mode == "enforce":
        return (
            f"REFUSED: {tref} was requested by {requester} (an operator's "
            f"task) and MCP_HUB_AGENT_KEYS=enforce: an owner's {act} on it "
            f"needs a verified grade ({verified}), and this call graded "
            f"{grade!r}. Run `mcp-hub agent-key ensure` on the machine that "
            f"owns the name, or act from the session you registered. Your "
            f"attempt was recorded.",
            f"unverified owner act under keys=enforce (grade {grade})",
            f"refused: keys=enforce — {act} by {actor} at grade {grade} on an "
            f"operator task",
        )
    # observe: the act PASSES and the ledger says what enforce would stop. The
    # verdict is the hub's, so it rides key_note and never the reason — a
    # done's result has to stay exactly the owner's text (mcp-hub 36836).
    return "", "", (
        f"would-refuse: keys=enforce would refuse this {act} — an owner act "
        f"on an operator task at grade {grade}, not one of {verified}"
    )


def _check_blocker_declaration(
    conn: sqlite3.Connection,
    hooks: Hooks,
    row: Any,
    actor: str,
    grade: str,
    blocked_by: str,
) -> str:
    """"" or a refusal — run BEFORE `BEGIN IMMEDIATE`, because it reads the
    lineage store and can refuse, and a check that can refuse has no business
    holding a write lock.

    A blocker is the REQUESTER's declaration at a verified grade: lineage's
    own author rule says the lane that owns the blocked work declares it
    blocked, and a task's owning lane is the one that asked for it. A failed
    check refuses the WHOLE note — half a note, with the blocker silently
    dropped, is the path view lying by omission.
    """
    tref = task_ref(row["id"])
    if actor != row["requester"]:
        return (
            f"REFUSED: a blocker on {tref} is the REQUESTER's declaration "
            f"({row['requester']}'s), not {actor}'s — an owner who is stuck "
            f"says so in the note's text, and the requester decides what the "
            f"work waits on. Nothing was noted."
        )
    spec = blocked_by
    if spec.startswith("clear:"):
        spec = spec[len("clear:"):]
    if "|" not in spec:
        return (
            "REFUSED: blocked_by format is '<task-ref>|<object-ref>' (or "
            "'clear:<task-ref>|<object-ref>') — the subject is the work that "
            "is blocked, the object what it waits on. Nothing was noted."
        )
    subject = spec.split("|", 1)[0].strip()
    try:
        if refs.canonical(refs.parse_ref(subject)) != tref:
            return (
                f"REFUSED: blocked_by's subject {subject!r} is not {tref} — a "
                f"note declares what THIS task waits on. Nothing was noted."
            )
    except refs.RefError as e:
        return f"REFUSED: blocked_by subject: {e}. Nothing was noted."
    if hooks.check_blocked_by is None:
        return (
            "REFUSED: this hub cannot check a blocker declaration here. "
            "Nothing was noted."
        )
    why = hooks.check_blocked_by(conn, actor, blocked_by, grade)
    if why:
        return f"REFUSED: {why}. Nothing was noted."
    return ""


# ---------------------------------------------------------------------------
# _transition — the one helper that holds THE LOCK RULE
# ---------------------------------------------------------------------------


def _transition(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    act: str,
    actor: str,
    grade: str,
    ref: str,
    reason: str = "",
    to: str = "",
    version: int | None = None,
    member_squads: Any = (),
    operator_senders: Any = (),
    key_mode: str = "off",
    blocked_by: str = "",
) -> str:
    """One act on one task: check it under the write lock, move the row, tell
    whoever the act tells, and record the edges afterwards.

    THE LOCK RULE (F18), and every act obeys it because every act comes
    through here:

      before BEGIN IMMEDIATE — anything that may COMMIT or that can refuse on
        its own (squad membership, read by the caller; the key mode, read by
        the caller; the reassign target's identity; a note's blocker check);
      inside the transaction — `tasks`, `task_events` and the notice insert,
        and nothing else;
      after COMMIT — lineage, always, and never anything the transition's
        outcome depends on.
    """
    task_id, refusal = _parse_task_id(ref)
    if refusal:
        return refusal
    row = _row(conn, task_id)
    if row is None:
        return _no_such_task(task_id)
    tref = task_ref(task_id)
    ops = frozenset(operator_senders or ())
    squads = list(member_squads or ())

    # -- before BEGIN IMMEDIATE ---------------------------------------------
    target_ref = ""
    target_kind = target_name = ""
    target_refusal = target_short = ""
    if act == ACT_REASSIGN:
        # THE LOCK RULE keeps the target's identity HERE, before the write
        # lock: `parse_addressee` and the agents read must not happen inside
        # the transaction. But r7 also ANSWERED here, before the state and
        # role checks that live inside it (mcp-hub finding 3, r8), and that
        # had two costs: a caller who may not reassign the task at all was
        # told whether a name is registered, and the attempt left no event
        # while every other refused act records one. So the refusal is
        # carried down and decided in the transaction's own order — state,
        # then role, then target. The short reason names the FAULT and never
        # the text: `to` is the one caller-supplied string on this path that
        # no secret scan has seen, and a refused target is stored nowhere
        # else. The reply names it, to whoever had the standing to be told.
        target_kind, target_name, target_refusal = parse_addressee(to)
        if target_refusal:
            target_short = "target is not a usable addressee"
        elif target_kind == "agent" and not conn.execute(
            "SELECT 1 FROM agents WHERE name = ?", (target_name,)
        ).fetchone():
            target_refusal = (
                f"REFUSED: to={to!r} names no agent on this hub — moving a "
                f"task to a name nobody answers is a promise nothing keeps. "
                f"Name an agent that has registered (list_agents), or a squad "
                f"as {SQUAD_SCHEME}?name=<squad>. Nothing was changed."
            )
            target_short = "target names no agent on this hub"
        else:
            target_ref = _addressed_ref(target_kind, target_name)
    if blocked_by:
        if act != ACT_NOTE:
            return (
                f"REFUSED: blocked_by rides a note, not a {act} — declare what "
                f"a task waits on with task_note(..., blocked_by=\"{tref}|"
                f"<object-ref>\")."
            )
        # Scanned BEFORE the blocker check, and before anything is written:
        # a ref's field values take any non-empty text, so a secret placed in
        # either side of '<task-ref>|<object-ref>' was stored in the event's
        # reason, in the lineage edge and in every render (mcp-hub finding 4,
        # r8). The check itself cannot run first — its subject refusal quotes
        # the subject back, which is one of the two sides. One scan over the
        # whole declaration covers both sides and the `clear:` form with it.
        secret = spec_guard.scan_secret(
            str(blocked_by), "blocked_by declaration"
        )
        if secret:
            recorded = "blocker declaration refused: a secret pattern matched"
            return _record_lone_refusal(
                conn, task_id, act=act, actor=actor, grade=grade,
                reason=recorded,
                text=(
                    f"{secret} Nothing was noted, and no blocker edge was "
                    f"written."
                ),
            )
        pre = _check_blocker_declaration(
            conn, hooks, row, actor, grade, blocked_by
        )
        if pre:
            return _record_lone_refusal(
                conn, task_id, act=act, actor=actor, grade=grade,
                reason="blocker declaration refused", text=pre,
            )

    # -- inside the transaction ---------------------------------------------
    sent: list[tuple[int, int, str]] = []
    try:
        _begin_immediate(conn)
        # Re-read under the write lock: the row this decision is made on has
        # to be the row the write lands on, or the check is decoration.
        row = _row(conn, task_id)
        if row is None:
            conn.commit()
            return _no_such_task(task_id)
        state = row["state"]
        refusal, short, key_note = "", "", None

        if act == ACT_CLAIM:
            # r6's order, kept: who you are, then where the task is, then
            # whether you were looking at it when you decided.
            refusal, short = _may_claim(row, actor, squads)
            if not refusal and state != OPEN:
                refusal = (
                    f"REFUSED: {tref} is already {state}, claimed by "
                    f"{row['owner']} — one owner at a time, and you were "
                    f"second. Nothing was taken from {row['owner']}. Your "
                    f"attempt was recorded."
                )
                short = f"already {state} by {row['owner']}"
            if not refusal and int(row["version"]) != int(version or 0):
                refusal = (
                    f"REFUSED: {tref} is at version {row['version']}, and you "
                    f"claimed against version {version}. The task moved after "
                    f"you read it: re-read it with get_task and claim the "
                    f"version you actually saw. Your attempt was recorded."
                )
                short = f"stale version {version} (now {row['version']})"
        elif state not in _ALLOWED_FROM[act]:
            refusal, short = _state_refusal(act, row)
        if not refusal:
            gate, gate_short, key_note = _authority(
                act, row, actor, grade, ops, key_mode
            )
            if gate:
                refusal, short = gate, gate_short
        # Third, and only now: where it was going. An outsider's refusal is
        # the same whatever `to` says.
        if not refusal and target_refusal:
            refusal, short = target_refusal, target_short
        same_open_target = (
            act == ACT_REASSIGN and state == OPEN
            and target_ref == row["addressed_to"]
        )
        if not refusal and same_open_target:
            refusal = (
                f"REFUSED: {tref} is already open and addressed to "
                f"{_whom(target_ref)} — reassigning it there changes nothing, "
                f"and a no-op transition in the log would read as a decision "
                f"somebody made. Your attempt was recorded."
            )
            short = "same open target: no change"
        if not refusal:
            # Text is scanned here, with the lock held, exactly as r6 scans a
            # result: the refusal names only the PATTERN, never the match,
            # including in the recorded reason.
            field = {
                ACT_DONE: "task result", ACT_NOTE: "task note",
            }.get(act, f"{act} reason")
            secret = spec_guard.scan_secret(str(reason or ""), field)
            if secret:
                refusal = secret
                short = f"{field} refused: it matched a secret pattern"
        if refusal:
            _append_event(
                conn, task_id, act=f"{act}-refused", from_state=state,
                to_state=state, actor=actor, grade=grade, reason=short,
                key_note=key_note,
            )
            conn.commit()
            return refusal

        now = time.time()
        requester, owner = row["requester"], row["owner"]
        addressed = row["addressed_to"]
        old_version = int(row["version"])
        new_version = old_version if act == ACT_NOTE else old_version + 1
        tag = hooks.grade_tag(grade)
        notices: list[tuple[str, str]] = []
        assigned_to = ""
        event_reason = str(reason or "")
        to_state = state
        pointer = (
            f"The record is the truth, this only points at it: "
            f'get_task(ref="{tref}").'
        )

        if act == ACT_CLAIM:
            to_state, assigned_to = CLAIMED, actor
            conn.execute(
                "UPDATE tasks SET owner = ?, state = ?, version = ?"
                " WHERE id = ?", (actor, CLAIMED, new_version, task_id),
            )
            event_reason = ""
            notices.append((requester, (
                f"task ⟨{tref}⟩ — claimed by {actor}{tag}; it is now "
                f"{CLAIMED} at version {new_version}. {pointer}"
            )))
            reply = (
                f"Claimed ⟨{tref}⟩ — you own it, {CLAIMED} at version "
                f'{new_version}. End it with task_done(from_agent="{actor}", '
                f'ref="{tref}", result="..."): done is YOUR recorded claim, '
                f"and nothing else makes it."
            )
        elif act == ACT_DONE:
            to_state = DONE
            conn.execute(
                "UPDATE tasks SET state = ?, version = ? WHERE id = ?",
                (DONE, new_version, task_id),
            )
            notices.append((requester, (
                f"task ⟨{tref}⟩ — reported {DONE} by {actor}{tag}. That is "
                f"the owner's recorded claim under their own grade, not the "
                f"hub's verdict: read it, result included, with "
                f'get_task(ref="{tref}"). Nothing auto-accepts — '
                f'accept_task(from_agent="{requester}", ref="{tref}") is '
                f"yours to make."
            )))
            reply = (
                f"Recorded: ⟨{tref}⟩ is {DONE}, claimed by {actor}{tag} at "
                f"version {new_version}. It is not finished with: it waits "
                f"for {requester}'s accept, and stays on their to-accept list "
                f'(list_tasks(requester="{requester}", state="{DONE}")) until '
                f"they make it."
            )
        elif act == ACT_RELEASE:
            to_state = OPEN
            conn.execute(
                "UPDATE tasks SET owner = NULL, state = ?, version = ?"
                " WHERE id = ?", (OPEN, new_version, task_id),
            )
            notices.append((requester, (
                f"task ⟨{tref}⟩ — released by {actor}{tag}: it is {OPEN} "
                f"again at version {new_version}, still addressed to "
                f"{_whom(addressed)}, and nobody owns it. {pointer}"
            )))
            reply = (
                f"Released ⟨{tref}⟩ — it is {OPEN} again at version "
                f"{new_version} and you no longer own it. It is still "
                f"addressed to {_whom(addressed)}, so the next claim can be "
                f"yours or anyone else's who may take it; the version moved, "
                f"so a claim held against the old one is refused."
            )
        elif act == ACT_FAILED:
            to_state = FAILED
            conn.execute(
                "UPDATE tasks SET state = ?, version = ? WHERE id = ?",
                (FAILED, new_version, task_id),
            )
            notices.append((requester, (
                f"task ⟨{tref}⟩ — reported {FAILED} by {actor}{tag}. That is "
                f"the owner's recorded report, not the hub's verdict, and it "
                f"is terminal: a new attempt is a new task. {pointer}"
            )))
            reply = (
                f"Recorded: ⟨{tref}⟩ is {FAILED}, reported by {actor}{tag} at "
                f"version {new_version}. {FAILED} is terminal — nothing moves "
                f"it again, and a fresh attempt is a fresh task, so what went "
                f"wrong stays readable."
            )
        elif act == ACT_ACCEPT:
            to_state = ACCEPTED
            conn.execute(
                "UPDATE tasks SET state = ?, version = ? WHERE id = ?",
                (ACCEPTED, new_version, task_id),
            )
            notices.append((owner, (
                f"task ⟨{tref}⟩ — {ACCEPTED} by {actor}{tag}: the requester "
                f"agrees the work is finished. {pointer}"
            )))
            reply = (
                f"Accepted ⟨{tref}⟩ — {ACCEPTED} at version {new_version}. "
                f"Your agreement is its own recorded transition, separate "
                f"from {owner}'s done: the log keeps both, and who said which."
            )
        elif act == ACT_CANCEL:
            to_state = CANCELLED
            conn.execute(
                "UPDATE tasks SET state = ?, version = ? WHERE id = ?",
                (CANCELLED, new_version, task_id),
            )
            kind, aname, _ = parse_addressee(addressed)
            # Whoever was actually holding it is the one who has to stop: the
            # owner if it was claimed, the addressee if it was only offered.
            # A squad has no inbox, so there is nobody to tell.
            told = owner if state == CLAIMED else (
                aname if kind == "agent" else ""
            )
            notices.append((told, (
                f"task ⟨{tref}⟩ — {CANCELLED} by {actor}{tag}; it was {state}. "
                f"Stop work on it: it is terminal and nothing moves it again. "
                f"{pointer}"
            )))
            reply = (
                f"Cancelled ⟨{tref}⟩ — {CANCELLED} at version {new_version}, "
                f"from {state}. It is terminal: the record keeps what was "
                f"done up to here, and nothing reopens it."
            )
        elif act == ACT_REASSIGN:
            to_state = OPEN
            addressed = target_ref
            conn.execute(
                "UPDATE tasks SET owner = NULL, addressed_to = ?, state = ?,"
                " version = ? WHERE id = ?",
                (target_ref, OPEN, new_version, task_id),
            )
            if state == CLAIMED:
                notices.append((owner, (
                    f"task ⟨{tref}⟩ — reassigned by {actor}{tag} to "
                    f"{_whom(target_ref)}: it is {OPEN} at version "
                    f"{new_version} and you no longer own it. {pointer}"
                )))
            if target_kind == "agent":
                notices.append((target_name, (
                    f"task ⟨{tref}⟩ — {actor}{tag} addressed this task to "
                    f"you. It is {OPEN} at version {new_version}: read it "
                    f'with get_task(ref="{tref}") and take it with '
                    f'claim_task(from_agent="{target_name}", ref="{tref}", '
                    f"version={new_version})."
                )))
            reply = (
                f"Reassigned ⟨{tref}⟩ to {_whom(target_ref)} — {OPEN} at "
                f"version {new_version}, owner cleared. The version moved, so "
                f"a claim held against the old one is refused; the previous "
                f"addressee stays in the log and in the task's lineage."
            )
        else:  # ACT_NOTE — the one act that moves nothing
            if blocked_by:
                event_reason = (
                    f"{event_reason}\nblocked_by: {blocked_by}"
                    if event_reason else f"blocked_by: {blocked_by}"
                )
            reply = (
                f"Noted on ⟨{tref}⟩ — nothing moved: still {state} at version "
                f"{new_version}, same owner, same addressee, and NOBODY was "
                f"told. A note is for the record, not for anyone's attention; "
                f"it is readable with get_task."
            )

        seq = _append_event(
            conn, task_id, act=act, from_state=state, to_state=to_state,
            actor=actor, grade=grade, reason=event_reason, ts=now,
            key_note=key_note,
        )
        for whom, body in notices:
            # Never to oneself: a notice telling you what you just did is
            # noise, and noise is what makes a real notice ignorable.
            if not whom or whom == actor:
                continue
            msg_id, prev_id = _notice(conn, task_id, whom, body)
            if msg_id:
                if not sent:
                    _stamp_notice(conn, task_id, seq, msg_id)
                sent.append((msg_id, prev_id, whom))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # -- after COMMIT --------------------------------------------------------
    for msg_id, prev_id, whom in sent:
        _notice_lineage(conn, hooks, msg_id, prev_id, whom)
    _record_task_lineage(
        conn, tref, requester=requester, addressed_to=addressed,
        assigned_to=assigned_to,
    )
    if blocked_by and hooks.write_blocked_by is not None:
        why = ""
        try:
            why = hooks.write_blocked_by(conn, actor, blocked_by, grade) or ""
        except Exception as e:  # noqa: BLE001 — the note is already committed
            why = str(e)
            logger.warning("blocker write failed for %s", tref, exc_info=True)
        if why:
            # The note STANDS — it is committed, and rolling it back from out
            # here is not available. So the reply says so loudly rather than
            # letting a dropped declaration pass for a recorded one.
            reply += (
                f" ⚠️ note stored; blocker NOT recorded: {why}; re-declare it "
                f"when the lineage store is back."
            )
    return reply


def _record_lone_refusal(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    act: str,
    actor: str,
    grade: str,
    reason: str,
    text: str,
) -> str:
    """Record a refusal decided BEFORE the act's own transaction (a note's
    blocker check), and return it. Every refusal is one event: a refusal the
    log does not carry is a conversation the record cannot reconstruct."""
    _begin_immediate(conn)
    try:
        row = _row(conn, task_id)
        state = row["state"] if row is not None else ""
        _append_event(
            conn, task_id, act=f"{act}-refused", from_state=state,
            to_state=state, actor=actor, grade=grade, reason=reason,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return text


# ---------------------------------------------------------------------------
# The acts — thin, because the rule lives in _transition
# ---------------------------------------------------------------------------


def claim(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    version: int,
    member_squads: list[str],
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """Compare-and-set `open` → `claimed`. One winner; the losers are told who.

    THE TRANSACTION RULE (F18), found by the trial's first claim race and
    confirmed by mcp-hub 36509: `member_squads` is read by the CALLER, before
    this runs, because `_squads_of` purges expired loans and COMMITS.
    """
    return _transition(
        conn, hooks, act=ACT_CLAIM, actor=actor, grade=grade, ref=ref,
        version=version, member_squads=member_squads,
        operator_senders=operator_senders, key_mode=key_mode,
    )


def done(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    result: str = "",
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """The owner's recorded claim that the work is finished. Nobody else's —
    and still not the end: the requester's accept is its own transition."""
    return _transition(
        conn, hooks, act=ACT_DONE, actor=actor, grade=grade, ref=ref,
        reason=result, operator_senders=operator_senders, key_mode=key_mode,
    )


def release(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    reason: str = "",
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """The owner hands the work back: `claimed` → `open`, owner cleared, and
    the task stays addressed where it was."""
    return _transition(
        conn, hooks, act=ACT_RELEASE, actor=actor, grade=grade, ref=ref,
        reason=reason, operator_senders=operator_senders, key_mode=key_mode,
    )


def task_failed(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    reason: str = "",
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """The owner reports that it cannot be done: `claimed` → `failed`."""
    return _transition(
        conn, hooks, act=ACT_FAILED, actor=actor, grade=grade, ref=ref,
        reason=reason, operator_senders=operator_senders, key_mode=key_mode,
    )


def accept(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """The requester agrees: `done` → `accepted`. No operator override."""
    return _transition(
        conn, hooks, act=ACT_ACCEPT, actor=actor, grade=grade, ref=ref,
        operator_senders=operator_senders, key_mode=key_mode,
    )


def cancel(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    reason: str = "",
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """The requester (or a VERIFIED operator) stops it: `open`/`claimed` →
    `cancelled`."""
    return _transition(
        conn, hooks, act=ACT_CANCEL, actor=actor, grade=grade, ref=ref,
        reason=reason, operator_senders=operator_senders, key_mode=key_mode,
    )


def reassign(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    to: str,
    reason: str = "",
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """The requester (or a VERIFIED operator) puts it on another desk:
    `open`/`claimed` → `open`, addressed to `to`, owner cleared."""
    return _transition(
        conn, hooks, act=ACT_REASSIGN, actor=actor, grade=grade, ref=ref,
        to=to, reason=reason, operator_senders=operator_senders,
        key_mode=key_mode,
    )


def note(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    text: str = "",
    blocked_by: str = "",
    operator_senders: Any = (),
    key_mode: str = "off",
) -> str:
    """Record something on the task's log without moving it. Nothing changes
    — not the state, not the owner, not the version, not `updated_ts` — and
    nobody is told."""
    return _transition(
        conn, hooks, act=ACT_NOTE, actor=actor, grade=grade, ref=ref,
        reason=text, blocked_by=blocked_by,
        operator_senders=operator_senders, key_mode=key_mode,
    )


# ---------------------------------------------------------------------------
# Reads — anyone may look, without asking anyone
# ---------------------------------------------------------------------------


def _event_view(hooks: Hooks, e: Any) -> dict:
    return {
        "seq": int(e["seq"]),
        "ts": float(e["ts"]),
        "act": e["act"],
        "from_state": e["from_state"],
        "to_state": e["to_state"],
        "actor": e["actor"],
        "grade": e["grade"],
        "reason": e["reason"],
        "notice": hooks.msg_ref(int(e["notice_msg_id"]))
        if e["notice_msg_id"] else None,
        # Hub-authored, and kept apart from `reason` on purpose: the actor's
        # words are the actor's, and the hub's verdict about the grade they
        # acted under is the hub's.
        "key_note": e["key_note"],
    }


def _updated_ts(row: Any, events: list[Any]) -> float:
    """The time of the task's last MOVING act.

    r6 took the last event, refusals included, so a refused claim or a note
    aged the task — a timestamp that says "something happened here" when
    nothing moved is an instrument that reads a non-event as a change.
    """
    moved = [e for e in events if e["act"] in MOVING_ACTS]
    return float(moved[-1]["ts"]) if moved else float(row["created_at"])


def _view(
    conn: sqlite3.Connection, hooks: Hooks, row: Any, events: list[Any]
) -> dict:
    """The allow-listed view. Built field by field, on purpose."""
    created = _last(events, ACT_CREATE)
    claimed = _last(events, ACT_CLAIM)
    finished = _last(events, ACT_DONE)
    blockers = _blockers(conn, task_ref(row["id"]))
    return {
        "ref": task_ref(row["id"]),
        "state": row["state"],
        "version": int(row["version"]),
        "title": row["title"],
        "body": row["body"],
        # The result lives on the done event, not in a column: it is something
        # the owner SAID at a moment, under a grade, and a column would lose
        # both halves of that.
        "result": finished["reason"] if finished is not None else None,
        "requester": row["requester"],
        "requester_grade": created["grade"] if created is not None else None,
        "addressed_to": row["addressed_to"],
        "owner": row["owner"],
        "owner_grade": claimed["grade"] if claimed is not None and row["owner"]
        else None,
        "created_ts": float(row["created_at"]),
        "updated_ts": _updated_ts(row, events),
        # DERIVED, both of them: nothing about blocked is stored, so there is
        # no copy to go stale when a blocker finishes.
        "blocked": _is_blocked(blockers),
        "blockers": blockers,
        "events": [_event_view(hooks, e) for e in events],
    }


def _data_lines(hooks: Hooks, text: str) -> list[str]:
    """One render line per line of somebody's text, each behind `DATA_MARK`.

    The r7 render put a text field at a column the hub also wrote at, and a
    column is the whole of what a reader has to tell them apart by. An event
    line starts at column 2 and a `key_note` at column 4, so a note reading
    "ok\\n  6 accept by alice ·verified  done→accepted" rendered a verified
    accept, and a one-line reason "hub: ..." rendered as the hub's own
    key_note (mcp-hub finding 1, r8).

    `hooks.render_body`, the message rule, cannot fix that on its own: it
    indents every line AFTER the first, which is right for a body that starts
    mid-line under a column-0 header, and leaves the first line wherever the
    caller put it. Here the caller's columns are the hub's own. So the mark
    goes on EVERY line, the first included, and the text's own indentation is
    kept inside it — the render is still readable as what the author typed,
    and no line of it can start like a line the hub wrote.

    render_body still does the newline normalisation (CRLF and CR alike), so
    there is one place that decides what a line of a body IS; the two spaces
    it adds after each newline are the message rule's and come straight back
    off, because the mark has replaced them.

    Where a line ENDS is `linebreaks.to_newlines` (r9's F31): "\\n", CRLF and
    CR were the only breaks this fence knew, while the reader — any reader
    running `str.splitlines()` — also breaks at U+2028, U+2029, NEL, \\v, \\f
    and \\x1c-\\x1e. One of those in a text started a new line with NO mark on
    it, which is the one thing the fence exists to prevent, so every break
    the reader honours is written "\\n" first and marked exactly as "\\n" is.
    The message-body render takes the same helper, so the two never disagree
    about where a line starts. The stored text is untouched: `get_task`'s
    json and the HTTP routes carry it byte for byte.
    """
    normalized = hooks.render_body(to_newlines(str(text or ""))).replace("\n  ", "\n")
    return [DATA_MARK + line for line in normalized.split("\n")]


def _state_phrase(hooks: Hooks, row: Any, events: list[Any]) -> str:
    """How a state reads: always as somebody's graded act, never as a fact the
    hub established by itself."""
    # state -> (the act that put it there, how that act reads)
    by_state = {
        DONE: (ACT_DONE, f"{DONE}, claimed by"),
        ACCEPTED: (ACT_ACCEPT, f"{ACCEPTED} by"),
        FAILED: (ACT_FAILED, f"{FAILED}, reported by"),
        CANCELLED: (ACT_CANCEL, f"{CANCELLED} by"),
        CLAIMED: (ACT_CLAIM, f"{CLAIMED} by"),
    }
    act, phrase = by_state.get(row["state"], ("", ""))
    e = _last(events, act) if act else None
    if e is not None:
        return f"{phrase} {e['actor']}{hooks.grade_tag(e['grade'])}"
    return f"{row['state']}, unclaimed"


def _blocked_line(blockers: list[dict]) -> str:
    if not blockers:
        return "  blocked: no (nothing declared)"
    live = [b for b in blockers if not b["cleared"]]
    def one(b: dict) -> str:
        state = b["state"] or "not a task"
        if b["state"] in (FAILED, CANCELLED):
            state = f"{b['state']} — ended without being done"
        return f"⟨{b['ref']}⟩ ({state})"
    if not live:
        return (
            "  blocked: no — every declared blocker has cleared: "
            + ", ".join(one(b) for b in blockers)
        )
    return "  blocked: yes — waiting on " + ", ".join(one(b) for b in live)


def _render_task(
    conn: sqlite3.Connection, hooks: Hooks, row: Any, events: list[Any]
) -> str:
    created = _last(events, ACT_CREATE)
    finished = _last(events, ACT_DONE)
    req_grade = created["grade"] if created is not None else ""
    lines = [
        f"⟨{task_ref(row['id'])}⟩ {_state_phrase(hooks, row, events)}",
        f"  requested by {row['requester']}{hooks.grade_tag(req_grade)} · "
        f"addressed to {row['addressed_to']} · version {row['version']}",
        _blocked_line(_blockers(conn, task_ref(row["id"]))),
        # Everything below this header is somebody's TEXT, every line of it
        # behind the mark, so the hub's own lines stay recognisable by their
        # start. The header says so out loud, and names the mark.
        f"{DATA_HEADER} — every line behind │ is somebody's text, never ours",
        "  title:",
        *_data_lines(hooks, row["title"]),
        "  body:",
        *_data_lines(hooks, row["body"]),
    ]
    if finished is not None:
        lines.append("  result:")
        lines.extend(_data_lines(hooks, finished["reason"]))
    lines.append(f"log ({len(events)} events, append-only)")
    for e in events:
        bits = [
            f"  {e['seq']} {e['act']} by {e['actor']}"
            f"{hooks.grade_tag(e['grade'])}",
            f"{e['from_state'] or '·'}→{e['to_state'] or '·'}",
        ]
        if e["notice_msg_id"]:
            bits.append(f"notice ⟨{hooks.msg_ref(int(e['notice_msg_id']))}⟩")
        lines.append("  ".join(bits))
        # The hub's verdict about the act, at column 4 and in the hub's voice;
        # the actor's own words go behind the mark under it, never at a column
        # this line could be confused with.
        if e["key_note"]:
            lines.append(f"    hub: {e['key_note']}")
        if e["reason"]:
            lines.extend(_data_lines(hooks, e["reason"]))
    return "\n".join(lines)


def view_json(
    conn: sqlite3.Connection, hooks: Hooks, task_id: int
) -> dict | None:
    """get_task's json as a dict, or None when there is no such task. The HTTP
    read calls this, so the cockpit and the tool cannot drift."""
    row = _row(conn, task_id)
    if row is None:
        return None
    return _view(conn, hooks, row, _event_rows(conn, task_id))


def get(
    conn: sqlite3.Connection, hooks: Hooks, ref: str, fmt: str = "text"
) -> str:
    task_id, refusal = _parse_task_id(ref)
    if refusal:
        return refusal
    row = _row(conn, task_id)
    if row is None:
        return _no_such_task(task_id)
    if fmt not in ("text", "json"):
        return (
            f"REFUSED: format={fmt!r} — use 'text' (rendered, task text as "
            "data) or 'json' (the named allow-list of fields)."
        )
    events = _event_rows(conn, task_id)
    if fmt == "json":
        return json.dumps(_view(conn, hooks, row, events), indent=2)
    return _render_task(conn, hooks, row, events)


def addressee_agent(addressed_to: str) -> str:
    """The agent an `addressed_to=` filter names, or '' when it names none
    (empty, a squad ref, or no usable ref). The caller reads that agent's
    squads BEFORE the query, for the lock rule `claim` keeps: the membership
    read purges lapsed loans and commits."""
    text = (addressed_to or "").strip()
    if not text:
        return ""
    kind, name, refusal = parse_addressee(text)
    return name if (kind == "agent" and not refusal) else ""


def member_squads_of(conn: sqlite3.Connection, agent: str) -> list[str]:
    """Every squad `agent` is a member of now, muted included — the squads
    `_may_claim` lets it claim from. Reads only: the caller purges lapsed
    loans first."""
    return sorted(r[0] for r in conn.execute(
        "SELECT squad FROM squad_members WHERE agent = ?", (agent,)
    ).fetchall())


def _filter(
    owner: str = "", requester: str = "", squad: str = "", state: str = "",
    addressed_to: str = "", member_squads: list[str] | tuple[str, ...] = (),
) -> tuple[str, list[Any], str, str]:
    """(sql tail, params, refusal, what-was-filtered) — ONE filter, so the
    HTTP list and the tool cannot disagree about what matches.

    `addressed_to` names an agent (bare, or its ref): it matches tasks
    addressed to that agent AND to every squad in `member_squads`, which the
    caller read for that agent (muted included: mute is about what wakes
    you, never what you may claim). A squad ref matches as `squad=` does."""
    where: list[str] = []
    params: list[Any] = []
    if addressed_to.strip():
        kind, name, refusal = parse_addressee(addressed_to.strip())
        if refusal:
            return "", [], refusal, ""
        if kind == "squad":
            where.append("addressed_to = ?")
            params.append(_addressed_ref("squad", name))
        else:
            wanted = [_addressed_ref("agent", name)] + [
                _addressed_ref("squad", sq) for sq in member_squads]
            where.append(
                "addressed_to IN (" + ", ".join("?" for _ in wanted) + ")")
            params.extend(wanted)
    if owner.strip():
        where.append("owner = ?")
        params.append(owner.strip())
    if requester.strip():
        where.append("requester = ?")
        params.append(requester.strip())
    if squad.strip():
        text = squad.strip()
        name = text
        if "?" in text or "/" in text:
            kind, name, refusal = parse_addressee(text)
            if refusal:
                return "", [], refusal, ""
            if kind != "squad":
                return "", [], (
                    f"REFUSED: squad={squad!r} is not a squad — pass the squad "
                    f"name, or {SQUAD_SCHEME}?name=<squad>."
                ), ""
        where.append("addressed_to = ?")
        params.append(_addressed_ref("squad", name))
    if state.strip():
        if state.strip() not in STATES:
            return "", [], (
                f"REFUSED: state={state!r} is not a task state — one of "
                f"{list(STATES)}."
            ), ""
        where.append("state = ?")
        params.append(state.strip())
    tail = (" WHERE " + " AND ".join(where)) if where else ""
    shown = ", ".join(
        f"{k}={v}" for k, v in (("owner", owner), ("requester", requester),
                                ("squad", squad), ("state", state),
                                ("addressed_to", addressed_to)) if v.strip()
    ) or "no filters"
    return tail, params, "", shown


def summaries(
    conn: sqlite3.Connection,
    *,
    owner: str = "",
    requester: str = "",
    squad: str = "",
    state: str = "",
    addressed_to: str = "",
    member_squads: list[str] | tuple[str, ...] = (),
) -> tuple[list[dict], str]:
    """(rows, refusal) — the cockpit's list, filtered exactly as `list_tasks`.

    Each row is exactly SUMMARY_FIELDS, the same allow-list discipline the
    single-task json keeps: a column added to `tasks` is not interface here
    until somebody names it.
    """
    tail, params, refusal, _shown = _filter(
        owner, requester, squad, state, addressed_to, member_squads)
    if refusal:
        return [], refusal
    rows = conn.execute(
        f"SELECT {_TASK_SELECT} FROM tasks{tail} ORDER BY id", params
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        tref = task_ref(row["id"])
        out.append({
            "ref": tref,
            "state": row["state"],
            "owner": row["owner"],
            "requester": row["requester"],
            "addressed_to": row["addressed_to"],
            "title": row["title"],
            "updated_ts": _updated_ts(row, _event_rows(conn, row["id"])),
            "blocked": _is_blocked(_blockers(conn, tref)),
        })
    return out, ""


def list_tasks(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    owner: str = "",
    requester: str = "",
    squad: str = "",
    state: str = "",
    addressed_to: str = "",
    member_squads: list[str] | tuple[str, ...] = (),
    format: str = "text",
) -> str:
    if format not in ("text", "json"):
        return (
            f"REFUSED: format={format!r} — one of 'text' (default) or 'json'."
        )
    if format == "json":
        rows_json, refusal = summaries(
            conn, owner=owner, requester=requester, squad=squad, state=state,
            addressed_to=addressed_to, member_squads=member_squads,
        )
        if refusal:
            return refusal
        return json.dumps({"tasks": rows_json}, ensure_ascii=False)
    tail, params, refusal, shown = _filter(
        owner, requester, squad, state, addressed_to, member_squads)
    if refusal:
        return refusal
    rows = conn.execute(
        f"SELECT {_TASK_SELECT} FROM tasks{tail} ORDER BY id", params
    ).fetchall()
    if not rows:
        return f"No tasks match ({shown})."
    # Each task's own text goes behind the mark under its ref, for the reason
    # the DATA_HEADER states in the single-task render: a title at one of the
    # columns this render writes at forges a line of it, and an unmarked title
    # reads as a message header to anything that splits a drain into records.
    lines = [
        f"{len(rows)} task(s) ({shown}); each title is {DATA_HEADER}, "
        f"behind │:"
    ]
    for row in rows:
        held = f"owner {row['owner']}" if row["owner"] else "unclaimed"
        blocked = " · BLOCKED" if _is_blocked(
            _blockers(conn, task_ref(row["id"]))
        ) else ""
        lines.append(
            f"⟨{task_ref(row['id'])}⟩ {row['state']}, {held} · requested by "
            f"{row['requester']} · addressed to {row['addressed_to']}{blocked}"
        )
        lines.extend(_data_lines(hooks, row["title"]))
    return "\n".join(lines)
