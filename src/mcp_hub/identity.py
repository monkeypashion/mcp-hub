"""Hub-signed identity assertions — a caller name another service can trust.

Brain (and any later service) needs to know WHO is calling it, and a name
the caller types is a claim, not an identity. The hub already knows better:
a session bound to a name grades `session-verified` (see _attribution).
This module lets the hub put that verdict in writing — a short-lived JWT
signed with the hub's own Ed25519 key — so a service can verify it without
asking the hub and without trusting the caller.

Design, agreed in #fleet-v2 (2026-10-06, brain + mcp-hub; operator's word
2026-10-07 by voice, "please create the key"):

- ASYMMETRIC. The hub signs with a private key that never leaves it.
  Verifiers hold only the public half, so a leaked verifier cannot mint an
  identity.
- NOBODY HANDLES THE SECRET. The hub generates its own key on first use,
  next to its database on the data volume, mode 0600. It is never printed,
  never sent, never typed into a brief. The "never put a secret in a brief
  or an input" rule holds because there is nothing to put.
- ONLY VERIFIED CALLERS GET ONE. An assertion is issued only when the
  calling session is bound to the name it asks about (session-verified or
  operator-verified). An unbound caller is refused, loudly; a hub that
  signed `asserted` names would launder a claim into an identity.
- SHORT-LIVED AND SINGLE-AUDIENCE. 5 minutes, one `aud`, a unique `jti` so a
  verifier can keep a replay cache within the window.

Standard JWT (alg EdDSA), so a verifier needs only PyJWT + the public key:

    jwt.decode(token, public_pem, algorithms=["EdDSA"],
               audience="brain", issuer="mcp-hub")
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ISSUER = "mcp-hub"
ASSERTION_TTL_SECONDS = 300
KEY_FILENAME = "identity-ed25519.pem"
VERIFIED_GRADES = ("session-verified", "operator-verified")


def key_path(db_path: Path) -> Path:
    """The key lives beside the database, so it persists on the same volume
    and across every redeploy the database survives."""
    return Path(db_path).resolve().parent / KEY_FILENAME


def _load_or_create(path: Path) -> Ed25519PrivateKey:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL: two processes racing first use must not each write a key
        # and leave verifiers holding the loser's public half.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _load_or_create(path)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return key
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def public_pem(db_path: Path) -> str:
    key = _load_or_create(key_path(db_path))
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def key_id(db_path: Path) -> str:
    """A fingerprint of the PUBLIC key, so a verifier can tell which key
    signed a token if the hub ever rotates."""
    return hashlib.sha256(public_pem(db_path).encode()).hexdigest()[:16]


def issue(db_path: Path, subject: str, grade: str, audience: str,
          now: float | None = None) -> str:
    if grade not in VERIFIED_GRADES:
        raise PermissionError(grade)
    now = time.time() if now is None else now
    key = _load_or_create(key_path(db_path))
    claims = {
        "iss": ISSUER,
        "sub": subject,
        "aud": audience,
        "grade": grade,
        "iat": int(now),
        "exp": int(now) + ASSERTION_TTL_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, key, algorithm="EdDSA",
                      headers={"kid": key_id(db_path)})
