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

    if _wanted("repo_mount"):
        bad = check_repo_mount(spec.get("repo_mount"))
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

# 🔴 THE GAP THE ABOVE LIST DID NOT COVER, and why it is separate.
#
# The prefix list refuses SYSTEM paths. `/home` was not on it, so a spec could
# mount the operator's entire home directory — ssh keys, `~/.claude` (hooks and
# credentials cache), `~/.mcp-hub/edge-env` (every seat credential on the box) —
# into a container running in bypassPermissions. Found 2026-08-11 while
# designing `repo_mount` (docs/seat-repo-access.md).
#
# It was DORMANT only because no seat mounted a host path at all. The moment
# host mounts become a deliberate feature, it stops being dormant — which is
# why it closes in the same change rather than being filed.
#
# Host-independent by construction: the hub validating a spec and the edge
# re-validating it before materialize run with DIFFERENT homes, so a rule
# phrased as "$HOME" would mean two different things. These are shapes.
_HOME_ROOTS = ("/home", "/Users")

# A path COMPONENT with one of these names carries credentials or the
# configuration that executes code on someone's behalf. Matching on the
# component (not the prefix) refuses `/home/me/.ssh` and any parent that would
# contain it. Deliberately blunt, in the same spirit as the secret scanner: a
# false positive costs one reworded mount, a false negative costs the estate.
_SENSITIVE_COMPONENTS = frozenset({
    ".ssh", ".claude", ".mcp-hub", ".aws", ".gnupg", ".docker", ".kube",
    ".config", ".gitconfig", ".netrc", ".npmrc",
})

# Where a seat's claude state lives INSIDE the container. Defined here rather
# than in edge.py because BOTH the guard and the executor need it and there
# must be one of it: a `repo_mount` landing on this path would shadow the
# memory volume, which is exactly the durability bug of 2026-08-06 rebuilt
# from new parts. edge.py imports it from here.
SEAT_STATE_DIR = "/home/seat/.claude"


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
        parts = [p for p in norm.split("/") if p]
        # `/home`, `/Users` — every account on the box — and `/home/<user>`,
        # one whole account. A path DEEPER than that is a normal project
        # directory and stays allowed; refusing those would forbid the
        # legitimate case this guard exists to make safe.
        if norm in _HOME_ROOTS or (len(parts) == 2
                                   and "/" + parts[0] in _HOME_ROOTS):
            return (
                f"REFUSED: '{src}' is a whole home directory. It carries ssh "
                "keys, ~/.claude (hooks the seat could rewrite) and "
                "~/.mcp-hub/edge-env (every seat credential on this host) — "
                "mounting it hands the container the operator's identity. "
                "Mount the specific project directory instead, or declare "
                "`repo_mount` and let the edge place the checkout."
            )
        hit = next((p for p in parts if p in _SENSITIVE_COMPONENTS), None)
        if hit:
            return (
                f"REFUSED: '{src}' contains '{hit}', which holds credentials "
                "or configuration that runs code on the operator's behalf. "
                "A seat runs in bypassPermissions on the premise that the "
                "container contains it; this mount would make that false."
            )
    return None


# --- repo_mount: the operator names a REPO, never a path -------------------

# The allowlist is structural rather than a list: the operator supplies an
# `org/repo`, and the EDGE derives the host directory under its own managed
# root. There is no operator-supplied host path to escape from, so "resolves
# outside the managed root" is unreachable by construction instead of being
# a rule that has to hold. Everything below guards the two things that DO
# travel: the repo name (which becomes a path component) and the container
# destination.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def check_repo_mount(repo_mount: object) -> str | None:
    """Validate `spec.repo_mount`. Returns a refusal, or None."""
    if repo_mount is None:
        return None
    if not isinstance(repo_mount, dict):
        return (
            "REFUSED: spec.repo_mount must be an object like "
            '{"repo": "org/name", "ref": "main", "dest": "/home/seat/work"}'
        )
    repo = str(repo_mount.get("repo") or "").strip()
    if not repo:
        return "REFUSED: spec.repo_mount needs a 'repo' (org/name)"
    if not _REPO_RE.match(repo):
        # `..` and `/` are the path-traversal shapes; a leading `-` would be
        # read as a flag by the git argv the edge builds.
        return (
            f"REFUSED: repo '{repo}' is not a plain 'org/name'. The name "
            "becomes a directory under the edge's managed root, so anything "
            "that could climb out of it is refused rather than sanitized."
        )
    if any(part in (".", "..") for part in repo.split("/")):
        return f"REFUSED: repo '{repo}' contains a path-traversal component"

    ref = str(repo_mount.get("ref") or "").strip()
    if ref:
        if ref.startswith("-"):
            # Not shell injection — the edge uses argv, no shell. A ref
            # beginning with `-` is read by git as an OPTION, which is its own
            # way of making the command do something else entirely.
            return (
                f"REFUSED: ref '{ref}' starts with '-', which git would read "
                "as an option rather than a revision"
            )
        if any(c.isspace() for c in ref):
            return f"REFUSED: ref '{ref}' contains whitespace"

    dest = str(repo_mount.get("dest") or "").strip()
    if dest:
        if not dest.startswith("/"):
            return (
                f"REFUSED: repo_mount dest '{dest}' must be an absolute path "
                "inside the container"
            )
        norm = dest.rstrip("/") or "/"
        if norm == "/":
            return "REFUSED: repo_mount dest may not be the container root"
        state = SEAT_STATE_DIR.rstrip("/")
        if norm == state or norm.startswith(state + "/"):
            return (
                f"REFUSED: repo_mount dest '{dest}' is inside the seat's "
                f"state directory ({SEAT_STATE_DIR}), where the memory volume "
                "is mounted. A checkout there would shadow the seat's memory "
                "and transcripts — the durability failure of 2026-08-06 in a "
                "new costume."
            )
    return None


