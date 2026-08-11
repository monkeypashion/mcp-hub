"""Ref envelopes — one identity mechanism for everything the hub can point at.

A ref names a thing: a hub message, a decision card, an agent, an external
work item. The design constraint (operator, 2026-08-11, no-regrets rule) is
that the core stays METHODOLOGY-NEUTRAL: this module knows what a scheme IS,
never what any particular scheme MEANS. `ra.feature/1` is a registered scheme
like any other — RA's own words: "I'd rather be one scheme among several than
the assumed default."

The hub's own artifacts go through the same mechanism (`hub.msg/1`,
`hub.decision/1`, …) and are registered FIRST — the hub dogfoods its own
envelope on every message it stores, so the adapter interface is exercised by
all traffic, not only by the occasional external ref.

## Canonical form

    scheme/version?key=value&key=value

Keys sorted, values percent-encoded, ONE encoder used for storage, display
and input alike. Why not JSON: refs are COPY-PASTED by agents into tool
parameters, where JSON's nested-quote escaping is an error factory — and a
graph whose node keys can be serialized two ways silently splits one node
into two, which makes the graph lie about connectivity. The parser is lenient
about field order on input and canonicalisation makes the orderings collapse.

Refusals in this module NAME the registered alternatives — the same rule as
broadcast's squadless refusal: an error that says only "no" sends the caller
hunting.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

__all__ = [
    "Ref",
    "RefError",
    "Scheme",
    "canonical",
    "parse_ref",
    "register_scheme",
    "registered_schemes",
]


class RefError(ValueError):
    """A ref that cannot be accepted. The message is operator-facing and
    names the rule or the registered alternatives — never a bare no."""


# scheme name: lowercase dotted words; version: a positive integer.
_SCHEME_RE = re.compile(r"^(?P<name>[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*)/(?P<ver>[1-9][0-9]*)$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Scheme:
    """A registered ref scheme — the adapter interface.

    `forbidden` maps a field name to the REASON it is refused, so the refusal
    can cite the contract that bans it (e.g. ra.feature/1 refuses `version`
    per FJ rule 2 — an item-version PIN is not the same thing as the scheme
    version in the envelope, and keeping them apart by construction is what
    stops the two ever being conflated).
    """

    name: str
    version: int
    required: frozenset
    optional: frozenset = frozenset()
    forbidden: dict = field(default_factory=dict)
    # Optional hooks, so an adapter can carry behaviour without the core
    # importing its semantics. Signature: resolve(conn, ref) -> dict.
    resolve: object = None

    @property
    def key(self) -> str:
        return f"{self.name}/{self.version}"


_REGISTRY: dict[str, Scheme] = {}


def register_scheme(scheme: Scheme) -> None:
    """Register a scheme. Public interface — external adapters use exactly
    this call, which is what the no-regrets test asserts: adding a scheme
    touches zero core files."""
    for f in scheme.required | scheme.optional:
        if not _KEY_RE.match(f):
            raise RefError(f"scheme {scheme.key!r}: field {f!r} is not a valid key")
    if scheme.key in _REGISTRY:
        raise RefError(f"scheme {scheme.key!r} is already registered")
    _REGISTRY[scheme.key] = scheme


def registered_schemes() -> list[str]:
    return sorted(_REGISTRY)


@dataclass(frozen=True)
class Ref:
    scheme: str  # "hub.msg/1"
    fields: tuple  # sorted ((key, value), ...) — hashable, order-canonical

    def get(self, key: str, default: str = "") -> str:
        return dict(self.fields).get(key, default)


def _scheme_of(key: str) -> Scheme:
    scheme = _REGISTRY.get(key)
    if scheme is None:
        raise RefError(
            f"unknown scheme {key!r} — registered: "
            f"{', '.join(registered_schemes()) or '(none)'}"
        )
    return scheme


def make_ref(scheme_key: str, **fields: object) -> Ref:
    """Build a validated ref from parts. Same validation as parse_ref."""
    return _validate(scheme_key, {k: str(v) for k, v in fields.items()})


def _validate(scheme_key: str, fields: dict) -> Ref:
    m = _SCHEME_RE.match(scheme_key)
    if not m:
        raise RefError(
            f"malformed scheme {scheme_key!r} — a scheme is name/version, "
            f"e.g. 'hub.msg/1' (the version names the CONTRACT, and is required)"
        )
    scheme = _scheme_of(scheme_key)
    for f, reason in scheme.forbidden.items():
        if f in fields:
            raise RefError(f"scheme {scheme.key!r} refuses field {f!r}: {reason}")
    missing = scheme.required - set(fields)
    if missing:
        raise RefError(
            f"scheme {scheme.key!r} requires {sorted(scheme.required)}; "
            f"missing {sorted(missing)}"
        )
    unknown = set(fields) - scheme.required - scheme.optional
    if unknown:
        raise RefError(
            f"scheme {scheme.key!r} does not define {sorted(unknown)} "
            f"(fields: {sorted(scheme.required | scheme.optional)})"
        )
    for k, v in fields.items():
        if not _KEY_RE.match(k):
            raise RefError(f"invalid field key {k!r}")
        if v == "":
            raise RefError(f"field {k!r} is empty — an empty half of an "
                           f"identity is a missing half")
    return Ref(scheme=scheme.key, fields=tuple(sorted(fields.items())))


def canonical(ref: Ref) -> str:
    """THE canonical string — storage key, display form and input form are
    all this one encoding. Keys sorted; values percent-encoded."""
    query = "&".join(
        f"{k}={urllib.parse.quote(v, safe='')}" for k, v in ref.fields
    )
    return f"{ref.scheme}?{query}"


def parse_ref(text: str) -> Ref:
    """Parse a canonical-form ref. Lenient about field ORDER (canonical()
    re-sorts, so two orderings collapse to one node); strict about
    everything else."""
    if not isinstance(text, str) or not text.strip():
        raise RefError("empty ref")
    text = text.strip()
    scheme_part, sep, query = text.partition("?")
    if not sep or not query:
        raise RefError(
            f"malformed ref {text!r} — canonical form is "
            f"scheme/version?key=value&key=value"
        )
    fields: dict = {}
    for pair in query.split("&"):
        k, eq, v = pair.partition("=")
        if not eq:
            raise RefError(f"malformed ref field {pair!r} in {text!r}")
        if k in fields:
            raise RefError(f"duplicate field {k!r} in {text!r}")
        fields[k] = urllib.parse.unquote(v)
    return _validate(scheme_part, fields)


# ---------------------------------------------------------------------------
# Hub-native schemes — registered FIRST, through the same public interface an
# external adapter uses. The hub's own traffic is the envelope's test bed;
# ra.feature/1 arrives as the FOURTH scheme, demonstrably unprivileged.
# ---------------------------------------------------------------------------

register_scheme(Scheme("hub.msg", 1, required=frozenset({"id"})))
register_scheme(Scheme("hub.decision", 1, required=frozenset({"card"})))
register_scheme(Scheme("hub.agent", 1, required=frozenset({"name"})))
register_scheme(Scheme("hub.channel", 1, required=frozenset({"name"})))
register_scheme(Scheme("hub.squad", 1, required=frozenset({"name"})))
