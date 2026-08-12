"""RA's contract test for the container-credential policy.

Authored by reliable-ai rather than by the lane that wrote the guard, on the
principle that the party who writes a control should not also write the proof
it works. `tests/test_spec_guard.py` already covers the policy's happy path
and its headline refusals; this file is deliberately the adversarial half —
the cases where a guard of this shape typically passes while not holding.

Every test here states the input that makes the check FAIL, up front. A guard
that cannot produce a refusal is not a control, and a refusal suite that never
demonstrates a pass cannot tell "correctly rejected" from "rejects
unconditionally" from "the harness never reached the guard" — so the refusal
cases are PAIRED with a passing sibling in the same test wherever the pairing
is what carries the meaning.

Scope, stated where the result is read: this bounds what a seat SPEC
DECLARES. It does not bound what a process does once running, nor what a
credential can do once mounted. All three are separate properties and none
implies another — which is the substitution (`:ro` bounds the FILE, not the
CAPABILITY) that this policy exists to prevent, so its own test suite should
not reproduce it.
"""

from pathlib import Path

import pytest

import mcp_hub.spec_guard as _spec_guard
from mcp_hub.spec_guard import check_credential_policy


def test_the_module_under_test_is_the_one_in_this_checkout():
    """The control on this file's own instrument, and it is not decorative.

    Authoring this suite, I mutation-tested it four ways and all four
    mutations passed — because the venv's editable install resolves
    `mcp_hub` to a DIFFERENT checkout, so I had been editing a file nothing
    imported. Four green mutation runs told me nothing at all, and would
    have shipped a suite I believed was falsifiable.

    This asserts the guard being exercised lives in the tree these tests
    live in. If it fails, the suite is measuring another copy and every
    result above it is void.
    """
    imported = Path(_spec_guard.__file__).resolve()
    here = Path(__file__).resolve().parents[1] / "src" / "mcp_hub" / "spec_guard.py"
    assert imported == here.resolve(), (
        f"tests import {imported}, but this checkout's guard is {here}. "
        "The suite is exercising a different copy — mutate that one and "
        "these tests will not notice. Run with PYTHONPATH=<repo>/src, or "
        "reinstall the package from this tree."
    )

# The counter-list, as literals rather than as a sentence. "Credentials
# refuse" is a claim; ANTHROPIC_API_KEY refusing is a check. These are the
# real names the estate carries, so the policy is bound to the actual threat
# rather than to a placeholder that would survive a rename.
REAL_CREDENTIAL_NAMES = [
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "DATABASE_URL",
    "COOLIFY_API_TOKEN",
    "GITHUB_TOKEN",
    "CODESPACE_DREAMTEAM_SERVICE_API_KEY",
]

DREAMTEAM_GH = "/home/monke/.config/gh-accounts/dreamteam-ai-labs"
MONKEYPASHION_GH = "/home/monke/.config/gh-accounts/monkeypashion"


def _adopting(**over):
    """A minimal policy-adopting spec; `allowed_env: []` adopts both halves."""
    spec = {"allowed_env": [], "env_from_host": [], "volumes": []}
    spec.update(over)
    return spec


# ── the counter-list, enumerated ────────────────────────────────────────


@pytest.mark.parametrize("name", REAL_CREDENTIAL_NAMES)
def test_each_real_credential_name_refuses_when_undeclared(name):
    """FAILS if the subset check is neutered: each name must be NAMED back.

    Paired: the same spec with the name declared must pass, so a guard that
    refuses everything cannot satisfy this test.
    """
    refused = check_credential_policy(_adopting(env_from_host=[name]))
    assert refused, f"{name} rode in undeclared"
    assert "REFUSED" in refused
    assert name in refused, "the refusal must name the offending item"

    allowed = check_credential_policy(
        {"allowed_env": [name], "env_from_host": [name], "volumes": []})
    assert allowed is None, "declaring the name must let it through"


def test_one_smuggled_name_among_approved_ones_still_refuses():
    """Selective absence beside present tokens — the pairing that proves the
    check reads each entry rather than the list's length or its first item."""
    spec = _adopting(
        allowed_env=["A", "B", "C"],
        env_from_host=["A", "B", "ANTHROPIC_API_KEY", "C"],
    )
    refused = check_credential_policy(spec)
    assert refused and "ANTHROPIC_API_KEY" in refused
    assert "A" not in refused.replace("ANTHROPIC_API_KEY", ""), (
        "only the offending name should be reported"
    )


