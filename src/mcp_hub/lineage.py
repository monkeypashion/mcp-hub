"""The lineage graph — how the fleet got from A to B, as data.

Operator's driving requirement, verbatim: "I am fed up of not knowing what
happened and how we arrived from A to B." The graph answers that as data —
nodes are refs (see `refs.py`: one identity mechanism for hub artifacts and
external work items alike), edges are `(subject, predicate, object)` triples.
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
    "coverage",
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


def walk(
    conn: sqlite3.Connection,
    ref: Ref | str,
    depth: int = 2,
    direction: str = "both",
    predicate: str | None = None,
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
                edge_keys.add(key)
                edges.append({
                    "subject": r["subject"], "predicate": r["predicate"],
                    "object": r["object"], "ts": r["ts"], "source": r["source"],
                })
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
