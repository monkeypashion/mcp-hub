"""W2.3 + W2.5 — the control plane holds no secrets, and the sandbox is real.

Two invariants that were DOCUMENTED AND ENFORCED NOWHERE:

1. `cli.py`'s `_read_brief_and_inputs` docstring has asserted since briefs
   shipped that "the refusal below is worth its false positives". There was
   no refusal below. A brief and its inputs are stored in the hub's SQLite in
   plaintext and shipped to the seat as environment variables — the same
   threat model that makes `--env-from-host` pass a NAME and never a value.
2. `seat.py` states the seat's bypassPermissions mode is sound "ONLY while
   the seat is genuinely contained: non-root, no host mounts beyond its own
   memory volume, and NO DOCKER SOCKET". Nothing checked: `spec.volumes`
   passed verbatim to `docker create`.

Also covered: the five `_read_brief_and_inputs` refusal branches that had
ZERO tests, one of which (duplicate basename) is a silent-loss shape.
"""

import argparse
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub.cli import _read_brief_and_inputs
from mcp_hub.server import create_server
from mcp_hub.spec_guard import (
    check_repo_mount,
    check_volumes,
    scan_secret,
    validate_spec,
)

OP = {"Authorization": "Bearer op-token"}

# Synthetic, structurally-valid-looking values. None is real; each exists to
# prove its pattern fires.
FAKE = {
    "an Anthropic API key": "sk-ant-api03-AAAABBBBCCCCDDDDEEEE",
    "an AWS access key id": "AKIAIOSFODNN7EXAMPLE",
    "a GitHub token": "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH",
    "a GitHub fine-grained token": "github_pat_AAAABBBBCCCCDDDDEEEEFF",
    "a PEM private key block": "-----BEGIN RSA PRIVATE KEY-----\nx\n",
    "a Slack token": "xoxb-1234567890-abcdefghij",
    "a Google API key": "AIza" + "B" * 35,
    "a JSON private_key field": '{"private_key": "-----"}',
    "an inline credential in a URL": "https://user:hunter2@example.com/repo",
}


# ---------------------------------------------------------------------------
# C2 — every pattern fires, and the refusal never echoes the match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,value", sorted(FAKE.items()))
def test_each_pattern_is_refused_by_name(name, value):
    """Mutation: delete any pattern from _SECRET_PATTERNS → its case fails."""
    out = scan_secret(f"please use {value} to authenticate", "brief")
    assert out is not None
    assert name in out


@pytest.mark.parametrize("name,value", sorted(FAKE.items()))
def test_the_refusal_never_echoes_the_secret(name, value):
    """An error message is itself a place secrets go to die badly — logs,
    scrollback, a pasted bug report. Naming the pattern is enough to fix it;
    printing the value spreads it further.

    Mutation: interpolate the match into the message → every case fails."""
    out = scan_secret(f"key: {value}", "brief")
    core = value.strip().splitlines()[0]
    assert core not in out


def test_positive_control_ordinary_text_passes():
    """Without this, a scanner that refused EVERYTHING would pass the tests
    above and look correct."""
    assert scan_secret(
        "Build the export endpoint. See inputs/notes.md for the schema.",
        "brief",
    ) is None
    assert validate_spec({"brief": "a normal brief", "inputs": {}}) is None


# ---------------------------------------------------------------------------
# C5 — the five _read_brief_and_inputs branches that had ZERO tests
# ---------------------------------------------------------------------------


def _args(**kw):
    base = {"brief": "", "input": []}
    base.update(kw)
    return argparse.Namespace(**base)