# ── absent is not empty: the exemption, tested where it bites ───────────


def test_empty_allowed_env_ADOPTS_and_refuses_everything_offered():
    """The exclusion tested for the OTHER reason.

    A pre-policy spec (declaring NEITHER field) passes untouched — a
    deliberate exemption, so that legacy seats do not all refuse on their
    next recreate. The risk in that exemption is a reader concluding "an
    empty list behaves like no list". It must not: `allowed_env: []` is
    adoption with nothing approved, and every name offered must refuse.
    """
    pre_policy = {"env_from_host": ["ANTHROPIC_API_KEY"], "volumes": ["/x:/y"]}
    assert check_credential_policy(pre_policy) is None, (
        "a pre-policy spec must pass untouched"
    )

    adopted_empty = {"allowed_env": [], "env_from_host": ["ANTHROPIC_API_KEY"]}
    refused = check_credential_policy(adopted_empty)
    assert refused and "ANTHROPIC_API_KEY" in refused, (
        "allowed_env: [] is adoption with nothing approved — absent != empty"
    )


def test_declaring_only_mounts_still_enforces_the_env_half():
    """Half-adoption would let the undeclared half deliver what the declared
    half refuses. Asserted from the mounts side; the guard's own suite
    asserts it from the env side."""
    spec = {
        "allowed_mounts": [
            {"path": "/host/x", "why": "w", "scopes": "none — not a "
             "credential artifact"}
        ],
        "env_from_host": ["ANTHROPIC_API_KEY"],
    }
    refused = check_credential_policy(spec)
    assert refused and "ANTHROPIC_API_KEY" in refused


# ── the gh identity: which one, not whether ─────────────────────────────


def test_a_different_gh_identity_does_not_satisfy_the_declared_one():
    """The estate boundary that has arrived three times in different clothes.

    There are several gh config dirs on the host and they are not
    interchangeable: the dreamteam one is the approved identity, while
    `monkeypashion` carries `repo` + `delete_repo` and has a standing
    prohibition on being used for dreamteam work. A policy that approved
    "the gh config" generically would be satisfied by the prohibited one —
    silently, and maximally.
    """
    spec = {
        "allowed_mounts": [
            {"path": DREAMTEAM_GH, "ro": True, "why": "codespace ops",
             "scopes": "codespace"}
        ],
        "volumes": [f"{MONKEYPASHION_GH}:/seat/gh:ro"],
    }
    refused = check_credential_policy(spec)
    assert refused and MONKEYPASHION_GH in refused, (
        "the wrong identity satisfied a declaration made for another"
    )

    spec["volumes"] = [f"{DREAMTEAM_GH}:/seat/gh:ro"]
    assert check_credential_policy(spec) is None, (
        "the declared identity must still be allowed — otherwise this test "
        "passes against a guard that refuses every mount"
    )


def test_a_path_that_merely_starts_with_a_declared_one_does_not_match():
    """Prefix confusion: `/host/gh` declared must not admit `/host/ghost`."""
    spec = {
        "allowed_mounts": [
            {"path": "/host/gh", "ro": True, "why": "w", "scopes": "codespace"}
        ],
        "volumes": ["/host/ghost:/seat/gh:ro"],
    }
    refused = check_credential_policy(spec)
    assert refused and "/host/ghost" in refused


def test_a_trailing_slash_is_the_same_path_not_a_new_one():
    """Normalisation must not become a bypass in either direction."""
    spec = {
        "allowed_mounts": [
            {"path": "/host/gh/", "ro": True, "why": "w", "scopes": "codespace"}
        ],
        "volumes": ["/host/gh:/seat/gh:ro"],
    }
    assert check_credential_policy(spec) is None


# ── ro is the default, not an opt-in ────────────────────────────────────


def test_an_entry_omitting_ro_still_requires_ro_on_the_volume():
    """`ro` defaults to True. An entry that simply doesn't mention it must
    not become the writable case — the default is where a policy quietly
    loosens."""
    spec = {
        "allowed_mounts": [
            {"path": "/host/gh", "why": "w", "scopes": "codespace"}
        ],
        "volumes": ["/host/gh:/seat/gh"],
    }
    refused = check_credential_policy(spec)
    assert refused and ":ro" in refused

    spec["volumes"] = ["/host/gh:/seat/gh:ro"]
    assert check_credential_policy(spec) is None


