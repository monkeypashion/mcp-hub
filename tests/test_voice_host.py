"""Host half of /voice — the authorisation rules, asserted without a machine.

The aligned sender lives in `voice.py` and is tested there; there is exactly
one implementation of it on purpose.
"""

from __future__ import annotations

import pytest

from mcp_hub.voice_host import (
    DEFAULT_GATEWAY,
    authorised,
    listen_address,
    parse_container_map,
    parse_squad_roster,
    roster_names,
    verify_peer,
)

ROSTER_CONF = """\
# comment line
dreamteam-dev-vm-1|/home/monke/Projects/code/x||--continue
mcp-hub-seat-dev-vm-1|/home/monke/Projects/code/mcp-hub-seat||@docker:mcp-hub-seat-dev-vm-1|faculty
vps-hetzner-cap-dev-vm-1|/home/monke/Projects/capsule/vps-hetzner||@docker:vps-hetzner-cap-dev-vm-1|faculty
mcp-hub-duo-dev-vm-1|/home/monke/Projects/duo/mcp-hub||@docker:duo-pod-dev-vm-1:mcp-hub-duo-dev-vm-1|faculty
vps-hetzner-duo-dev-vm-1|/home/monke/Projects/duo/vps-hetzner||@docker:duo-pod-dev-vm-1:vps-hetzner-duo-dev-vm-1|faculty
"""

INSPECT = (
    "c89fd45af06e|/duo-pod-dev-vm-1|mcp-hub-seat:2026-08-07|172.17.0.5 \n"
    "ccce49f2061c|/mcp-hub-seat-dev-vm-1|mcp-hub-seat:latest|172.17.0.2 \n"
    "09449ef3d43c|/vps-hetzner-cap-dev-vm-1|mcp-hub-seat:latest|172.17.0.4 172.18.0.4 \n"
    # NOT one of ours: a database someone started on the same box.
    "aa11bb22cc33|/some-postgres|postgres:16|172.17.0.8 \n"
)

ROSTER = parse_squad_roster(ROSTER_CONF)


# -- the bind address, which is a security control rather than a default ------


def test_listen_address_is_the_gateway_never_the_wildcard():
    # ufw here permits everything on tailscale0, so a wildcard bind would put
    # the operator's live microphone on the tailnet.
    assert listen_address() == DEFAULT_GATEWAY
    assert listen_address("") == DEFAULT_GATEWAY
    assert listen_address("   ") == DEFAULT_GATEWAY
    assert listen_address("172.20.0.1") == "172.20.0.1"


# -- the docker map ----------------------------------------------------------


def test_parse_container_map_reads_ids_names_and_every_address():
    cmap = parse_container_map(INSPECT)
    assert cmap["172.17.0.5"] == ("c89fd45af06e", "duo-pod-dev-vm-1",
                                  "mcp-hub-seat:2026-08-07")
    # a container on two networks is reachable at either address
    assert cmap["172.18.0.4"].name == "vps-hetzner-cap-dev-vm-1"
    assert len(cmap) == 5


@pytest.mark.parametrize("junk", ["", "garbage", "onlyid|", "|/name|img|1.2.3.4",
                                  "id|/|img|1.2.3.4", "id|/name|img"])
def test_parse_container_map_skips_what_it_cannot_read(junk):
    # A half-understood mapping is how a stream reaches the wrong container.
    assert parse_container_map(junk) == {}


def test_roster_accepts_both_naming_conventions():
    # created seats name themselves by SEAT_CONTAINER (the name); ADOPTED ones
    # fall back to hostname, which docker defaults to the short id.
    names = roster_names(parse_container_map(INSPECT))
    assert "duo-pod-dev-vm-1" in names
    assert "c89fd45af06e" in names


# -- authentication, not just authorisation ----------------------------------


def test_verify_peer_matches_name_or_id():
    cmap = parse_container_map(INSPECT)
    assert verify_peer("172.17.0.2", "mcp-hub-seat-dev-vm-1", cmap)
    assert verify_peer("172.17.0.2", "ccce49f2061c", cmap)


def test_a_container_cannot_claim_another_seats_name():
    # THE attack the handshake alone cannot stop: seat names are public, so a
    # rogue container could ask for any of them and be fed the operator's mic.
    cmap = parse_container_map(INSPECT)
    assert not verify_peer("172.17.0.5", "mcp-hub-seat-dev-vm-1", cmap)
    assert not authorised("172.17.0.5", "mcp-hub-seat-dev-vm-1", cmap, ROSTER)


def test_unknown_address_is_refused():
    cmap = parse_container_map(INSPECT)
    assert not verify_peer("172.17.0.99", "duo-pod-dev-vm-1", cmap)


def test_empty_roster_fails_closed():
    # "I could not tell who was asking" must never resolve to "go ahead".
    assert not authorised("172.17.0.5", "duo-pod-dev-vm-1", {}, ROSTER)


def test_authorised_is_the_address_not_the_claim():
    # CHANGED DELIBERATELY 2026-08-08, and the earlier version of this test
    # asserted the opposite: that an unrecognised claim is refused. It is not,
    # because an ADOPTED container's only identity is gethostname(), which was
    # measured drifting from both the docker name and the id. Refusing on the
    # claim would have made adopted seats permanently and silently voiceless.
    # Nothing is lost: any live container could always have obtained audio by
    # naming itself correctly, so the claim never carried security.
    cmap = parse_container_map(INSPECT)
    assert authorised("172.17.0.5", "duo-pod-dev-vm-1", cmap, ROSTER)
    assert authorised("172.17.0.5", "an-unrecognised-hostname", cmap, ROSTER)
    # What IS still refused: an address docker cannot account for, and a claim
    # that names a DIFFERENT live container.
    assert not authorised("172.17.0.99", "duo-pod-dev-vm-1", cmap, ROSTER)
    assert not authorised("172.17.0.5", "mcp-hub-seat-dev-vm-1", cmap, ROSTER)


