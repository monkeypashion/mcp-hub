"""Headless seats: the errand's result must OUTLIVE its container.

The through-line of every test here is one measured fact-pair (2026-08-08):
`docker logs` die with `docker rm`, and reclaim's harvest (`docker exec`)
refuses on an exited container. So a headless seat's output has exactly one
durable home — the memory volume — and everything else follows: the artifact
writer, the door refusal when no volume is mounted, the edge learning to tell
"finished" from "crashed", and `seats logs` reading the volume once the
container is gone.

The tee/timeout tests run _seat_headless against a REAL subprocess (sh, not
claude) because the properties under test — output passes through as it
arrives, a killed turn leaves its partial log — are properties of the I/O
loop, and a mocked Popen would test the mock. The one thing these tests
cannot prove is the whole path inside a container; that evidence is a real
placement (and one was run: throwaway on :latest, rc=0, before any of this
was written).
"""

from __future__ import annotations

import json

import pytest

from mcp_hub.edge import observed_report, plan
from mcp_hub.seat import (
    EXIT_TIMEOUT,
    HEADLESS_RESULTS_SUBDIR,
    HEADLESS_TIMEOUT_DEFAULT,
    SeatContract,
    SeatContractError,
    headless_result_paths,
    headless_verdict,
    launch_argv,
    parse_seat_contract,
)

BASE_ENV = {
    "SEAT_IDENTITY": "errand-1",
    "MCP_HUB_URL": "http://hub:8090/mcp",
}


def _contract(**over) -> SeatContract:
    fields = dict(
        identity="errand-1", project="acme/x", hub_url="http://hub:8090/mcp",
        mode="headless", prompt="do the thing", squads="", repo="",
    )
    fields.update(over)
    return SeatContract(**fields)


# ---------------------------------------------------------------------------
# Contract: SEAT_TIMEOUT
# ---------------------------------------------------------------------------


class TestTimeoutContract:
    def test_headless_defaults_bounded(self):
        c = parse_seat_contract(
            {**BASE_ENV, "SEAT_MODE": "headless", "SEAT_PROMPT": "go"})
        # Unset must NOT mean unbounded: nobody watches a headless seat, so
        # a hung turn would hold the container (still `running`) forever.
        assert c.timeout == HEADLESS_TIMEOUT_DEFAULT

    def test_interactive_defaults_unbounded(self):
        c = parse_seat_contract(BASE_ENV)
        assert c.timeout == 0

    def test_explicit_value_wins(self):
        c = parse_seat_contract(
            {**BASE_ENV, "SEAT_MODE": "headless", "SEAT_PROMPT": "go",
             "SEAT_TIMEOUT": "90"})
        assert c.timeout == 90

    def test_zero_means_unbounded_even_for_headless(self):
        c = parse_seat_contract(
            {**BASE_ENV, "SEAT_MODE": "headless", "SEAT_PROMPT": "go",
             "SEAT_TIMEOUT": "0"})
        assert c.timeout == 0

    @pytest.mark.parametrize("bad", ["ninety", "1.5", "-10"])
    def test_junk_is_refused_naming_the_var(self, bad):
        with pytest.raises(SeatContractError, match="SEAT_TIMEOUT"):
            parse_seat_contract(
                {**BASE_ENV, "SEAT_MODE": "headless", "SEAT_PROMPT": "go",
                 "SEAT_TIMEOUT": bad})


# ---------------------------------------------------------------------------
# Launch argv: the verdict must be the TURN's, not the CLI's
# ---------------------------------------------------------------------------


