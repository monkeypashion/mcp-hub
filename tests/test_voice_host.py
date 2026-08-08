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
    roster_names,
    verify_peer,
)

INSPECT = (
    "c89fd45af06e|/duo-pod-dev-vm-1|172.17.0.5 \n"
    "ccce49f2061c|/mcp-hub-seat-dev-vm-1|172.17.0.2 \n"
    "09449ef3d43c|/vps-hetzner-cap-dev-vm-1|172.17.0.4 172.18.0.4 \n"
)


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
    assert cmap["172.17.0.5"] == ("c89fd45af06e", "duo-pod-dev-vm-1")
    # a container on two networks is reachable at either address
    assert cmap["172.18.0.4"] == ("09449ef3d43c", "vps-hetzner-cap-dev-vm-1")
    assert len(cmap) == 4


@pytest.mark.parametrize("junk", ["", "garbage", "onlyid|", "|/name|1.2.3.4", "id|/|1.2.3.4"])
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
    assert not authorised("172.17.0.5", "mcp-hub-seat-dev-vm-1", cmap)


def test_unknown_address_is_refused():
    cmap = parse_container_map(INSPECT)
    assert not verify_peer("172.17.0.99", "duo-pod-dev-vm-1", cmap)


def test_empty_roster_fails_closed():
    # "I could not tell who was asking" must never resolve to "go ahead".
    assert not authorised("172.17.0.5", "duo-pod-dev-vm-1", {})


def test_authorised_requires_both_roster_and_peer_identity():
    cmap = parse_container_map(INSPECT)
    assert authorised("172.17.0.5", "duo-pod-dev-vm-1", cmap)
    assert not authorised("172.17.0.5", "not-a-seat", cmap)
