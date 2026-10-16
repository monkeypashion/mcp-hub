"""Hub tasks — a task is its own record, never a kind of message (v2-fleet r6).

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
`body`, a `result`) goes through `spec_guard.scan_secret` before it is stored,
because this store is the same plaintext SQLite as everything else — and it is
rendered through `_render_body`, under a header that says it is data, because a
task body containing a header-shaped line would otherwise forge a message in
any drain that read it back.

Deliberately NOT here in r6: `deadline_at`, `lease_seconds` and `lease_until`.
They arrive in r8 with the clocks that enforce them. Until then a non-empty
`deadline` or `lease` argument is REFUSED rather than stored, because a stored
deadline nothing sweeps is a promise the record makes and no code keeps — the
"declaration nothing enforces" shape that has already cost this fleet three
seats' memory. Accept/reopen/reassign, the authority model and the task's own
lineage edges are r7's.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from mcp_hub import refs, spec_guard

TASK_SCHEME = "hub.task/1"
AGENT_SCHEME = "hub.agent/1"
SQUAD_SCHEME = "hub.squad/1"

# The r6 lifecycle, and it is deliberately short: open → claimed → done.
# `accept` (the requester's separate transition) is r7's; stopping here keeps
# "the owner says it is finished" and "the requester agrees" from collapsing
# into one word, which is the distinction the whole feature is for.
OPEN = "open"
CLAIMED = "claimed"
DONE = "done"
STATES = (OPEN, CLAIMED, DONE)

ACT_CREATE = "create"
ACT_CLAIM = "claim"
ACT_CLAIM_REFUSED = "claim-refused"
ACT_DONE = "done"
ACT_DONE_REFUSED = "done-refused"

# `get_task(format="json")` is an explicit ALLOW-LIST (mcp-hub 36509), never
# the stored row. A column added to `tasks` later — r8's lease is the known
# one — is not interface until it is NAMED here. Dumping the row would make
# every future migration a silent interface change, and a consumer that grew
# to depend on the leak would be broken by the next one.
TASK_JSON_FIELDS = (
    "ref", "state", "version", "title", "body", "result", "requester",
    "requester_grade", "addressed_to", "owner", "owner_grade", "created_ts",
    "updated_ts", "events",
)
EVENT_JSON_FIELDS = (
    "seq", "ts", "act", "from_state", "to_state", "actor", "grade", "reason",
    "notice",
)

# Named columns on every read, for the same reason the json is an allow-list.
_TASK_COLUMNS = (
    "id", "title", "body", "requester", "addressed_to", "owner", "state",
    "created_at", "last_notice_msg_id", "version",
)
_TASK_SELECT = ", ".join(_TASK_COLUMNS)

# The header that says the lines under it are DATA, not hub voice.
DATA_HEADER = "task text (data)"


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks (state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks (owner)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_addressed ON tasks (addressed_to)"
    )
    conn.commit()


@dataclass(frozen=True)
class Hooks:
    """The four server-side helpers a task reuses, injected rather than imported.

    `server.py` imports this module, so the arrow cannot point back — and
    re-implementing any of these here is how two renderers of the same thing
    drift apart. `render_body` in particular is a safety rule (every line after
    the first is indented, so a body can never forge a message header): a
    second copy of it is a second place to forget.
    """

    render_body: Callable[[str], str]
    grade_tag: Callable[[str], str]
    msg_ref: Callable[[int], str]
    record_msg_lineage: Callable[..., None]


# ---------------------------------------------------------------------------
# Identity — a task's ref, and who a task is addressed to
# ---------------------------------------------------------------------------


def task_ref(task_id: int) -> str:
    return refs.canonical(refs.make_ref(TASK_SCHEME, id=int(task_id)))


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
    raw = parsed.get("id")
    if not raw.isdigit():
        return 0, f"REFUSED: ref={ref!r} has a non-numeric id."
    return int(raw), ""


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


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _row(conn: sqlite3.Connection, task_id: int) -> Any:
    return conn.execute(
        f"SELECT {_TASK_SELECT} FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


def _event_rows(conn: sqlite3.Connection, task_id: int) -> list[Any]:
    return conn.execute(
        "SELECT task_id, seq, act, from_state, to_state, actor, grade, reason,"
        " ts, notice_msg_id FROM task_events WHERE task_id = ? ORDER BY seq",
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
) -> int:
    """Append one event and return its seq. Caller holds the write lock."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS top FROM task_events WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    seq = int(row["top"]) + 1
    conn.execute(
        "INSERT INTO task_events (task_id, seq, act, from_state, to_state,"
        " actor, grade, reason, ts, notice_msg_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (task_id, seq, act, from_state, to_state, actor, grade, reason,
         time.time() if ts is None else ts),
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
) -> str:
    """Make a task. The reply carries its ref."""
    for field, value in (("deadline", deadline), ("lease", lease)):
        if str(value or "").strip():
            return (
                f"REFUSED: {field}={value!r} — r6 keeps no clocks. The "
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
# claim
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


def claim(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    version: int,
    member_squads: list[str],
) -> str:
    """Compare-and-set `open` → `claimed`. One winner; the losers are told who.

    THE TRANSACTION RULE (F18), found by the trial's first claim race and
    confirmed by mcp-hub 36509: `member_squads` is read by the CALLER, before
    this function runs, because `_squads_of` purges expired loans and COMMITS.
    A commit inside this transaction releases the write lock, and two claimers
    then take the same event number. So: membership before BEGIN IMMEDIATE,
    nothing that commits before our own COMMIT, lineage after it.
    """
    task_id, refusal = _parse_task_id(ref)
    if refusal:
        return refusal
    if _row(conn, task_id) is None:
        return _no_such_task(task_id)

    _begin_immediate(conn)
    try:
        # Re-read under the write lock: the row this decision is made on has to
        # be the row the write lands on, or the CAS is decoration.
        row = _row(conn, task_id)
        if row is None:
            conn.commit()
            return _no_such_task(task_id)
        tref = task_ref(task_id)
        refusal, reason = _may_claim(row, actor, member_squads)
        if not refusal and row["state"] != OPEN:
            refusal = (
                f"REFUSED: {tref} is already {row['state']}, claimed by "
                f"{row['owner']} — one owner at a time, and you were second. "
                f"Nothing was taken from {row['owner']}. Your attempt was "
                f"recorded."
            )
            reason = f"already {row['state']} by {row['owner']}"
        if not refusal and int(row["version"]) != int(version):
            refusal = (
                f"REFUSED: {tref} is at version {row['version']}, and you "
                f"claimed against version {version}. The task moved after you "
                f"read it: re-read it with get_task and claim the version you "
                f"actually saw. Your attempt was recorded."
            )
            reason = f"stale version {version} (now {row['version']})"
        if refusal:
            _append_event(
                conn, task_id, act=ACT_CLAIM_REFUSED, from_state=row["state"],
                to_state=row["state"], actor=actor, grade=grade, reason=reason,
            )
            conn.commit()
            return refusal

        now = time.time()
        new_version = int(row["version"]) + 1
        conn.execute(
            "UPDATE tasks SET owner = ?, state = ?, version = ? WHERE id = ?",
            (actor, CLAIMED, new_version, task_id),
        )
        seq = _append_event(
            conn, task_id, act=ACT_CLAIM, from_state=OPEN, to_state=CLAIMED,
            actor=actor, grade=grade, ts=now,
        )
        requester = row["requester"]
        msg_id, prev_id = _notice(
            conn, task_id, requester if requester != actor else "",
            f"task ⟨{tref}⟩ — claimed by {actor}{hooks.grade_tag(grade)}; it "
            f"is now {CLAIMED} at version {new_version}. The record is the "
            f'truth, this only points at it: get_task(ref="{tref}").',
        )
        if msg_id:
            _stamp_notice(conn, task_id, seq, msg_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _notice_lineage(conn, hooks, msg_id, prev_id, requester)
    return (
        f"Claimed ⟨{tref}⟩ — you own it, {CLAIMED} at version {new_version}. "
        f'End it with task_done(from_agent="{actor}", ref="{tref}", '
        f'result="..."): done is YOUR recorded claim, and nothing else '
        f"makes it."
    )


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------


def done(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    actor: str,
    grade: str,
    ref: str,
    result: str = "",
) -> str:
    """The owner's recorded claim that the work is finished. Nobody else's."""
    task_id, refusal = _parse_task_id(ref)
    if refusal:
        return refusal
    if _row(conn, task_id) is None:
        return _no_such_task(task_id)

    _begin_immediate(conn)
    try:
        row = _row(conn, task_id)
        if row is None:
            conn.commit()
            return _no_such_task(task_id)
        tref = task_ref(task_id)
        refusal, reason = "", ""
        if row["state"] != CLAIMED:
            refusal = (
                f"REFUSED: {tref} is {row['state']}, not {CLAIMED} — done is "
                f"the OWNER's claim, and a task nobody has claimed has no "
                f"owner to make it. Claim it first. Your attempt was recorded."
            )
            reason = f"not claimed (state {row['state']})"
        elif row["owner"] != actor:
            refusal = (
                f"REFUSED: {tref} is owned by {row['owner']}, not {actor}. "
                f"Done is the owner's recorded claim: a requester who thinks "
                f"it is finished says so to {row['owner']}, and r7's accept is "
                f"where their agreement gets its own transition. Your attempt "
                f"was recorded."
            )
            reason = f"not the owner ({row['owner']} is)"
        else:
            # The result is stored as this event's reason, so it is scanned
            # exactly like a title or a body — and the refusal names only the
            # PATTERN, never the match, including in the recorded reason.
            secret = spec_guard.scan_secret(str(result or ""), "task result")
            if secret:
                refusal = secret
                reason = "result refused: it matched a secret pattern"
        if refusal:
            _append_event(
                conn, task_id, act=ACT_DONE_REFUSED, from_state=row["state"],
                to_state=row["state"], actor=actor, grade=grade, reason=reason,
            )
            conn.commit()
            return refusal

        now = time.time()
        new_version = int(row["version"]) + 1
        conn.execute(
            "UPDATE tasks SET state = ?, version = ? WHERE id = ?",
            (DONE, new_version, task_id),
        )
        seq = _append_event(
            conn, task_id, act=ACT_DONE, from_state=CLAIMED, to_state=DONE,
            actor=actor, grade=grade, reason=str(result or ""), ts=now,
        )
        requester = row["requester"]
        msg_id, prev_id = _notice(
            conn, task_id, requester if requester != actor else "",
            f"task ⟨{tref}⟩ — reported {DONE} by {actor}"
            f"{hooks.grade_tag(grade)}. That is the owner's recorded claim "
            f"under their own grade, not the hub's verdict: read it, result "
            f'included, with get_task(ref="{tref}").',
        )
        if msg_id:
            _stamp_notice(conn, task_id, seq, msg_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _notice_lineage(conn, hooks, msg_id, prev_id, requester)
    return (
        f"Recorded: ⟨{tref}⟩ is {DONE}, claimed by {actor}"
        f"{hooks.grade_tag(grade)} at version {new_version}. r6's lifecycle "
        f"ends here — the requester's accept is a separate transition, in r7."
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
    }


def _view(hooks: Hooks, row: Any, events: list[Any]) -> dict:
    """The allow-listed view. Built field by field, on purpose."""
    created = _last(events, ACT_CREATE)
    claimed = _last(events, ACT_CLAIM)
    finished = _last(events, ACT_DONE)
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
        "updated_ts": float(events[-1]["ts"]) if events
        else float(row["created_at"]),
        "events": [_event_view(hooks, e) for e in events],
    }


def _state_phrase(hooks: Hooks, row: Any, events: list[Any]) -> str:
    """How a state reads: always as somebody's graded act, never as a fact the
    hub established by itself."""
    finished = _last(events, ACT_DONE)
    claimed = _last(events, ACT_CLAIM)
    if row["state"] == DONE and finished is not None:
        return (f"{DONE}, claimed by {finished['actor']}"
                f"{hooks.grade_tag(finished['grade'])}")
    if row["state"] == CLAIMED and claimed is not None:
        return (f"{CLAIMED} by {claimed['actor']}"
                f"{hooks.grade_tag(claimed['grade'])}")
    return f"{row['state']}, unclaimed"


def _render_task(hooks: Hooks, row: Any, events: list[Any]) -> str:
    created = _last(events, ACT_CREATE)
    finished = _last(events, ACT_DONE)
    req_grade = created["grade"] if created is not None else ""
    lines = [
        f"⟨{task_ref(row['id'])}⟩ {_state_phrase(hooks, row, events)}",
        f"  requested by {row['requester']}{hooks.grade_tag(req_grade)} · "
        f"addressed to {row['addressed_to']} · version {row['version']}",
        # Everything below this header is somebody's TEXT, indented so no line
        # of it can be read as a message header. The header says so out loud.
        DATA_HEADER,
        f"  title: {hooks.render_body(row['title'])}",
        f"  body:  {hooks.render_body(row['body'])}",
    ]
    if finished is not None:
        lines.append(f"  result: {hooks.render_body(finished['reason'])}")
    lines.append(f"log ({len(events)} events, append-only)")
    for e in events:
        bits = [
            f"  {e['seq']} {e['act']} by {e['actor']}"
            f"{hooks.grade_tag(e['grade'])}",
            f"{e['from_state'] or '·'}→{e['to_state'] or '·'}",
        ]
        if e["notice_msg_id"]:
            bits.append(f"notice ⟨{hooks.msg_ref(int(e['notice_msg_id']))}⟩")
        line = "  ".join(bits)
        if e["reason"]:
            line += f"\n    {hooks.render_body(e['reason'])}"
        lines.append(line)
    return "\n".join(lines)


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
        return json.dumps(_view(hooks, row, events), indent=2)
    return _render_task(hooks, row, events)


def list_tasks(
    conn: sqlite3.Connection,
    hooks: Hooks,
    *,
    owner: str = "",
    requester: str = "",
    squad: str = "",
    state: str = "",
) -> str:
    where: list[str] = []
    params: list[Any] = []
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
                return refusal
            if kind != "squad":
                return (
                    f"REFUSED: squad={squad!r} is not a squad — pass the squad "
                    f"name, or {SQUAD_SCHEME}?name=<squad>."
                )
        where.append("addressed_to = ?")
        params.append(_addressed_ref("squad", name))
    if state.strip():
        if state.strip() not in STATES:
            return (
                f"REFUSED: state={state!r} is not a task state — one of "
                f"{list(STATES)}. (accept and reopen arrive in r7.)"
            )
        where.append("state = ?")
        params.append(state.strip())

    sql = f"SELECT {_TASK_SELECT} FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    shown = ", ".join(
        f"{k}={v}" for k, v in (("owner", owner), ("requester", requester),
                                ("squad", squad), ("state", state)) if v.strip()
    ) or "no filters"
    if not rows:
        return f"No tasks match ({shown})."
    # Each task's own text is indented under its ref, for the reason the
    # DATA_HEADER states in the single-task render: an unindented title would
    # read as a message header to anything that splits a drain into records.
    lines = [f"{len(rows)} task(s) ({shown}); each title is {DATA_HEADER}:"]
    for row in rows:
        held = f"owner {row['owner']}" if row["owner"] else "unclaimed"
        lines.append(
            f"⟨{task_ref(row['id'])}⟩ {row['state']}, {held} · requested by "
            f"{row['requester']} · addressed to {row['addressed_to']}"
        )
        lines.append(f"  title: {hooks.render_body(row['title'])}")
    return "\n".join(lines)