class TestHeadlessArgv:
    def test_prompt_form_emits_structured_result(self):
        argv = launch_argv(_contract(), "/w")
        assert argv[:3] == ["claude", "-p", "do the thing"]
        # Exit code 0 only says the CLI ran; --output-format json is what
        # makes the turn assert its own verdict into result.json.
        assert argv[-2:] == ["--output-format", "json"]

    def test_brief_form_emits_structured_result(self):
        argv = launch_argv(_contract(prompt="", brief="the brief"), "/w")
        assert argv[0:2] == ["claude", "-p"]
        assert "BRIEF.md" in argv[2]
        assert argv[-2:] == ["--output-format", "json"]

    def test_interactive_untouched(self):
        argv = launch_argv(_contract(mode="interactive", prompt=""), "/w")
        assert argv[0] == "tmux"
        assert "--output-format" not in " ".join(argv)


# ---------------------------------------------------------------------------
# Verdict + artifact paths (pure)
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_parses_the_turns_own_record(self):
        out = json.dumps({"is_error": False, "subtype": "success",
                          "result": "DONE", "num_turns": 1,
                          "session_id": "s-1", "irrelevant": "dropped"})
        doc = headless_verdict(0, False, out)
        assert doc["is_error"] is False
        assert doc["result"] == "DONE"
        assert doc["exit_code"] == 0
        assert "irrelevant" not in doc

    def test_unparsable_output_still_yields_an_artifact(self):
        # A broken run is exactly when the artifact matters most; degrading
        # to the exit code alone must never become a refusal to write.
        doc = headless_verdict(1, False, "not json at all")
        assert doc == {"exit_code": 1, "timed_out": False, "unparsed": True}

    def test_timeout_is_recorded(self):
        doc = headless_verdict(EXIT_TIMEOUT, True, "")
        assert doc["timed_out"] is True
        assert doc["exit_code"] == EXIT_TIMEOUT

    def test_paths_live_in_the_subdir_not_the_memory_root(self):
        paths = headless_result_paths("/home/seat", "errand-1")
        for p in paths.values():
            # Loose in ~/.claude and the artifact eventually gets read as
            # MEMORY by something — the subdir is the fence.
            assert f"/.claude/{HEADLESS_RESULTS_SUBDIR}/errand-1/" in p


# ---------------------------------------------------------------------------
# The tee: output passes through AS IT ARRIVES, and the artifact is written
# ---------------------------------------------------------------------------


def _run_headless(monkeypatch, tmp_path, script, timeout=0):
    """_seat_headless against a real sh subprocess, HOME redirected."""
    import mcp_hub.cli as cli
    import mcp_hub.seat as seat

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        seat, "launch_argv", lambda c, w, session="seat": ["sh", "-c", script])
    contract = _contract(timeout=timeout)
    rc = cli._seat_headless(contract, tmp_path)
    base = tmp_path / ".claude" / HEADLESS_RESULTS_SUBDIR / "errand-1"
    return rc, base


class TestSeatHeadless:
    def test_tee_both_properties(self, monkeypatch, tmp_path, capfd):
        # fireblade's demanded assertion, both halves in one test: the
        # artifact is written AND the output still reaches stdout — capture
        # for the file must not have eaten the operator's live view.
        rc, base = _run_headless(
            monkeypatch, tmp_path, "echo hello; echo world")
        assert rc == 0
        assert (base / "output.log").read_text() == "hello\nworld\n"
        assert "hello" in capfd.readouterr().out
        assert (base / "exit_code").read_text().strip() == "0"

    def test_exit_code_passes_through(self, monkeypatch, tmp_path, capfd):
        rc, base = _run_headless(monkeypatch, tmp_path, "exit 7")
        assert rc == 7
        doc = json.loads((base / "result.json").read_text())
        assert doc["exit_code"] == 7

    def test_verdict_parsed_from_output(self, monkeypatch, tmp_path, capfd):
        rec = json.dumps({"is_error": False, "subtype": "success",
                          "result": "DONE"})
        rc, base = _run_headless(
            monkeypatch, tmp_path, f"echo '{rec}'")
        doc = json.loads((base / "result.json").read_text())
        assert doc["result"] == "DONE"
        assert doc["identity"] == "errand-1"

    def test_timeout_kills_and_keeps_partial_output(
            self, monkeypatch, tmp_path, capfd):
        # The partial log is the point of the timeout: it is the only
        # evidence that diagnoses the hang. It also PROVES the tee is
        # incremental — a writer that buffered until exit would leave an
        # empty file when killed.
        rc, base = _run_headless(
            monkeypatch, tmp_path, "echo partial; sleep 30", timeout=1)
        assert rc == EXIT_TIMEOUT
        assert (base / "output.log").read_text() == "partial\n"
        doc = json.loads((base / "result.json").read_text())
        assert doc["timed_out"] is True
        assert doc["exit_code"] == EXIT_TIMEOUT