def test_ro_must_be_a_flag_not_a_substring_of_the_destination():
    """`:ro` is checked as a mount option, so a destination containing the
    letters must not satisfy it."""
    spec = {
        "allowed_mounts": [
            {"path": "/host/gh", "why": "w", "scopes": "codespace"}
        ],
        "volumes": ["/host/gh:/seat/rondom"],
    }
    refused = check_credential_policy(spec)
    assert refused and ":ro" in refused


# ── the scopes field: un-skippable, and only ever a claim ───────────────


@pytest.mark.parametrize("scopes", ["", "   ", None])
def test_a_mount_entry_with_blank_scopes_is_malformed(scopes):
    """`:ro` bounds the file, not the capability — so the capability is
    stated where the mount is, and a blank statement is not a statement."""
    spec = {"allowed_mounts": [
        {"path": "/host/gh", "why": "w", "scopes": scopes}]}
    refused = check_credential_policy(spec)
    assert refused and "scopes" in refused


def test_a_non_credential_mount_states_that_rather_than_omitting_it():
    """The escape hatch must be an assertion, not a silence: a mount that
    carries no token says so, and that sentence is what a reviewer reads."""
    spec = {
        "allowed_mounts": [
            {"path": "/host/data", "ro": True, "why": "fixture inputs",
             "scopes": "none — not a credential artifact"}
        ],
        "volumes": ["/host/data:/seat/data:ro"],
    }
    assert check_credential_policy(spec) is None


def test_scopes_is_a_claim_by_the_author_not_a_measurement():
    """Documents a LIMIT of this guard rather than a behaviour of it.

    A spec may declare `scopes: "codespace"` for an artifact whose token in
    fact carries `repo` + `delete_repo`, and the guard will pass it: nothing
    here reads the credential. The field makes the grant REVIEWABLE, not
    true. If a future materialize can verify declared-vs-actual scopes, this
    test should be replaced by one that fails on the mismatch.
    """
    understated = {
        "allowed_mounts": [
            {"path": DREAMTEAM_GH, "ro": True, "why": "codespace ops",
             "scopes": "codespace"}
        ],
        "volumes": [f"{DREAMTEAM_GH}:/seat/gh:ro"],
    }
    assert check_credential_policy(understated) is None, (
        "if this now refuses, the guard has gained scope verification and "
        "this test is obsolete — replace it, do not delete it"
    )


# ── malformed declarations refuse rather than degrade ───────────────────


@pytest.mark.parametrize("allowed_env", ["NOT_A_LIST", {"a": 1}, [1, 2]])
def test_a_malformed_allowed_env_refuses(allowed_env):
    """A policy field that can be mis-typed into inertness is not a policy.
    The dangerous reading is a string: `"ABC"` is iterable, and a membership
    test against it would silently approve every substring."""
    refused = check_credential_policy(
        {"allowed_env": allowed_env, "env_from_host": ["ANTHROPIC_API_KEY"]})
    assert refused and "allowed_env" in refused


def test_a_malformed_allowed_mounts_refuses():
    refused = check_credential_policy({"allowed_mounts": {"path": "/x"}})
    assert refused and "allowed_mounts" in refused


def test_an_entry_without_a_path_refuses():
    refused = check_credential_policy(
        {"allowed_mounts": [{"why": "w", "scopes": "none"}]})
    assert refused and "path" in refused


# ── the suite's own positive control ────────────────────────────────────


def test_the_guard_can_say_yes_at_all():
    """The control on this file's instrument.

    Every other test above asserts a refusal. If the guard were broken-closed
    — refusing unconditionally — all of them would still pass. This is the
    one that fails in that case.
    """
    fully_declared = {
        "allowed_env": ["CODESPACE_DREAMTEAM_SERVICE_API_KEY"],
        "env_from_host": ["CODESPACE_DREAMTEAM_SERVICE_API_KEY"],
        "allowed_mounts": [
            {"path": DREAMTEAM_GH, "ro": True, "why": "codespace create/delete",
             "scopes": "codespace"}
        ],
        "volumes": [f"{DREAMTEAM_GH}:/seat/gh:ro",
                    "seat-memory-x:/home/seat/.claude"],
    }
    assert check_credential_policy(fully_declared) is None
