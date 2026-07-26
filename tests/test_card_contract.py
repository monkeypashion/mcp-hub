"""The DECISION card format CONTRACT between its writers and its readers.

Three parties depend on one shape: agents author the block (per the hub's
connect-time instructions), squad's harvest/board detect and render it
(WAIT_ON_OP_RE + the field splitter), and the hub parses it into the
decisions table. Until now the contract lived only in regexes and in
whether anyone remembered the convention — fireblade-wsl's ask, 2026-07-26:
pin one canonical block against every reader, so a format change breaks a
test instead of silently making asks invisible (an undetected ask is an
invisible blocker — the expensive failure class).
"""

import re
from pathlib import Path

from mcp_hub.server import parse_decision_card

SQUAD = Path(__file__).resolve().parent.parent / "squad" / "squad"

CANONICAL_V2 = (
    "**DECISION**\n"
    "**ASK:** approve the widget rebuild\n"
    "**WHY:** the current one is broken\n"
    "**VALUE:** dashboards work again [7/10]\n"
    "**RISK:** an hour lost if wrong [3/10]\n"
    "**TAGS:** design, ops\n"
)

# What the squad harvest actually matches against: whitespace-flattened.
FLATTENED = " ".join(CANONICAL_V2.split())


def _squad_wait_re() -> str:
    """The live WAIT_ON_OP_RE from squad/squad — the single-sourced pattern
    the harvest, the 🙋 badge and the board's bio check all read."""
    for line in SQUAD.read_text().splitlines():
        if line.startswith("export WAIT_ON_OP_RE="):
            return line.split("=", 1)[1].strip().strip("'")
    raise AssertionError("WAIT_ON_OP_RE not found in squad/squad")


def test_canonical_card_matches_squad_detector():
    assert re.search(_squad_wait_re(), FLATTENED, re.I), (
        "canonical DECISION block no longer matches squad's WAIT_ON_OP_RE — "
        "asks would stop badging 🙋 and stop floating on the board"
    )


def test_canonical_card_splits_into_all_fields():
    """The board splitter's label set (mirrored here) must break the
    flattened block into every field the author wrote."""
    parts = re.sub(
        r"(^| )(ASK|WHY|VALUE|RISK|SCORE|TAGS):",
        r"\n\2:",
        FLATTENED.replace("**", ""),
    ).splitlines()
    labels = [p.split(":", 1)[0] for p in parts if ":" in p]
    for want in ("ASK", "WHY", "VALUE", "RISK", "TAGS"):
        assert want in labels, f"{want} line lost by the board splitter"


def test_canonical_card_parses_hub_side():
    f = parse_decision_card(CANONICAL_V2)
    assert f["ask"] and f["why"] and f["value_text"] and f["risk_text"]
    assert f["value_score"] == 7 and f["risk_score"] == 3
    assert f["net_score"] == 4
    assert f["tags"] == "design,ops"


def test_v1_pipe_card_still_detected_and_parsed():
    """v1 single-line cards exist in the wild; both readers must keep
    accepting them until the last one ages out."""
    v1 = ("DECISION — ASK: build it | WHY: needed | VALUE: works | "
          "RISK: an hour | SCORE: 8/10")
    assert re.search(_squad_wait_re(), v1, re.I)
    assert parse_decision_card(v1)["net_score"] == 8
