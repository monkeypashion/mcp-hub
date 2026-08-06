"""The edge's credential door.

Two halves of one defect: a hand-run `edge apply` inherits nothing from the
systemd unit's `EnvironmentFile=`, so it injected no credential and built a
container that exited 42 at its own door — present to `docker ps`, never an
agent. The file is now loaded by the CLI, and a materialize that would have
NO credential at all is refused instead of created.
"""

from pathlib import Path

from mcp_hub.edge import DockerExecutor, apply_env_file, load_env_file


class Runner:
    def __init__(self, rc=0, out=""):
        self.calls = []
        self._rc, self._out = rc, out

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        return self._rc, self._out


# ------------------------------------------------------------- load_env_file


def test_reads_the_systemd_dialect(tmp_path: Path):
    p = tmp_path / "edge-env"
    p.write_text(
        "# seat credentials\n"
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-abc\n"
        "\n"
        'ANTHROPIC_API_KEY="sk-quoted"\n'
        "SINGLE='sk-single'\n",
        encoding="utf-8",
    )
    assert load_env_file(p) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-abc",
        "ANTHROPIC_API_KEY": "sk-quoted",
        "SINGLE": "sk-single",
    }


def test_a_missing_file_is_empty_not_an_error(tmp_path: Path):
    """`EnvironmentFile=-` is optional for the unit; so is this. A box with no
    seats has no edge-env and must still reconcile its worktrees."""
    assert load_env_file(tmp_path / "nope") == {}


def test_one_malformed_line_does_not_take_the_good_ones_down(tmp_path: Path):
    p = tmp_path / "edge-env"
    p.write_text("GOOD=1\nthis is not an assignment\nALSO_GOOD=2\n",
                 encoding="utf-8")
    assert load_env_file(p) == {"GOOD": "1", "ALSO_GOOD": "2"}


def test_a_value_containing_equals_survives(tmp_path: Path):
    """Base64 and JWT-ish credentials carry `=`. Splitting on the LAST one
    would truncate the token to nothing and produce an implausible-length
    refusal inside the container that names the wrong cause."""
    p = tmp_path / "edge-env"
    p.write_text("TOKEN=abc==\n", encoding="utf-8")
    assert load_env_file(p) == {"TOKEN": "abc=="}


# ------------------------------------------------------------ apply_env_file


def test_it_fills_in_what_the_environment_lacks():
    env: dict[str, str] = {}
    assert apply_env_file(env, {"A": "1", "B": "2"}) == ["A", "B"]
    assert env == {"A": "1", "B": "2"}


def test_a_deliberate_export_outranks_the_file():
    env = {"A": "exported"}
    assert apply_env_file(env, {"A": "from-file"}) == []
    assert env["A"] == "exported"


def test_an_EMPTY_export_does_not_outrank_the_file():
    """An empty export clobbering a good credential is a named incident
    (docs/seat-image.md). Present-but-empty is indistinguishable from it, so
    the file wins — the alternative is honouring the clobber."""
    env = {"A": ""}
    assert apply_env_file(env, {"A": "from-file"}) == ["A"]
    assert env["A"] == "from-file"


def test_it_returns_names_never_values():
    """This list is printed. Values are the one thing that must not be."""
    env: dict[str, str] = {}
    supplied = apply_env_file(env, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-secret"})
    assert supplied == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert "sk-secret" not in str(supplied)


# ------------------------------------------------- the materialize refusal

SPEC = {"spec": {
    "image": "mcp-hub-seat:latest",
    "env_from_host": ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
}}


def test_no_credential_at_all_refuses_instead_of_creating_a_dead_seat():
    r = Runner()
    out = DockerExecutor(r, {}).execute({"op": "materialize", "seat": "s"}, SPEC)
    assert out["skipped"] is True
    assert "exit 42" in out["reason"]
    assert "edge-env" in out["reason"]      # names the fix, not just the fault
    assert r.calls == []                    # nothing was created


def test_one_lane_set_is_the_NORMAL_case_and_proceeds():
    """A spec naming both lanes with one set is every healthy seat on the
    fleet. Refusing on 'any missing' would have taken all of them down."""
    r = Runner()
    out = DockerExecutor(
        r, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-live"}
    ).execute({"op": "materialize", "seat": "s"}, SPEC)
    assert not out.get("skipped")
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-live" in r.calls[-1]


def test_a_container_naming_no_credentials_is_untouched():
    """nginx has no env_from_host and must still materialize — the refusal is
    about a spec that ASKED for credentials and got none."""
    r = Runner()
    out = DockerExecutor(r, {}).execute(
        {"op": "materialize", "seat": "web"}, {"spec": {"image": "nginx"}})
    assert not out.get("skipped")
    assert r.calls[-1][:3] == ["docker", "create", "--name"]


# --------------------------------------------- the memory volume is MOUNTED

def test_memory_volume_is_actually_mounted():
    """It was declared everywhere and mounted nowhere, so ~/.claude died with
    the container while the edge reported harvests of it. Measured by losing
    three live seats' memory to `docker rm`, 2026-08-06."""
    argv = DockerExecutor.create_argv("s", {
        "image": "mcp-hub-seat:latest",
        "memory_volume": "seat-memory-s:/home/seat/.claude",
    })
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert "seat-memory-s:/home/seat/.claude" in pairs


def test_a_bare_volume_name_gets_the_documented_destination():
    """A name with no destination is not a reason to skip the mount — that is
    how it came to be ignored in the first place."""
    argv = DockerExecutor.create_argv("s", {
        "image": "img", "memory_volume": "seat-memory-s",
    })
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert "seat-memory-s:/home/seat/.claude" in pairs


def test_it_does_not_disturb_the_work_mount():
    """Both mounts, distinct destinations. A memory volume landing on the
    worktree would hide the repo the seat is meant to work on."""
    argv = DockerExecutor.create_argv("s", {
        "image": "img",
        "volumes": ["/host/work:/home/seat/work"],
        "memory_volume": "seat-memory-s:/home/seat/.claude",
    })
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert pairs == ["/host/work:/home/seat/work",
                     "seat-memory-s:/home/seat/.claude"]


def test_a_seat_with_no_memory_volume_mounts_nothing_extra():
    """Stateless units (nginx, an inference server) stay stateless — the
    absence of a memory volume is the agent-vs-service line."""
    argv = DockerExecutor.create_argv("web", {"image": "nginx"})
    assert "-v" not in argv


def test_the_destination_is_the_CONTAINER_home_not_the_edge_hosts():
    """create_argv runs on the edge host, whose HOME is the operator's. A
    destination derived from it would mount the seat's memory into a path
    that does not exist inside the image."""
    from mcp_hub.edge import SEAT_STATE_DIR
    assert SEAT_STATE_DIR == "/home/seat/.claude"
