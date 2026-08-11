"""Seat-spec validation — the control plane holds no secrets (W2.3).

`cli.py` has asserted since briefs shipped that "the refusal below is worth
its false positives". There was no refusal below. This module is that
refusal, placed where it cannot be bypassed: the CLI is not the only writer —
`operator_api` is a plain HTTP client, and anything holding the operator
token can POST a spec directly.

Why the hub side is the real gate: a brief and its inputs are stored in the
hub's SQLite in PLAINTEXT and shipped to the seat as environment variables.
That is the same threat model that makes `--env-from-host` pass a NAME and
never a value. A key pasted into a brief is a key in the control plane's
database, readable by anything holding the operator token, and it will
outlive the conversation that produced it.

Two deliberate design choices:

- **The refusal NAMES the pattern and never echoes the match.** An error
  message is itself a place secrets go to die badly: logs, terminal
  scrollback, a pasted bug report. Saying "an AWS access key id" is enough
  to fix it; printing the key spreads it further.
- **Legacy content is not re-validated.** Clone and capsule-mint copy a spec
  that already exists, so a rule added today must not make a seat declared
  yesterday uncloneable. Only NEW operator-supplied content is checked.
"""

from __future__ import annotations

import re

# Filename rules mirror seat.py's container-side check EXACTLY (the escape
# refusal there exists because a `../` input could overwrite
# ~/.claude/settings.json and thereby install hooks). That check runs inside
# the container; a spec written straight to the API never reached it, so the
# same rule has to live at the write boundary too.
_MAX_BRIEF_BYTES = 64 * 1024
_MAX_INPUT_BYTES = 256 * 1024

# Each entry: (human name, compiled pattern). The name is what the refusal
# says; the match itself is never shown.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("a PEM private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("a GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("a JSON private_key field", re.compile(r'"private_key"\s*:\s*"')),
    (
        "an inline credential in a URL",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@"),
    ),
]

_ADVICE = (
    "The control plane holds NO secrets: briefs and inputs are stored in the "
    "hub's database in plaintext and shipped to the seat as environment "
    "variables. Pass the NAME of a credential with `--env-from-host` and let "
    "the edge inject the value, or mount it into the container."
)


def scan_secret(text: str, where: str) -> str | None:
    """Return a refusal naming the pattern, or None.

    `where` names the field for the operator ("brief", "input 'notes.md'"),
    so a refusal on a multi-input command says which file to fix.
    """
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return (
                f"REFUSED: the {where} appears to contain {name}. "
                f"{_ADVICE} (If this is a false positive — a sample, a "
                f"doc fragment — reword it; the guard is deliberately "
                f"blunt and the value of never leaking a real key is "
                f"higher than the cost of rephrasing one line.)"
            )
    return None


def check_input_name(name: str) -> str | None:
    """Mirror of seat.py's container-side filename rule, at the API boundary."""
    clean = name.strip()
    if not clean:
        return "REFUSED: an input filename may not be empty"
    if clean != _basename(clean) or clean.startswith(".") or "\\" in clean:
        return (
            f"REFUSED: input filename '{name}' could escape the inputs "
            "directory. Bare filenames only — a path that climbs out could "
            "overwrite the seat's own settings and install hooks."
        )
    return None


def _basename(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def validate_spec(spec: dict, *, keys: set[str] | None = None) -> str | None:
    """Validate the operator-supplied parts of a seat spec.

    `keys` limits the check to specific top-level keys — PATCH merges
    key-by-key, so only what the caller actually sent is validated. Passing
    None checks everything (a fresh create).

    Returns a refusal string, or None when the spec is acceptable.
    """
    if not isinstance(spec, dict):
        return "REFUSED: spec must be an object"

    def _wanted(k: str) -> bool:
        return keys is None or k in keys

    if _wanted("brief"):
        brief = spec.get("brief")
        if isinstance(brief, str) and brief:
            if len(brief.encode("utf-8")) > _MAX_BRIEF_BYTES:
                return (
                    f"REFUSED: brief is larger than {_MAX_BRIEF_BYTES} bytes "
                    "— it travels as a container environment variable"
                )
            found = scan_secret(brief, "brief")
            if found:
                return found

    if _wanted("inputs"):
        inputs = spec.get("inputs")
        if isinstance(inputs, dict):
            for fname, content in inputs.items():
                bad = check_input_name(str(fname))
                if bad:
                    return bad
                if not isinstance(content, str):
                    return (
                        f"REFUSED: input '{fname}' must be text — inputs are "
                        "UTF-8 only; mount a volume for binaries"
                    )
                if len(content.encode("utf-8")) > _MAX_INPUT_BYTES:
                    return (
                        f"REFUSED: input '{fname}' is larger than "
                        f"{_MAX_INPUT_BYTES} bytes"
                    )
                found = scan_secret(content, f"input '{fname}'")
                if found:
                    return found

    if _wanted("volumes"):
        bad = check_volumes(spec.get("volumes"))
        if bad:
            return bad

    return None


# --- the sandbox premise (W2.5, enforced in the same place) ----------------

# A seat runs with permissions.defaultMode: bypassPermissions on the stated
# grounds that THE CONTAINER IS THE SANDBOX — sound only while the container
# genuinely contains it: non-root, no host mounts beyond its own memory
# volume, and NO DOCKER SOCKET. Nothing enforced that; `spec.volumes` passed
# verbatim to `docker create`, so a spec mounting the socket silently
# falsified the premise the whole security posture rests on.
_FORBIDDEN_MOUNTS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
)
_SENSITIVE_PREFIXES = (
    "/etc",
    "/root",
    "/boot",
    "/sys",
    "/proc",
    "/var/run",
    "/run",
)


def check_volumes(volumes: object) -> str | None:
    """Refuse mounts that break the container-is-the-sandbox premise."""
    if not volumes:
        return None
    if not isinstance(volumes, list):
        return "REFUSED: spec.volumes must be a list of 'source:dest' strings"
    for v in volumes:
        src = str(v).split(":", 1)[0].strip()
        if not src.startswith("/"):
            continue  # a NAMED volume — docker-managed, not a host path
        norm = src.rstrip("/") or "/"
        if norm in _FORBIDDEN_MOUNTS:
            return (
                f"REFUSED: mounting '{src}' gives the seat control of the "
                "docker daemon, which makes 'the container is the sandbox' "
                "false — and that premise is the sole justification for the "
                "seat running in bypassPermissions mode. Container "
                "management is the edge's job, from outside."
            )
        if any(norm == p or norm.startswith(p + "/")
               for p in _SENSITIVE_PREFIXES):
            return (
                f"REFUSED: '{src}' is a host system path; mounting it "
                "breaks the containment the seat's permission mode assumes. "
                "Mount a named volume, or the seat's own workdir."
            )
    return None
