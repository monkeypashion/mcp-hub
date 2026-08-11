"""The `ra.feature/1` scheme — RA's work items as refs, under FJ's contract.

The FOURTH registered scheme, deliberately: the hub's own schemes came first
(refs.py), so this adapter demonstrably uses the same public interface and
carries no privilege. RA's steer, verbatim: "I'd rather be one scheme among
several than the assumed default."

Contract citations:
- `reliable_ai.progress.features.FEATURE_ID_CONTRACT` (RA, `95d3cac`): the
  identifying pair is **(feature_set_key, feature.id)** — scoped, not global;
  ids are immutable once written; unique within the set (now enforced at
  RA's end — `DuplicateFeatureIdError` at `FeatureList.__init__`).
- `hub-work-item-ref-contract.md` §4 @ `7b2e0eb` (FJ): the six refusal rules.
  Rules 1, 2, 5, 6 live here (they are about refs and their resolution);
  rules 3 and 4 are about the RESOLVED ANSWER and live in
  `status_resolution.py` — FJ's own fit-check: building rule 4 into the
  envelope would put it where it cannot do its job.

⚠️ `feature_set_key` is NEVER derived from a repo path or name. The live
counter-example is `dreamteam-analytics-service`, whose key is
`analytics-service` — restored as-is, because repair criteria forbid editing
during a restore. A key comes from the ref, or from registration; nowhere
else.
"""

from __future__ import annotations

import json
import sqlite3
import time

from mcp_hub.refs import Ref, RefError, Scheme, register_scheme

__all__ = [
    "ensure_schema",
    "list_feature_sets",
    "register_feature_set",
    "resolve",
]

CONTRACT = "hub-work-item-ref-contract.md@7b2e0eb"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ra_feature_sets (
               key           TEXT PRIMARY KEY,
               document      TEXT NOT NULL,
               registered_at REAL NOT NULL,
               registered_by TEXT NOT NULL DEFAULT ''
           )"""
    )
    conn.commit()


def _features_of(document: dict) -> list[dict]:
    feats = document.get("features")
    if not isinstance(feats, list):
        raise RefError(
            "document refused: no features[] list — this store resolves "
            "IDENTITY only and needs ids, nothing else (the canonical shape "
            "is FJ's lane, deliberately not re-implemented here)"
        )
    return [f for f in feats if isinstance(f, dict)]


def register_feature_set(
    conn: sqlite3.Connection, key: str, document: dict, registered_by: str = ""
) -> dict:
    """Store a feature-set document under its key.

    The KEY IS GIVEN, never derived — see the module docstring's live
    counter-example. Duplicate ids are refused HERE, at first notice (rule 6:
    the hub is the first place that can notice; uniqueness is unenforced
    upstream) — and checked AGAIN at resolve time, because a row can predate
    this gate or arrive by other means. Same defence-in-depth as W2.3's
    validator: the second gate exists for the paths the first cannot see.
    """
    if not key:
        raise RefError("feature_set_key required — and never derived from a "
                       "repo path or name")
    feats = _features_of(document)
    ids = [str(f.get("id", "")) for f in feats]
    if "" in ids:
        raise RefError("document refused: a feature without an id is "
                       "unaddressable — every feature needs one")
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise RefError(
            f"document refused: duplicate feature ids {dupes} — rule 6 "
            f"({CONTRACT}): a pair resolving to more than one feature must "
            f"fail loudly, not pick one"
        )
    conn.execute(
        "INSERT INTO ra_feature_sets (key, document, registered_at,"
        " registered_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET document = excluded.document,"
        " registered_at = excluded.registered_at,"
        " registered_by = excluded.registered_by",
        (key, json.dumps(document), time.time(), registered_by),
    )
    conn.commit()
    return {"key": key, "features": len(feats)}


def list_feature_sets(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"key": r["key"], "registered_at": r["registered_at"],
         "registered_by": r["registered_by"],
         "features": len(_features_of(json.loads(r["document"])))}
        for r in conn.execute(
            "SELECT * FROM ra_feature_sets ORDER BY key"
        ).fetchall()
    ]


def resolve(conn: sqlite3.Connection, ref: Ref) -> dict:
    """Resolve a ref to the WORK ITEM's identity — deliberately not its
    status.

    Rule 3 ({CONTRACT}): status must never be read from an authored/stored
    document — intent ≠ state. So the one field this resolver conspicuously
    OMITS is `status`; asking "is it done" is `resolve_status`, which fails
    closed until a blessed observed target exists (W3.5).

    Distinct outcomes, never conflated:
    - unknown key      -> rule 5 refusal (unknown ≠ new; nothing minted)
    - ambiguous doc    -> rule 6 refusal (document-corrupt, names the count)
    - id not in doc    -> not-found (a MISSING feature, not a corrupt file)
    - found            -> identity fields, no status
    """
    key = ref.get("feature_set_key")
    row = conn.execute(
        "SELECT document FROM ra_feature_sets WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        known = [r["key"] for r in conn.execute(
            "SELECT key FROM ra_feature_sets ORDER BY key"
        ).fetchall()]
        raise RefError(
            f"unknown feature_set_key {key!r} — rule 5 ({CONTRACT}): unknown "
            f"means UNKNOWN, never 'new'; no lineage is minted. Registered "
            f"sets: {', '.join(known) or '(none)'}. Keys are never derived "
            f"from repo names — check the set's birth certificate."
        )
    fid = ref.get("id")
    matches = [f for f in _features_of(json.loads(row["document"]))
               if str(f.get("id", "")) == fid]
    if len(matches) > 1:
        raise RefError(
            f"AMBIGUOUS: ({key!r}, {fid!r}) matches {len(matches)} features "
            f"— rule 6 ({CONTRACT}): fail loudly, never pick one. This is a "
            f"corrupt DOCUMENT, not a missing feature; re-register the set "
            f"after repair."
        )
    if not matches:
        return {"resolved": False, "reason": "not-found",
                "detail": f"no feature {fid!r} in set {key!r} — the set "
                          f"exists and is unambiguous; this id is not in it"}
    f = matches[0]
    return {
        "resolved": True,
        "feature_set_key": key,
        "id": fid,
        "name": f.get("name", ""),
        "description": f.get("description", ""),
        "phase": f.get("phase"),
        # rule 3: status is CONSPICUOUSLY absent — an authored document
        # carries intent, and intent ≠ state. Ask resolve_status, which
        # fails closed until an OBSERVED target is registered.
        "status": None,
        "status_note": (
            f"deliberately omitted (rule 3, {CONTRACT}) — use "
            f"resolve_status, which refuses until a blessed observed "
            f"target is registered"
        ),
    }


register_scheme(Scheme(
    "ra.feature", 1,
    # Rule 1: BOTH halves of the pair, no key-only, no id-only — enforced by
    # the envelope's required-fields machinery, which is the point of one
    # identity mechanism: the rule costs one line.
    required=frozenset({"feature_set_key", "id"}),
    forbidden={
        # Rule 2: an item-version PIN is not the scheme version. The envelope
        # carries /1 as the CONTRACT version; a ref pinning the ITEM is how
        # a stale pointer masquerades as a precise one.
        "version": f"rule 2 ({CONTRACT}) — display-only or absent; the /1 in "
                   f"the scheme names the contract, never the item",
    },
    resolve=resolve,
))