def check_credential_policy(spec: dict) -> str | None:
    """The container-credential policy, enforced where it can still say no.

    PROPOSAL-container-credential-policy-2026-08-12: the credential stays
    outside the container; the container gets the outcome. A seat that
    declares `allowed_env` / `allowed_mounts` adopts the policy, and from
    then on `env_from_host` and every host-path mount must be a subset of
    what it declared — an undeclared name refuses the materialize rather
    than riding in on it.

    Declaring EITHER field adopts BOTH halves. A half-adopted policy would
    let the undeclared half deliver what the declared half refuses, which
    is the `:ro`-bounds-the-file substitution wearing yet another coat.

    ⚠️ `scopes` is a documented LIMIT, not a verified behaviour: nothing
    here reads the mounted credential, so a spec may declare `codespace`
    for a token that in fact carries `repo`+`delete_repo`, and pass. The
    field makes a grant REVIEWABLE, not true — if materialize ever gains
    real scope verification, replace RA's matching contract case too
    (test_credential_policy_contract.py says the same from its side).

    A spec that declares NEITHER is a pre-policy spec and passes untouched:
    turning absent into empty would refuse every legacy seat on its next
    recreate with nothing telling the operator why — a migration that could
    be half-done, made mandatory. Adoption is per-seat and explicit.
    """
    allowed_env = spec.get("allowed_env")
    allowed_mounts = spec.get("allowed_mounts")
    if allowed_env is None and allowed_mounts is None:
        return None

    if allowed_env is not None and not (
            isinstance(allowed_env, list)
            and all(isinstance(n, str) for n in allowed_env)):
        return "REFUSED: spec.allowed_env must be a list of variable names"
    names = {str(n) for n in (allowed_env or [])}
    extra = [str(n) for n in (spec.get("env_from_host") or [])
             if str(n) not in names]
    if extra:
        return (
            f"REFUSED: env_from_host names {', '.join(sorted(extra))} — not "
            "in this seat's allowed_env. The approved list is the policy; "
            "grow the list (with the operator's word) rather than the env."
        )

    if allowed_mounts is not None and not isinstance(allowed_mounts, list):
        return "REFUSED: spec.allowed_mounts must be a list of objects"
    entries: dict[str, dict] = {}
    for m in (allowed_mounts or []):
        if not isinstance(m, dict) or not str(m.get("path") or "").strip():
            return (
                "REFUSED: each allowed_mounts entry must be an object with "
                "a host 'path'"
            )
        if not str(m.get("why") or "").strip() or not str(
                m.get("scopes") or "").strip():
            # The reason is the payload, and the scope question must be
            # answered in the spec — ':ro' bounds the FILE, not the
            # CAPABILITY, so the capability is stated where the mount is.
            # A non-credential mount states that: scopes: "none — not a
            # credential artifact".
            return (
                f"REFUSED: allowed_mounts entry '{m.get('path')}' needs a "
                "'why' and the token 'scopes' inside the mounted artifact "
                "(or 'none — not a credential artifact')"
            )
        entries[str(m["path"]).rstrip("/") or "/"] = m
    for v in (spec.get("volumes") or []):
        src, _, rest = str(v).partition(":")
        src = src.strip()
        if not src.startswith("/"):
            continue  # a NAMED volume — docker-managed, not a host path
        entry = entries.get(src.rstrip("/") or "/")
        if entry is None:
            return (
                f"REFUSED: host mount '{src}' is not in this seat's "
                "allowed_mounts. Every host path a policy-adopting seat "
                "mounts is declared, with its reason and its scopes."
            )
        if entry.get("ro", True) and "ro" not in rest.split(":"):
            return (
                f"REFUSED: allowed_mounts marks '{src}' read-only but the "
                "volume string does not carry ':ro'"
            )
    return None
