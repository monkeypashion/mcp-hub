"""Hub-signed identity assertions (identity.py).

What must hold, each with a cheaper wrong version:
  A VERIFIED NAME ONLY — an unbound caller is refused; signing `asserted`
  names would turn a claim into an identity.
  A VERIFIER NEEDS ONLY THE PUBLIC KEY — and a token for one audience, or
  past its expiry, or signed by another key, does not verify.
  THE KEY IS THE HUB'S — generated once, 0600, stable across restarts.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import jwt
import pytest

from mcp_hub import identity
from mcp_hub.server import create_server


class _FakeSess:
    _write_stream = object()

    async def send_ping(self):
        return None


class _FakeCtx:
    def __init__(self, session):
        self.session = session


def _text(result) -> str:
    if isinstance(result, str):
        return result
    for block in getattr(result, "content", None) or result:
        if hasattr(block, "text"):
            return block.text
    return str(result)


async def _call(server, name, args, ctx=None):
    return _text(await server._tool_manager.call_tool(name, args, context=ctx))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "hub.db"


@pytest.fixture
def server(db):
    return create_server(db_path=db)


async def _bound(server, name="alice"):
    await _call(server, "register", {"name": name, "project": "p"})
    sess = _FakeSess()
    server._hub_registry.bind(name, sess)
    return _FakeCtx(sess)


async def _public(server) -> str:
    return json.loads(await _call(server, "identity_public_key", {}))["public_key_pem"]


async def test_a_bound_caller_gets_a_token_that_verifies_with_the_public_key(server):
    ctx = await _bound(server)
    token = await _call(server, "identity_assertion",
                        {"agent_name": "alice", "audience": "brain"}, ctx)
    claims = jwt.decode(token, await _public(server), algorithms=["EdDSA"],
                        audience="brain", issuer="mcp-hub")
    assert claims["sub"] == "alice"
    assert claims["grade"] == "session-verified"
    assert claims["exp"] - claims["iat"] == identity.ASSERTION_TTL_SECONDS
    assert claims["jti"]


async def test_an_unbound_caller_is_refused(server):
    await _call(server, "register", {"name": "alice", "project": "p"})
    # No context at all (the stop hook's ephemeral client), and a session
    # that is bound to nobody: neither can prove the name.
    for ctx in (None, _FakeCtx(_FakeSess())):
        out = await _call(server, "identity_assertion",
                          {"agent_name": "alice", "audience": "brain"}, ctx)
        assert out.startswith("REFUSED"), out
        assert "." not in out.split()[0]  # no token shape slipped through


async def test_a_session_cannot_get_a_token_for_someone_else(server):
    ctx = await _bound(server, "alice")
    await _call(server, "register", {"name": "bob", "project": "p"})
    out = await _call(server, "identity_assertion",
                      {"agent_name": "bob", "audience": "brain"}, ctx)
    assert "REFUSED" in out


async def test_wrong_audience_expired_or_foreign_key_does_not_verify(server, db, tmp_path):
    ctx = await _bound(server)
    token = await _call(server, "identity_assertion",
                        {"agent_name": "alice", "audience": "brain"}, ctx)
    pub = await _public(server)
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(token, pub, algorithms=["EdDSA"], audience="other",
                   issuer="mcp-hub")
    old = identity.issue(db, "alice", "session-verified", "brain",
                         now=time.time() - 3600)
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(old, pub, algorithms=["EdDSA"], audience="brain",
                   issuer="mcp-hub")
    (tmp_path / "other").mkdir()
    other_hub = create_server(db_path=tmp_path / "other" / "hub.db")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, await _public(other_hub), algorithms=["EdDSA"],
                   audience="brain", issuer="mcp-hub")


async def test_the_key_is_private_to_the_hub_and_stable(server, db):
    first = await _public(server)
    path = identity.key_path(db)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # A restart (a new server on the same volume) keeps the same key, or
    # every verifier would break at every deploy.
    again = await _public(create_server(db_path=db))
    assert again == first
    # What is published is the public half only.
    assert "PRIVATE" not in first


async def test_audience_is_required(server):
    ctx = await _bound(server)
    out = await _call(server, "identity_assertion",
                      {"agent_name": "alice", "audience": " "}, ctx)
    assert out.startswith("REFUSED")