# ---------------------------------------------------------------------------
# The edge: finished must stop reading as crashed-or-never-started
# ---------------------------------------------------------------------------


def _enum(**over):
    e = {"container": "errand-1", "alive": False, "exists": True,
         "headless": True, "exit_code": 0}
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


class TestObservedHeadless:
    def test_exit_zero_is_completed(self):
        r = observed_report({"id": "p1", "desired": "ran"}, _enum())
        assert r["state"] == "completed"

    def test_nonzero_is_failed(self):
        r = observed_report(
            {"id": "p1", "desired": "ran"}, _enum(exit_code=43))
        assert r["state"] == "failed"

    def test_timeout_marker_is_failed(self):
        r = observed_report(
            {"id": "p1", "desired": "ran"}, _enum(exit_code=EXIT_TIMEOUT))
        assert r["state"] == "failed"

    def test_still_running_stays_running(self):
        r = observed_report(
            {"id": "p1", "desired": "ran"},
            _enum(alive=True, exit_code=None))
        assert r["state"] == "running"

    def test_no_exit_code_is_stopped_not_completed(self):
        # A `created` container never gets an exit_code enumerated (docker
        # reports 0 for it) — reading that as completed would call a job
        # done that never ran. This is the mutation check on the mapping's
        # guard clause.
        r = observed_report(
            {"id": "p1", "desired": "ran"}, _enum(exit_code=None))
        assert r["state"] == "stopped"

    def test_interactive_exited_is_still_stopped(self):
        # The mapping must be gated on the MODE: an interactive seat that
        # exited 0 is stopped, and calling it completed would let desired=
        # running converge against a dead container.
        r = observed_report(
            {"id": "p1", "desired": "running"}, _enum(headless=None))
        assert r["state"] == "stopped"

    def test_reclaimed_absence_still_wins(self):
        r = observed_report(
            {"id": "p1", "desired": "reclaimed"},
            _enum(exists=False, exit_code=None))
        assert r["state"] == "reclaimed"


class TestPlanRan:
    def test_absent_is_materialized_and_started(self):
        actions = plan(
            [{"id": "p1", "seat": "errand-1", "substrate": "docker",
              "desired": "ran"}],
            {"errand-1": {"materialized": False, "running": False}})
        assert [a["op"] for a in actions] == ["materialize", "start"]
        assert actions[1].get("fresh") is True

    def test_exited_is_never_restarted(self):
        # THE bug this desired-state exists to prevent: restart re-runs the
        # errand. A finished (or failed) headless container plans nothing.
        actions = plan(
            [{"id": "p1", "seat": "errand-1", "substrate": "docker",
              "desired": "ran"}],
            {"errand-1": {"materialized": True, "running": False}})
        assert actions == []

    def test_in_flight_is_left_alone(self):
        actions = plan(
            [{"id": "p1", "seat": "errand-1", "substrate": "docker",
              "desired": "ran"}],
            {"errand-1": {"materialized": True, "running": True}})
        assert actions == []


