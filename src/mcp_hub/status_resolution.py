"""'Is this work item done?' — a question the hub currently REFUSES to answer.

FJ's reframe, adopted as the design (2026-08-11): **"no blessed target
exists" is a buildable state.** Refusing to resolve is correct behaviour
today; the resolver arrives later as a REGISTERED TARGET, not as a rewrite.
An unresolvable question is a different claim from "not done", and conflating
them is a false delivery report.

Why no target exists yet, on evidence read from source (dt pasted the
function body unasked): the one candidate, `feature_outcomes`, is populated
by `record_build_outcome()`, which inserts
`COALESCE(v_feature->>'status','not_attempted')` straight from
`p_features_json` — whose own schema comment reads "The features.json content
with final statuses". A store populated by copying the asserted value is a
MIRROR of the claim: it agrees with the document exactly when the document is
wrong, at any row count. That is the failure mode the intent plane exists to
catch, so it is refused BY NAME below.

Rules 3 and 4 of FJ's contract live here — their subject is the RESOLVED
ANSWER, not the ref (FJ's fit-check; building them into the envelope would
put them where they cannot do their job).
"""

from __future__ import annotations

import sqlite3
import time

from mcp_hub.refs import RefError

__all__ = [
    "ensure_schema",
    "register_status_target",
    "resolve_status",
]

CONTRACT = "hub-work-item-ref-contract.md@7b2e0eb"

# FJ's standing condition, literal: rows reappearing in feature_outcomes is
# NOT the registration signal — dt's mirror-detector passing is.
REQUIRED_ATTESTATION = "mirror-detector-passed"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS status_targets (
               name          TEXT PRIMARY KEY,
               document      TEXT NOT NULL,
               vocabulary    TEXT NOT NULL,
               source_kind   TEXT NOT NULL,
               attestation   TEXT NOT NULL,
               registered_at REAL NOT NULL
           )"""
    )
    conn.commit()


def register_status_target(
    conn: sqlite3.Connection,
    name: str,
    *,
    document: str = "",
    vocabulary: str = "",
    source_kind: str = "",
    attestation: str = "",
) -> dict:
    """Bless a status-resolution target. A DELIBERATE act with four gates —
    there is deliberately NO route to this in W3: nothing real can be
    registered yet, so the strongest correct state is a hub on which
    registration is impossible and resolution always refuses.

    The gates, each a named refusal:
    - rule 3: `source_kind` must be 'observed-log'. An authored document or a
      repo copy carries INTENT, and intent ≠ state.
    - rule 4 (two-part, and implementable as one check that silently loses
      half): `document` AND `vocabulary` are each required — a resolution
      must name which document and which status vocabulary it read. There are
      FOUR vocabularies in the estate, not three (`FeatureStatus` adds
      `skipped`), which is exactly how an unnamed vocabulary goes lossy.
    - E3: the attestation must be {REQUIRED_ATTESTATION!r}. Rows appearing in
      a repopulated table is NOT the signal; a mirror-detector passing is.
    """
    if not name:
        raise RefError("target name required")
    if source_kind != "observed-log":
        raise RefError(
            f"target {name!r} refused: source_kind {source_kind!r} — rule 3 "
            f"({CONTRACT}): status must never resolve against an authored "
            f"document or repo copy; intent ≠ state. Only an OBSERVED "
            f"completion log ('observed-log') can be blessed."
        )
    if not document:
        raise RefError(
            f"target {name!r} refused: no document named — rule 4 "
            f"({CONTRACT}): a resolution must name WHICH DOCUMENT it read"
        )
    if not vocabulary:
        raise RefError(
            f"target {name!r} refused: no vocabulary named — rule 4 "
            f"({CONTRACT}): a resolution must name WHICH STATUS VOCABULARY "
            f"it read (the estate has four, and an unnamed one is how "
            f"'skipped' silently collapses into 'blocked')"
        )
    if attestation != REQUIRED_ATTESTATION:
        extra = ""
        if name == "feature_outcomes":
            extra = (
                " Evidence on file: record_build_outcome() inserts "
                "COALESCE(v_feature->>'status','not_attempted') straight "
                "from p_features_json — a mirror of the authored document, "
                "which agrees with the claim exactly when the claim is "
                "wrong, at any row count."
            )
        raise RefError(
            f"target {name!r} refused: attestation "
            f"{REQUIRED_ATTESTATION!r} required, got {attestation!r}. "
            f"A writer whose rows DERIVE from the input document is a "
            f"mirror, not an observation.{extra}"
        )
    conn.execute(
        "INSERT INTO status_targets (name, document, vocabulary, source_kind,"
        " attestation, registered_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, document, vocabulary, source_kind, attestation, time.time()),
    )
    conn.commit()
    return {"registered": name, "document": document, "vocabulary": vocabulary}


def resolve_status(conn: sqlite3.Connection, ref_text: str) -> dict:
    """Answer 'is this done?' — today, by refusing.

    UNRESOLVABLE is the answer, and it is not 'not done': the first says the
    hub has no instrument, the second claims a measurement. E4's test pins
    the difference, because a hub that answered 'not done' with no target
    would be issuing confident false delivery reports.
    """
    ensure_schema(conn)
    target = conn.execute(
        "SELECT * FROM status_targets ORDER BY registered_at LIMIT 1"
    ).fetchone()
    if target is None:
        return {
            "resolvable": False,
            "status": None,
            "reason": (
                f"UNRESOLVABLE: no blessed status target is registered — and "
                f"this is NOT 'not done'; the hub has no instrument, which "
                f"is a different claim from a measurement. Registering a "
                f"target is a deliberate act (rule 5, {CONTRACT}: unknown "
                f"means unknown, never inferred) gated on an OBSERVED "
                f"completion log whose writer has passed a mirror check."
            ),
            "ref": ref_text,
        }
    # Defence in depth: a target row missing either half of rule 4 must not
    # produce an answer, however it got here (a schema migration, a hand
    # edit) — the registration gate is the first check, not the only one.
    if not target["document"] or not target["vocabulary"]:
        raise RefError(
            f"target {target['name']!r} is missing "
            f"{'a document' if not target['document'] else 'a vocabulary'} — "
            f"rule 4 ({CONTRACT}) makes this unanswerable; re-register the "
            f"target"
        )
    return {
        "resolvable": True,
        "target": target["name"],
        "document": target["document"],
        "vocabulary": target["vocabulary"],
        "ref": ref_text,
        # The actual lookup lands when a real target exists; W3 ships the
        # CONTRACT of the answer (named document, named vocabulary), which is
        # what downstream consumers build against.
        "status": None,
        "note": "target registered; per-item lookup arrives with the real "
                "target's read path",
    }
