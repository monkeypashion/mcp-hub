"""The lineage graph — work-item relationships, as data.

Operator's driving requirement (corrected at his request, 2026-08-26 — he
retracted an earlier fed-up-with-the-past phrasing this header used to
quote): "I care much more about looking forward and seeing a well defined
path than looking back … I do want to understand relationships but only in
the name of quality and speed of production." So the graph exists to make
the PATH legible — what relates to what, in service of moving forward —
not as archaeology for its own sake. Nodes are refs (see `refs.py`: one
identity mechanism for hub artifacts and external work items alike), edges
are `(subject, predicate, object)` triples.
The triple SHAPE is deliberately RDF-compatible so an exporter stays a
serializer over existing data; the RDF machinery (SPARQL, ontologies, IRIs)
is deliberately absent.

## How edges get here — three paths, one refusal

- **auto**: facts the hub itself performed — authorship, routing, the card
  lifecycle. Written by the verbs in server.py at the moment they act.
- **declared**: `in_reply_to` on send/post/broadcast — what only the sender
  knows. The `source` column keeps the two apart so a consumer can weight a
  hub-witnessed edge above a self-reported one.
- **never inferred**: the hub does NOT guess what a DM replies to. A guessed
  causal edge is a record that mirrors a plausible story instead of observing
  one — the `feature_outcomes` disease in new clothing — and it would be
  confidently wrong exactly where the chain matters most.

Edges are APPEND-ONLY and OUTLIVE their artifacts (the `api_seat_events`
death-fact pattern): purging an artifact never cascades here, and a dangling
object ref MEANS "the artifact is gone", which is itself lineage. There is no
raw edge-write API — edges only ride existing verbs, under the existing
attribution gate.
"""

from __future__ import annotations

import sqlite3
import time

from mcp_hub.refs import Ref, RefError, canonical, parse_ref

__all__ = [
    "PREDICATES",
    "clear_blocked",
    "coverage",
    "declare_blocked",
    "ensure_schema",
    "walk",
    "write_edge",
]

# The registered predicate vocabulary. An unknown predicate is REFUSED naming
# this set — the same fail-closed rule as an unknown scheme and as status
# resolution. A vocabulary nobody can extend by typo is the point.
PREDICATES: dict[str, str] = {
    "authored-by": "subject artifact was written by object agent (auto)",
    "addressed-to": "subject artifact was routed to object (auto)",
    "replies-to": "subject artifact answers object artifact (declared)",
    "resolves": "subject agent resolved object decision card (auto)",
    "supersedes": "subject card replaced object card (auto)",
    "blocked-by": "subject work cannot start until object clears (declared, "
                  "lifecycle: live until explicitly cleared)",
}