# -- identity from the ADDRESS, not from the claim ---------------------------


def test_decide_takes_identity_from_the_address():
    from mcp_hub.voice_host import decide

    cmap = parse_container_map(INSPECT)
    ok, seat, why = decide("172.17.0.5", "duo-pod-dev-vm-1", cmap, ROSTER)
    assert (ok, seat, why) == (True, "duo-pod-dev-vm-1", "")


def test_adopted_container_with_a_drifted_hostname_still_gets_audio():
    # MEASURED 2026-08-08: a container created with an explicit --hostname
    # reports `totally-unrelated-name` — neither its docker name nor its id —
    # and gethostname() is the only identity an adopted container has. Matching
    # that against a roster refuses it silently, forever.
    from mcp_hub.voice_host import decide

    cmap = parse_container_map(INSPECT)
    ok, seat, why = decide("172.17.0.5", "totally-unrelated-name", cmap, ROSTER)
    assert ok is True
    assert seat == "duo-pod-dev-vm-1", "identity must come from the address"
    assert why, "an odd-but-accepted claim must still be diagnosable"


def test_claiming_another_live_container_is_refused_as_impersonation():
    from mcp_hub.voice_host import decide

    cmap = parse_container_map(INSPECT)
    ok, _seat, why = decide("172.17.0.5", "mcp-hub-seat-dev-vm-1", cmap, ROSTER)
    assert ok is False
    assert "refused" in why


def test_a_dotted_container_name_is_a_real_name():
    # MEASURED: `docker create --name voice.dotcheck.tmp` is ACCEPTED, so a dot
    # is legal and the wire parser must not reject it.
    cmap = parse_container_map(
        "abc123def456|/seat.one|mcp-hub-seat:latest|172.17.0.9\n")
    from mcp_hub.voice_host import decide

    ok, seat, _why = decide("172.17.0.9", "seat.one", cmap, {"seat.one"})
    assert (ok, seat) == (True, "seat.one")


def test_every_refusal_states_a_reason():
    from mcp_hub.voice_host import decide

    cmap = parse_container_map(INSPECT)
    for peer, claim in (("172.17.0.99", "duo-pod-dev-vm-1"),
                        ("172.17.0.5", "mcp-hub-seat-dev-vm-1")):
        ok, _seat, why = decide(peer, claim, cmap, ROSTER)
        assert not ok and why, "fail-closed must never also be undiagnosable"
    ok, _seat, why = decide("172.17.0.5", "x", {}, ROSTER)
    assert not ok and why == "roster unavailable"


# -- authorisation: being a container is not being a SEAT --------------------


def test_a_container_not_in_the_roster_is_refused_the_microphone():
    # ⭐ fireblade's push-back. `some-postgres` is a real, live, correctly
    # identified container — we know EXACTLY who it is — and it still may not
    # listen to the room, because nobody enrolled it.
    from mcp_hub.voice_host import decide

    cmap = parse_container_map(INSPECT)
    ok, seat, why = decide("172.17.0.8", "some-postgres", cmap, ROSTER)
    assert ok is False
    assert seat == "some-postgres", "we still know who it was, for the log"
    assert "not enrolled" in why


def test_an_empty_or_unreadable_roster_fails_closed():
    from mcp_hub.voice_host import decide

    cmap = parse_container_map(INSPECT)
    ok, _seat, why = decide("172.17.0.5", "duo-pod-dev-vm-1", cmap, set())
    assert ok is False
    assert "roster unavailable" in why


def test_roster_parses_pods_and_ignores_non_container_rows():
    # A POD puts SEVERAL rows on ONE container — measured on this box, two
    # duo-* rows both name duo-pod-dev-vm-1. Worktree agents have no @docker
    # field and must not appear at all.
    assert "duo-pod-dev-vm-1" in ROSTER
    assert "mcp-hub-seat-dev-vm-1" in ROSTER
    assert "dreamteam-dev-vm-1" not in ROSTER, "a worktree agent is not a container"
    assert "mcp-hub-duo-dev-vm-1" not in ROSTER, "that is the AGENT, not the container"


def test_roster_ignores_comments_and_junk():
    assert parse_squad_roster("") == set()
    assert parse_squad_roster("# @docker:not-a-row") == set()
    assert parse_squad_roster("a|b|c") == set()
    assert parse_squad_roster("a|b|c|@docker:|x") == set()


def test_an_adopted_container_is_authorised_whatever_its_image():
    # 🔴 THE CORRECTION. My first gate used the container IMAGE, which is 100%
    # accurate on today's fleet and WRONG about the rule: `squad add-container`
    # never reads the image, so an adopted seat may be built from anything.
    # Enrolment is the decision; the image is a coincidence of the population.
    from mcp_hub.voice_host import decide

    cmap = parse_container_map(
        "ff00ff00ff00|/mcp-hub-seat-dev-vm-1|some-random:image|172.17.0.20\n")
    ok, seat, _why = decide("172.17.0.20", "mcp-hub-seat-dev-vm-1", cmap, ROSTER)
    assert ok is True, "enrolment decides, not the image"
    assert seat == "mcp-hub-seat-dev-vm-1"