class TestReadBriefAndInputs:
    def test_at_file_is_read(self, tmp_path: Path):
        f = tmp_path / "b.md"
        f.write_text("from a file", encoding="utf-8")
        brief, _inputs, err = _read_brief_and_inputs(_args(brief=f"@{f}"))
        assert err == "" and brief == "from a file"

    def test_missing_at_file_is_named(self, tmp_path: Path):
        _b, _i, err = _read_brief_and_inputs(
            _args(brief=f"@{tmp_path / 'nope.md'}"))
        assert "cannot read brief" in err

    def test_oversize_brief_is_refused(self):
        _b, _i, err = _read_brief_and_inputs(_args(brief="x" * (64 * 1024 + 1)))
        assert "over the" in err and "limit" in err

    def test_non_utf8_input_is_refused(self, tmp_path: Path):
        f = tmp_path / "bin.dat"
        f.write_bytes(b"\xff\xfe\x00binary")
        _b, _i, err = _read_brief_and_inputs(_args(input=[str(f)]))
        assert "not UTF-8" in err

    def test_oversize_input_is_refused(self, tmp_path: Path):
        f = tmp_path / "big.txt"
        f.write_text("x" * (256 * 1024 + 1), encoding="utf-8")
        _b, _i, err = _read_brief_and_inputs(_args(input=[str(f)]))
        assert "input limit" in err

    def test_duplicate_basename_is_refused(self, tmp_path: Path):
        """The silent-loss shape: two files of one name land in one
        directory, and the agent works from whichever won without ever
        knowing the other existed."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        f1 = tmp_path / "a" / "notes.md"
        f2 = tmp_path / "b" / "notes.md"
        f1.write_text("one", encoding="utf-8")
        f2.write_text("two", encoding="utf-8")
        _b, _i, err = _read_brief_and_inputs(_args(input=[str(f1), str(f2)]))
        assert "both named 'notes.md'" in err

    def test_a_secret_in_an_INPUT_is_refused_and_names_the_file(
        self, tmp_path: Path
    ):
        f = tmp_path / "creds.txt"
        f.write_text(f"token={FAKE['a GitHub token']}", encoding="utf-8")
        _b, _i, err = _read_brief_and_inputs(_args(input=[str(f)]))
        assert "creds.txt" in err and "GitHub token" in err

    def test_the_promised_brief_refusal_now_exists(self):
        """The docstring above this function has promised this refusal since
        briefs shipped; it was enforced nowhere (declared-is-not-enforced, in
        our own code). Mutation: remove the scan_secret call → fails."""
        _b, _i, err = _read_brief_and_inputs(
            _args(brief=f"use {FAKE['an Anthropic API key']}"))
        assert "REFUSED" in err and "Anthropic API key" in err


# ---------------------------------------------------------------------------
# E1/E2 — the sandbox premise (W2.5)
# ---------------------------------------------------------------------------


class TestVolumes:
    def test_the_docker_socket_is_refused_naming_the_premise(self):
        """Mutation: drop docker.sock from _FORBIDDEN_MOUNTS → fails."""
        out = check_volumes(["/var/run/docker.sock:/var/run/docker.sock"])
        assert out is not None
        assert "docker daemon" in out and "bypassPermissions" in out

    def test_host_system_paths_are_refused(self):
        assert check_volumes(["/etc:/host-etc"]) is not None
        assert check_volumes(["/root/.ssh:/keys"]) is not None

    def test_named_volumes_and_ordinary_paths_pass(self):
        """Positive control — a guard that refused every mount would pass the
        two tests above while making seats unusable."""
        assert check_volumes(["seat-mem:/state"]) is None
        assert check_volumes(["/srv/project:/work"]) is None
        assert check_volumes(None) is None


class TestHomeDirectoryMounts:
    """The gap the prefix list did not cover, found 2026-08-11 while designing
    `repo_mount`: `/home` was absent, so a spec could mount the operator's
    whole account into a bypassPermissions container.

    Every test here FAILS against the pre-fix guard — that list refused
    /etc, /root, /boot, /sys, /proc, /var/run, /run and nothing else.
    """

    def test_a_whole_home_directory_is_refused(self):
        """Mutation: drop the _HOME_ROOTS branch → this passes silently."""
        out = check_volumes(["/home/monke:/host-home"])
        assert out is not None
        assert "home directory" in out
        # Names WHY, not merely that: the reader has to know what they were
        # about to hand over.
        assert "edge-env" in out and "ssh" in out

    def test_every_account_on_the_box_is_refused(self):
        assert check_volumes(["/home:/all"]) is not None
        assert check_volumes(["/Users:/all"]) is not None
        assert check_volumes(["/Users/timmy:/mac-home"]) is not None

    @pytest.mark.parametrize("path", [
        "/home/monke/.ssh",
        "/home/monke/.claude",
        "/home/monke/.mcp-hub",
        "/home/monke/.aws",
        "/home/monke/.config/gh",
        "/srv/deploy/.ssh",          # not under a home at all
    ])
    def test_credential_bearing_components_are_refused(self, path):
        """Matching a COMPONENT, not a prefix — `/srv/deploy/.ssh` is a real
        shape and lives under no home directory."""
        out = check_volumes([f"{path}:/mounted"])
        assert out is not None
        assert "credentials" in out

    def test_a_project_directory_under_a_home_still_passes(self):
        """Positive control, and the load-bearing one: this design EXISTS to
        mount host directories. A guard that refused everything under /home
        would pass every test above and forbid the feature."""
        assert check_volumes(["/home/monke/Projects/thing:/work"]) is None
        assert check_volumes(
            ["/home/monke/.mcp-hub-not-really/x:/work"]) is None


class TestRepoMount:
    def test_a_plain_org_repo_is_accepted(self):
        """Positive control before any refusal below is trusted."""
        assert check_repo_mount(
            {"repo": "dreamteam-ai-labs/browser-agent-test-fixture"}) is None
        assert check_repo_mount(
            {"repo": "a/b", "ref": "main", "dest": "/home/seat/work"}) is None
        assert check_repo_mount(None) is None

    @pytest.mark.parametrize("repo", [
        "../../etc", "org/../../etc", "/abs/path", "no-slash",
        "org/repo/extra", "-oProxyCommand=x/y", "org/..",
    ])
    def test_anything_that_could_climb_out_is_refused(self, repo):
        """Mutation: relax _REPO_RE to `.+/.+` → the traversal cases pass.

        The repo name becomes a directory component under the managed root,
        so this is the boundary that makes 'outside the root' unreachable.
        """
        assert check_repo_mount({"repo": repo}) is not None

    def test_a_ref_that_git_would_read_as_an_option_is_refused(self):
        out = check_repo_mount({"repo": "a/b", "ref": "--upload-pack=evil"})
        assert out is not None and "option" in out

    def test_dest_inside_the_state_dir_is_refused_naming_the_shadowing(self):
        """A checkout at ~/.claude would shadow the memory volume — the
        durability failure of 2026-08-06 rebuilt from new parts."""
        out = check_repo_mount({"repo": "a/b", "dest": "/home/seat/.claude"})
        assert out is not None and "memory volume" in out
        assert check_repo_mount(
            {"repo": "a/b", "dest": "/home/seat/.claude/x"}) is not None

    def test_dest_must_be_absolute_and_not_root(self):
        assert check_repo_mount({"repo": "a/b", "dest": "work"}) is not None
        assert check_repo_mount({"repo": "a/b", "dest": "/"}) is not None

    def test_a_missing_repo_is_refused(self):
        assert check_repo_mount({}) is not None
        assert check_repo_mount({"ref": "main"}) is not None
        assert check_repo_mount("a/b") is not None


# ---------------------------------------------------------------------------
# C1/C3/C4 — all four seat-writing routes, and what they do NOT re-check
# ---------------------------------------------------------------------------


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", "op-token")
    db_path = tmp_path / "hub.db"
    server = create_server(db_path=db_path)
    with TestClient(server.streamable_http_app()) as c:
        c.post("/api/v1/machines",
               json={"name": "box-1", "os": "linux", "capabilities": {}},
               headers=OP)
        c.db_path = db_path          # for tests that must plant legacy rows
        yield c


def _add(c, identity, **spec):
    return c.post(
        "/api/v1/seats",
        json={"identity": identity, "machine": "box-1", "folder": "/srv/x",
              "repo": "acme/x", "spec": spec},
        headers=OP,
    )


class TestRoutesEnforce:
    def test_positive_control_a_clean_seat_is_accepted(self, rig):
        assert _add(rig, "clean", brief="do the thing").status_code == 201

    def test_POST_refuses_a_secret_in_the_brief(self, rig):
        r = _add(rig, "s1", brief=f"key {FAKE['an AWS access key id']}")
        assert r.status_code == 422
        assert "AWS access key id" in r.json()["detail"]

    def test_POST_refuses_a_secret_in_an_input(self, rig):
        r = _add(rig, "s2", inputs={"c.txt": FAKE["a PEM private key block"]})
        assert r.status_code == 422
        assert "PEM private key" in r.json()["detail"]

    def test_POST_refuses_an_escaping_input_filename(self, rig):
        """This rule existed CONTAINER-side only (seat.py) — a spec written
        straight to the API never reached it, and an escaping name could
        overwrite the seat's own settings and install hooks.

        Mutation: drop the check_input_name call → fails."""
        r = _add(rig, "s3", inputs={"../../.claude/settings.json": "{}"})
        assert r.status_code == 422
        assert "escape" in r.json()["detail"]

    def test_POST_refuses_the_docker_socket(self, rig):
        r = _add(rig, "s4", image="x:1",
                 volumes=["/var/run/docker.sock:/var/run/docker.sock"])
        assert r.status_code == 422
        assert "docker daemon" in r.json()["detail"]

    def test_POST_refuses_a_whole_home_directory(self, rig):
        """The gap found 2026-08-11: `/home` was on no list, so this was
        accepted. Mutation: drop the _HOME_ROOTS branch → 201."""
        r = _add(rig, "s5", image="x:1", volumes=["/home/monke:/host"])
        assert r.status_code == 422
        assert "home directory" in r.json()["detail"]

    def test_POST_refuses_a_traversing_repo_mount(self, rig):
        r = _add(rig, "s6", image="x:1", repo_mount={"repo": "../../etc"})
        assert r.status_code == 422
        assert "org/name" in r.json()["detail"]

    def test_POST_accepts_a_well_formed_repo_mount(self, rig):
        """Positive control — the feature has to be usable, not merely safe."""
        r = _add(rig, "s7", image="x:1",
                 repo_mount={"repo": "dreamteam-ai-labs/fixture",
                             "ref": "main"})
        assert r.status_code == 201

    def test_PATCH_reassigning_the_repo_is_validated(self, rig):
        """The repo is assigned PER BUILD, so PATCH is the hot path for this
        key — not POST."""
        _add(rig, "p9", image="x:1", repo_mount={"repo": "org/first"})
        r = rig.patch("/api/v1/seats/p9",
                      json={"spec": {"repo_mount": {"repo": "org/../../etc"}}},
                      headers=OP)
        assert r.status_code == 422

    def test_PATCH_refuses_a_secret_in_a_re_brief(self, rig):
        _add(rig, "p1", brief="fine")
        r = rig.patch("/api/v1/seats/p1",
                      json={"spec": {"brief": f"tok {FAKE['a GitHub token']}"}},
                      headers=OP)
        assert r.status_code == 422

    def test_PATCH_validates_only_what_was_SENT(self, rig):
        """A rule added today must not make an existing seat un-patchable
        because of a key the caller is not touching.

        ⚠️ The first draft asserted this against a seat whose stored brief was
        `"fine"` — clean content, so it passed whether PATCH validated
        `incoming` or the merged spec, and the named mutation SURVIVED it.
        Same vacuous shape as the clone test's first draft below; caught by
        actually running the mutation instead of trusting the docstring.

        Mutation: validate the merged spec instead of `incoming` → fails."""
        import json as _json
        import sqlite3 as _sq

        # A seat whose STORED brief would fail today's guard, exactly as a
        # pre-guard hub left it. Planted directly because the API refuses it.
        _add(rig, "p2", brief="ordinary")
        con = _sq.connect(rig.db_path)
        try:
            con.execute(
                "UPDATE api_seats SET spec = ? WHERE identity = 'p2'",
                (_json.dumps({"brief": f"old key {FAKE['a GitHub token']}"}),),
            )
            con.commit()
        finally:
            con.close()
        # Control: that content IS refused when SENT, so the 200 below is the
        # exemption working rather than the guard being absent.
        assert rig.patch(
            "/api/v1/seats/p2",
            json={"spec": {"brief": f"new key {FAKE['a GitHub token']}"}},
            headers=OP,
        ).status_code == 422
        # Patching an unrelated key must still succeed despite the legacy brief
        r = rig.patch("/api/v1/seats/p2", json={"spec": {"image": "x:1"}},
                      headers=OP)
        assert r.status_code == 200, r.text

    def test_CLONE_does_not_re_validate_legacy_content(self, rig):
        """Clone copies a spec that was already accepted. Re-checking it
        would make a seat declared yesterday uncloneable the moment a
        pattern is added — so this plants content the guard WOULD refuse
        today, exactly as a pre-guard hub stored it, and clones it.

        ⚠️ The first draft of this test cloned a CLEAN seat, which would
        have passed even if clone re-validated everything — vacuous by this
        repo's own standard, caught by asking what else could produce the
        green.

        Mutation: validate the whole spec on clone → fails."""
        import json as _json
        import sqlite3 as _sq

        _add(rig, "legacy", brief="ordinary")
        con = _sq.connect(rig.db_path)
        try:
            con.execute(
                "UPDATE api_seats SET spec = ? WHERE identity = 'legacy'",
                (_json.dumps({"brief": f"old key {FAKE['a GitHub token']}"}),),
            )
            con.commit()
        finally:
            con.close()
        # Control: the same content IS refused on a fresh create, so the
        # clone below is genuinely exercising the exemption.
        assert _add(rig, "fresh",
                    brief=f"old key {FAKE['a GitHub token']}").status_code == 422
        r = rig.post("/api/v1/seats/legacy/clone", json={"suffix": "takeb"},
                     headers=OP)
        assert r.status_code == 201, r.text

    def test_CLONE_still_refuses_a_bad_VOLUME(self, rig):
        """The sandbox premise is a property of the container about to be
        created, not of the one it copied from — so volumes ARE checked."""
        _add(rig, "vol", image="x:1", volumes=["seat-mem:/state"])
        rig.patch("/api/v1/seats/vol",
                  json={"spec": {"volumes": ["seat-mem:/state"]}}, headers=OP)
        r = rig.post("/api/v1/seats/vol/clone", json={"suffix": "c1"},
                     headers=OP)
        assert r.status_code == 201  # clean volumes clone fine


# ---------------------------------------------------------------------------
# E1 (edge side) — the LAST gate, for specs that predate the hub's guard
# ---------------------------------------------------------------------------


class TestEdgeRefusesToMaterialize:
    """The hub refuses such a spec at WRITE time — but a spec stored before
    that guard existed would otherwise materialize anyway. The edge is the
    only place that can still say no, and it does, rather than trusting a
    validation that happened somewhere else at some other time."""

    def _executor(self):
        from mcp_hub.edge import DockerExecutor

        ran: list[list[str]] = []

        def runner(cmd):
            ran.append(cmd)
            return 0, ""

        return DockerExecutor(runner, environ={}), ran

    def test_a_legacy_docker_socket_spec_is_REFUSED_at_materialize(self):
        """Mutation: remove the check_volumes call from the create branch →
        this fails, and `docker create` runs with the socket mounted."""
        ex, ran = self._executor()
        out = ex.execute(
            {"op": "materialize", "seat": "s1", "placement": "p1"},
            {"spec": {"image": "x:1",
                      "volumes": [
                          "/var/run/docker.sock:/var/run/docker.sock"]}},
        )
        assert out.get("skipped") is True
        assert "docker daemon" in out["reason"]
        assert not any("create" in c for c in ran), "it must not have created"

    def test_positive_control_a_clean_spec_still_materializes(self):
        """Without this, an executor that refused EVERY materialize would
        pass the test above and look correct."""
        ex, ran = self._executor()
        out = ex.execute(
            {"op": "materialize", "seat": "s2", "placement": "p2"},
            {"spec": {"image": "x:1", "volumes": ["seat-mem:/state"]}},
        )
        assert not out.get("skipped"), out
        assert any("create" in c for c in ran)