# Walks are COLD-tier queries (operator/agent asks), never hot-path — but an
# unbounded walk over a pathological graph is still a refusal, not a hang.
MAX_DEPTH = 6


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent, like every prior hub migration."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lineage_edges (
               subject   TEXT NOT NULL,
               predicate TEXT NOT NULL,
               object    TEXT NOT NULL,
               ts        REAL NOT NULL,
               source    TEXT NOT NULL,
               PRIMARY KEY (subject, predicate, object)
           )"""
    )
    # Q1 walks backward (index on object), Q2 forward (PK's subject prefix),
    # Q4 filters by predicate.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lineage_object ON lineage_edges(object)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lineage_pred_subj"
        " ON lineage_edges(predicate, subject)"
    )
    # blocked-by lifecycle columns (docs/lineage-blocked-by.md). Nullable on
    # purpose: NULL cleared_at means LIVE, and every pre-existing edge (all
    # past-fact predicates) is timelessly "live" — absent-vs-empty as ever.
    # declared_by exists because blocked-by is the first predicate where WHO
    # asserted it is load-bearing: the same authority that declares must
    # clear, and a consumer weights an attributed-but-unverified declaration
    # below an ownership-verified one.
    for _sql in (
        "ALTER TABLE lineage_edges ADD COLUMN declared_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE lineage_edges ADD COLUMN cleared_at REAL",
        "ALTER TABLE lineage_edges ADD COLUMN cleared_by TEXT",
    ):
        try:
            conn.execute(_sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def write_edge(
    conn: sqlite3.Connection,
    subject: Ref | str,
    predicate: str,
    obj: Ref | str,
    source: str,
) -> bool:
    """Record one edge. Returns False if it already existed (append-only:
    re-assertion is a no-op, never an update — the ts stays the FIRST time
    the fact was recorded).

    Refusals happen HERE, at write time, not in the reader: an unknown
    predicate, a malformed ref, or a self-edge must never enter the store for
    a consumer to trip over.
    """
    if predicate not in PREDICATES:
        raise RefError(
            f"unknown predicate {predicate!r} — registered: "
            f"{', '.join(sorted(PREDICATES))}"
        )
    if predicate == "blocked-by":
        # The one predicate with a lifecycle and an authority check — it has
        # its own writer. Letting it through here would mint an
        # unattributed, unclearable blockage.
        raise RefError(
            "blocked-by edges are written via declare_blocked(), which "
            "carries the authority check and the lifecycle — not write_edge"
        )
    if source not in ("auto", "declared"):
        raise RefError(f"edge source must be auto|declared, got {source!r}")
    s = canonical(subject) if isinstance(subject, Ref) else canonical(parse_ref(subject))
    o = canonical(obj) if isinstance(obj, Ref) else canonical(parse_ref(obj))
    if s == o:
        raise RefError(f"self-edge refused: {s} -{predicate}-> itself")
    cur = conn.execute(
        "INSERT OR IGNORE INTO lineage_edges"
        " (subject, predicate, object, ts, source) VALUES (?, ?, ?, ?, ?)",
        (s, predicate, o, time.time(), source),
    )
    conn.commit()
    return cur.rowcount > 0


def declare_blocked(
    conn: sqlite3.Connection,
    subject: Ref | str,
    obj: Ref | str,
    declared_by: str,
) -> bool:
    """Declare subject blocked-by object — docs/lineage-blocked-by.md.

    AUTHORITY, enforced where the graph can and attributed where it cannot:
    when the subject has a recorded author (an authored-by edge) it must be
    the declarer — a lane provably declaring about SOMEONE ELSE'S work is
    refused, because that turns the path view into a surface where one lane
    paints another stuck. A subject with NO recorded author (external work
    items — the hub has no ownership model for them) is allowed and
    attributed: `declared_by` rides the edge so a consumer can weight an
    unverified declaration, which is honest in a way a refusal that makes
    the most useful refs undeclarable would not be.

    LIFECYCLE: re-declaring a LIVE edge is idempotent (returns False,
    nothing moves — mashing "still blocked" must not shift the clock).
    Re-declaring a CLEARED edge RE-OPENS it with ts = now: unlike every
    past-fact predicate, the declaration time here feeds staleness
    rendering, and keeping the original ts would date a new blockage by a
    dead one.
    """
    s = canonical(subject) if isinstance(subject, Ref) else canonical(parse_ref(subject))
    o = canonical(obj) if isinstance(obj, Ref) else canonical(parse_ref(obj))
    if s == o:
        raise RefError(f"self-edge refused: {s} -blocked-by-> itself")
    if not declared_by:
        raise RefError("blocked-by needs a declarer — anonymous intent is "
                       "not a record")
    author = conn.execute(
        "SELECT object FROM lineage_edges WHERE subject = ? AND "
        "predicate = 'authored-by' LIMIT 1", (s,),
    ).fetchone()
    if author is not None:
        # Exact canonical comparison, not a substring — an endswith here
        # would let 'bob' pass for 'alice-bob', and the first draft of this
        # check guessed the wrong param name entirely (agent= for name=),
        # which is why it compares refs, never strings.
        declarer_ref = canonical(parse_ref(f"hub.agent/1?name={declared_by}"))
        if author["object"] != declarer_ref:
            raise RefError(
                f"blocked-by refused: {s} has a recorded author "
                f"({author['object']}) and it is not '{declared_by}' — only "
                f"the lane that owns the blocked work may declare it blocked"
            )
    row = conn.execute(
        "SELECT ts, cleared_at FROM lineage_edges WHERE subject = ? AND "
        "predicate = 'blocked-by' AND object = ?", (s, o),
    ).fetchone()
    if row is not None:
        if row["cleared_at"] is None:
            return False  # live already — idempotent, clock untouched
        conn.execute(
            "UPDATE lineage_edges SET ts = ?, declared_by = ?, "
            "cleared_at = NULL, cleared_by = NULL WHERE subject = ? AND "
            "predicate = 'blocked-by' AND object = ?",
            (time.time(), declared_by, s, o),
        )
        conn.commit()
        return True
    conn.execute(
        "INSERT INTO lineage_edges (subject, predicate, object, ts, source, "
        "declared_by) VALUES (?, 'blocked-by', ?, ?, 'declared', ?)",
        (s, o, time.time(), declared_by),
    )
    conn.commit()
    return True


def clear_blocked(
    conn: sqlite3.Connection,
    subject: Ref | str,
    obj: Ref | str,
    cleared_by: str,
    is_operator: bool = False,
) -> None:
    """Clear a live blocked-by edge — the first-class half of the lifecycle.

    The edge is KEPT, marked cleared — never deleted (a vanished edge is
    indistinguishable from one never declared). Only the declarer or the
    operator clears: the authority that asserted the future fact is the one
    entitled to retract it. Clearing a non-existent or already-cleared edge
    REFUSES loudly — a clear against nothing is a typo wearing a path's
    clothes, and swallowing it would hide exactly the misfire the loud
    in_reply_to refusal exists to surface.
    """
    s = canonical(subject) if isinstance(subject, Ref) else canonical(parse_ref(subject))
    o = canonical(obj) if isinstance(obj, Ref) else canonical(parse_ref(obj))
    row = conn.execute(
        "SELECT declared_by, cleared_at FROM lineage_edges WHERE subject = ? "
        "AND predicate = 'blocked-by' AND object = ?", (s, o),
    ).fetchone()
    if row is None:
        raise RefError(
            f"clear refused: no blocked-by edge {s} -> {o} was ever "
            f"declared — check the refs"
        )
    if row["cleared_at"] is not None:
        raise RefError(
            f"clear refused: blocked-by edge {s} -> {o} is already cleared "
            f"— a second clear would silently rewrite cleared_by"
        )
    if not is_operator and row["declared_by"] != cleared_by:
        raise RefError(
            f"clear refused: declared by '{row['declared_by']}', and only "
            f"the declaring authority (or the operator) may clear it"
        )
    conn.execute(
        "UPDATE lineage_edges SET cleared_at = ?, cleared_by = ? WHERE "
        "subject = ? AND predicate = 'blocked-by' AND object = ?",
        (time.time(), cleared_by, s, o),
    )
    conn.commit()


def walk(
    conn: sqlite3.Connection,
    ref: Ref | str,
    depth: int = 2,
    direction: str = "both",
    predicate: str | None = None,
    include_cleared: bool = False,
) -> dict:
    """Bounded subgraph around `ref` — the Q1/Q2 read. Never a whole-graph
    dump: that answers none of the queries this graph exists for and invites
    O(everything) reads.

    A node with no edges at all comes back `lineage_blind: True` — the honest
    render of "nothing was recorded", which must stay distinguishable from
    "this is a root" (absence ≠ health, applied to our own surface).
    """
    if direction not in ("out", "in", "both"):
        raise RefError("direction must be out|in|both")
    if predicate is not None and predicate not in PREDICATES:
        raise RefError(
            f"unknown predicate {predicate!r} — registered: "
            f"{', '.join(sorted(PREDICATES))}"
        )
    depth = max(1, min(int(depth), MAX_DEPTH))
    root = canonical(ref) if isinstance(ref, Ref) else canonical(parse_ref(ref))

    pred_sql = " AND predicate = ?" if predicate else ""
    seen: set[str] = {root}
    edges: list[dict] = []
    edge_keys: set[tuple] = set()
    frontier = [root]
    for _ in range(depth):
        if not frontier:
            break
        nxt: list[str] = []
        for node in frontier:
            rows: list[sqlite3.Row] = []
            if direction in ("out", "both"):
                rows += conn.execute(
                    f"SELECT * FROM lineage_edges WHERE subject = ?{pred_sql}",
                    (node, predicate) if predicate else (node,),
                ).fetchall()
            if direction in ("in", "both"):
                rows += conn.execute(
                    f"SELECT * FROM lineage_edges WHERE object = ?{pred_sql}",
                    (node, predicate) if predicate else (node,),
                ).fetchall()
            for r in rows:
                key = (r["subject"], r["predicate"], r["object"])
                if key in edge_keys:
                    continue
                # A cleared blocked-by edge leaves the PATH view by default
                # — routing around finished blockages is the whole point —
                # but stays queryable as history (include_cleared=True).
                # Deleted-vs-excluded matters: the row survives.
                if (r["predicate"] == "blocked-by"
                        and r["cleared_at"] is not None
                        and not include_cleared):
                    continue
                edge_keys.add(key)
                edge = {
                    "subject": r["subject"], "predicate": r["predicate"],
                    "object": r["object"], "ts": r["ts"], "source": r["source"],
                }
                if r["predicate"] == "blocked-by":
                    # declared_at is the staleness instrument: the consumer
                    # renders the AGE of every live blockage ("declared 6d
                    # ago"), because the hub never infers completion and an
                    # old uncleared edge must be a visible question for its
                    # owner, not a hidden falsehood.
                    edge["declared_by"] = r["declared_by"]
                    edge["declared_at"] = r["ts"]
                    edge["cleared_at"] = r["cleared_at"]
                    edge["cleared_by"] = r["cleared_by"]
                edges.append(edge)
                for end in (r["subject"], r["object"]):
                    if end not in seen:
                        seen.add(end)
                        nxt.append(end)
        frontier = nxt

    return {
        "root": root,
        "lineage_blind": not edges,
        "nodes": sorted(seen),
        "edges": sorted(edges, key=lambda e: e["ts"]),
        "depth": depth,
        "direction": direction,
    }


def coverage(conn: sqlite3.Connection) -> dict:
    """What fraction of artifacts carry ANY lineage — so a thin graph reads
    as thinly POPULATED, never as thinly CONNECTED. This is the instrument
    that keeps the accepted day-one sparseness visible instead of assumed."""
    out: dict = {}
    for label, table, scheme_prefix in (
        ("messages", "messages", "hub.msg/1?id="),
    ):
        total = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        linked = conn.execute(
            "SELECT COUNT(DISTINCT subject) AS n FROM lineage_edges"
            " WHERE subject LIKE ? || '%'",
            (scheme_prefix,),
        ).fetchone()["n"]
        out[label] = {"total": total, "with_lineage": linked}
    out["edges"] = conn.execute(
        "SELECT COUNT(*) AS n FROM lineage_edges"
    ).fetchone()["n"]
    return out