class TestEnumerateExitCodes:
    def _runner(self, ps_out, inspect_out):
        def run(cmd, cwd=None):
            if cmd[:2] == ["docker", "ps"]:
                return 0, ps_out
            if cmd[:2] == ["docker", "inspect"]:
                return 0, inspect_out
            return 0, ""
        return run

    def test_exited_carries_its_code(self):
        from mcp_hub.edge import enumerate_docker

        state = enumerate_docker(
            self._runner("errand-1\texited\n",
                         "/errand-1 sha256:abc exited 7\n"),
            ["errand-1"])
        assert state["errand-1"]["exit_code"] == 7
        assert state["errand-1"]["running"] is False

    def test_created_gets_no_code(self):
        # docker reports ExitCode 0 for a created-never-started container;
        # enumerating that would let the mapping call it completed.
        from mcp_hub.edge import enumerate_docker

        state = enumerate_docker(
            self._runner("errand-1\tcreated\n",
                         "/errand-1 sha256:abc created 0\n"),
            ["errand-1"])
        assert "exit_code" not in state["errand-1"]

    def test_running_gets_no_code(self):
        from mcp_hub.edge import enumerate_docker

        state = enumerate_docker(
            self._runner("errand-1\trunning\n",
                         "/errand-1 sha256:abc running 0\n"),
            ["errand-1"])
        assert "exit_code" not in state["errand-1"]
        assert state["errand-1"]["running"] is True


# ---------------------------------------------------------------------------
# The edge refuses a headless seat that could not keep its result
# ---------------------------------------------------------------------------


class TestMaterializeRefusal:
    def _exec(self, spec):
        from mcp_hub.edge import DockerExecutor

        calls = []

        def runner(cmd, cwd=None):
            calls.append(cmd)
            return 0, ""

        ex = DockerExecutor(runner, environ={"CLAUDE_CODE_OAUTH_TOKEN": "x" * 60})
        result = ex.execute(
            {"op": "materialize", "seat": "errand-1", "substrate": "docker"},
            {"spec": spec})
        return result, calls

    def test_headless_without_volume_is_skipped_with_the_fix(self):
        result, calls = self._exec(
            {"image": "img:1", "env": {"SEAT_MODE": "headless"}})
        assert result.get("skipped") is True
        assert "memory_volume" in result["reason"]
        assert calls == []  # nothing was created

    def test_headless_with_volume_materializes(self):
        result, calls = self._exec(
            {"image": "img:1", "env": {"SEAT_MODE": "headless"},
             "memory_volume": "errand-mem"})
        assert not result.get("skipped")
        assert any(c[:2] == ["docker", "create"] for c in calls)

    def test_interactive_without_volume_is_untouched(self):
        result, _ = self._exec({"image": "img:1", "env": {}})
        assert not result.get("skipped")


# ---------------------------------------------------------------------------
# seats logs: the artifact answers after the container is gone
# ---------------------------------------------------------------------------


class _Api:
    def __init__(self, seats):
        self._seats = seats

    def list_seats(self):
        return self._seats


class TestLogsArtifact:
    def test_no_volume_names_the_real_cause(self, capsys):
        from mcp_hub.cli import _seat_logs_artifact

        rc = _seat_logs_artifact(
            _Api([{"identity": "errand-1", "spec": {"image": "img:1"}}]),
            "errand-1")
        assert rc == 1
        err = capsys.readouterr().err
        # "no artifact" alone reads like a seat that printed nothing, and an
        # operator who believes that stops looking.
        assert "WITHOUT a memory volume" in err

    def test_reads_from_the_volume_root(self, monkeypatch):
        from mcp_hub import cli

        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)

            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        rc = cli._seat_logs_artifact(
            _Api([{"identity": "errand-1",
                   "spec": {"image": "img:1", "memory_volume": "errand-mem"}}]),
            "errand-1")
        assert rc == 0
        argv = seen[0]
        assert "errand-mem:/artifact:ro" in argv
        # The volume root IS ~/.claude, so the artifact sits at
        # seat-results/<identity> — a .claude/ prefix here would read an
        # empty directory and report a seat that printed nothing.
        script = argv[-1]
        assert f"/artifact/{HEADLESS_RESULTS_SUBDIR}/errand-1" in script
        assert ".claude" not in script
