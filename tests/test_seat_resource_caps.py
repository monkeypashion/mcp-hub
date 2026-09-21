"""Seat resource caps — and the rule that a declared bound must BE a bound.

Seats were created with no `--memory`, no `--cpus` and no `--pids-limit`:
`docker create` carried none of them and nothing anywhere in `src/` mentioned
them. That was never decided, it was just never written — and it is not a
property of any one machine. An uncapped seat is uncapped on the dev box too.

Why it matters beyond capacity: the kernel OOM killer scores by FOOTPRINT, so
the runaway seat is the process most likely to evict the biggest innocent
neighbour rather than to die alone.

🔴 THE FAILURE THIS FILE EXISTS TO PREVENT, named by slipstream-dev-vm-1
before the feature was built: *"a cap that is declared and never applied is
worse than no cap, because it reads as a bound"*. Docker makes that failure
reachable through the FRONT DOOR — measured against the live daemon
2026-09-21, `--memory=0`, `--cpus=0`, `--pids-limit=0` and `--pids-limit=-1`
are all ACCEPTED and all mean UNLIMITED. So the guard refuses the unlimited
spellings, and these tests assert the refusal in both directions: a real cap
must pass, or a guard that refuses everything would look identical here.

The argv tests are pure string assembly and reach no docker. The one test
that proves the flag actually BINDS is marked `docker` and skipped by
default — see its own docstring for why it is not a unit test.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from mcp_hub.edge import DockerExecutor
from mcp_hub.spec_guard import check_limits, validate_spec

# ---- the caps reach the container --------------------------------------

def test_absent_caps_emit_no_flags():
    """Every seat that existed before caps did. Absent must stay absent —
    a default that quietly capped them would change running seats on their
    next recreate, with nothing telling the operator why."""
    argv = DockerExecutor.create_argv("seat-1", {"image": "busybox"})
    joined = " ".join(argv)
    assert "--memory" not in joined
    assert "--cpus" not in joined
    assert "--pids-limit" not in joined


def test_declared_caps_reach_docker_create():
    argv = DockerExecutor.create_argv("seat-1", {
        "image": "busybox", "memory": "512m", "cpus": "1.5",
        "pids_limit": 256,
    })
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert argv[argv.index("--pids-limit") + 1] == "256"


def test_caps_land_before_the_image():
    """A flag after the image name is an argument to the CONTAINER, not to
    docker — it would be silently handed to the entrypoint instead of
    capping anything, which is this file's whole failure mode in a new
    costume."""
    argv = DockerExecutor.create_argv("seat-1", {
        "image": "busybox", "memory": "512m", "command": ["sleep", "1"]})
    assert argv.index("--memory") < argv.index("busybox")


# ---- zero is not a cap --------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("memory", "0"),
    ("memory", 0),
    ("cpus", "0"),
    ("cpus", 0.0),
    ("pids_limit", 0),
    ("pids_limit", -1),
])
def test_an_unlimited_spelling_is_refused(field, value):
    """All six are accepted by docker and all six mean UNLIMITED. A spec
    carrying one reads as capped to every reviewer and is bounded by
    nothing."""
    bad = check_limits({field: value})
    assert bad and "UNLIMITED" in bad.upper()


@pytest.mark.parametrize("field,value", [
    ("memory", "512m"),
    ("memory", "2g"),
    ("cpus", "1.5"),
    ("cpus", 2),
    ("pids_limit", 256),
    ("pids_limit", "256"),
])
def test_a_real_cap_passes(field, value):
    """The other direction, and it is not a formality: a guard that refused
    everything would pass every test above while making the feature
    unusable."""
    assert check_limits({field: value}) is None


def test_absent_caps_pass_untouched():
    assert check_limits({"image": "busybox"}) is None
    assert check_limits({}) is None


@pytest.mark.parametrize("field,value", [
    ("memory", "lots"),
    ("memory", "512 megabytes"),
    ("cpus", "two"),
    ("pids_limit", "many"),
])
def test_an_unparseable_cap_is_refused_not_dropped(field, value):
    """Dropping it would be the same defect as accepting 0 — the spec would
    still SAY capped. The console's `on_who` door had exactly this shape:
    four spellings accepted, all four silently discarded, 200 each time."""
    assert check_limits({field: value}) is not None


# ---- the guard is actually wired in -------------------------------------

def test_validate_spec_runs_the_cap_check():
    """check_limits being correct buys nothing if no door calls it."""
    assert validate_spec({"image": "busybox", "memory": "0"}) is not None
    assert validate_spec({"image": "busybox", "memory": "512m"}) is None


def test_a_patch_of_one_cap_is_still_checked():
    """PATCH validates key-by-key. A patch sending only `cpus` must meet the
    same rule as a create, or the zero-trap reopens on the update path."""
    assert validate_spec({"cpus": "0"}, keys={"cpus"}) is not None
    assert validate_spec({"cpus": "1.5"}, keys={"cpus"}) is None


def test_a_patch_that_touches_no_cap_does_not_trip_on_a_stored_one():
    """A spec already holding a cap, patched elsewhere, must not be
    re-validated into a refusal by a key the caller never sent."""
    assert validate_spec({"brief": "hello"}, keys={"brief"}) is None


# ---- the must-fire: the cap BINDS ---------------------------------------

@pytest.mark.docker
def test_a_memory_cap_actually_kills_the_container():
    """THE MUST-FIRE. Everything above proves the flag is ASSEMBLED; only
    this proves it BINDS.

    Not a unit test and not run by default: it needs a real daemon, pulls
    `alpine`, and takes a few seconds. It is here so the claim "seats can be
    capped" has one test behind it that would fail if docker ever stopped
    honouring the flag — the difference between a bound and a string in an
    argv list.

    ⚠️ It opts IN via `MCP_HUB_DOCKER_TESTS=1` and SKIPS WITH A REASON rather
    than being deselected by a marker expression in `addopts`. Those are not
    equivalent: a global `-m "not docker"` makes every future full-suite run
    silently smaller, and "2841 passed" would stop meaning what it says. A
    named skip stays visible in the report — the instrument says it did not
    measure, instead of reading clean because it never ran.

    `tail /dev/zero` grows its buffer without limit. Under a 64m cap the OOM
    killer takes it: SIGKILL, exit 137. Measured 2026-09-21 — uncapped, the
    same command reports "out of memory" and KEEPS RUNNING, competing against
    the whole host, which is precisely the runaway this feature bounds.

    No seat image, no hub, no fleet socket: a throwaway `--rm alpine`.
    """
    if os.environ.get("MCP_HUB_DOCKER_TESTS") != "1":
        pytest.skip(
            "needs a real docker daemon — set MCP_HUB_DOCKER_TESTS=1 to run "
            "the cap-binds must-fire"
        )
    if not shutil.which("docker"):
        pytest.skip("no docker on this machine")
    argv = DockerExecutor.create_argv("cap-probe", {
        "image": "alpine", "memory": "64m",
        "command": ["sh", "-c", "tail /dev/zero"],
    })
    # Run what create_argv BUILT, so the test cannot pass against an argv
    # the executor would never emit.
    run = ["docker", "run", "--rm", "--memory-swap=64m"] + argv[2:]
    run.remove("--name")
    run.remove("cap-probe")
    done = subprocess.run(run, capture_output=True, timeout=120)
    assert done.returncode == 137, (
        f"expected SIGKILL from the OOM killer, got {done.returncode} — "
        "the cap did not bind"
    )
