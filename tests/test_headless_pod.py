"""Headless PODS — N briefed agents, one turn each, one honest exit code.

🔴 THE REFUSAL THAT OUTLIVED ITS REASON (2026-08-08). `parse_pod_manifest`
refused `SEAT_MODE=headless` outright, on the grounds that "SEAT_PROMPT is
single-valued and a pod has several agents". True of a PROMPT — and it stopped
being the whole story the moment briefs landed, because a brief is per-agent
and already worked for pods. I re-endorsed the refusal in a gap review four
hours after shipping the thing that invalidated it.

⇒ When a refusal's justification names ONE mechanism, check whether it still
forbids the whole CATEGORY after that mechanism changes.

The operator's shape: "three agents, here's the brief, work overnight, leave
me the results." Two hard parts, and neither is the launch:
  * N outcomes must reach ONE exit code without losing "not all fine"
  * N concurrent turns share one stdout, which must stay readable
"""
from __future__ import annotations

import json

import pytest

from mcp_hub import cli
from mcp_hub.seat import (
    EXIT_PARTIAL,
    SeatContractError,
    agent_contract,
    headless_agent_succeeded,
    headless_pod_verdict,
    parse_pod_manifest,
)


def _env(agents, **kw):
    env = {"MCP_HUB_URL": "http://h/mcp", "SEAT_MODE": "headless",
           "SEAT_MANIFEST": json.dumps({"squad": "spike", "agents": agents})}
    env.update(kw)
    return env


# ------------------------------------------------------------- the door


class TestTheDoorNarrowed:
    def test_a_briefed_pod_is_ACCEPTED_headless(self):
        """The gap. This raised SeatContractError until 2026-08-08."""
        pod = parse_pod_manifest(_env(
            [{"identity": "alice"}, {"identity": "bob"}],
            SEAT_BRIEF="Spike the cache question."))
        assert pod.mode == "headless"
        assert [a.identity for a in pod.agents] == ["alice", "bob"]

    def test_per_agent_briefs_alone_are_enough(self):
        pod = parse_pod_manifest(_env([
            {"identity": "alice", "brief": "You do the maths."},
            {"identity": "bob", "brief": "You do the prose."}]))
        assert {a.identity: agent_contract(pod, a).brief
                for a in pod.agents} == {"alice": "You do the maths.",
                                         "bob": "You do the prose."}

    def test_a_single_valued_PROMPT_is_still_refused(self):
        """The part of the old reasoning that was always correct: one prompt
        cannot address N agents, and guessing is how work lands in the wrong
        lane."""
        with pytest.raises(SeatContractError) as e:
            parse_pod_manifest(_env([{"identity": "alice"},
                                     {"identity": "bob"}],
                                    SEAT_PROMPT="do the thing"))
        assert "single-valued" in str(e.value)
        assert "brief" in str(e.value).lower()      # names the way forward

    def test_an_UNBRIEFED_agent_is_refused_AND_NAMED(self):
        """🔴 The silent-nothing case. A pod where one agent is briefed and the
        rest are not would start N turns, N-1 with no instruction — each
        exiting instantly, which the edge reads as a crash. The fix is
        per-agent so the message must name the agents."""
        with pytest.raises(SeatContractError) as e:
            parse_pod_manifest(_env([
                {"identity": "alice", "brief": "go"},
                {"identity": "bob"},
                {"identity": "carol"}]))
        msg = str(e.value)
        assert "bob" in msg and "carol" in msg
        assert "alice" not in msg, "named an agent that WAS briefed"

    def test_an_interactive_pod_needs_no_brief(self):
        """The control: this rule is headless-only. An interactive pod with no
        brief is the ordinary squad container and must stay legal."""
        pod = parse_pod_manifest({
            "MCP_HUB_URL": "http://h/mcp",
            "SEAT_MANIFEST": json.dumps(
                {"agents": [{"identity": "alice"}]})})
        assert pod.mode == "interactive"


# --------------------------------------------------- the completion rule


class TestPodVerdict:
    def _v(self, identity, exit_code=0, timed_out=False, **kw):
        return {"identity": identity, "exit_code": exit_code,
                "timed_out": timed_out, **kw}

    def test_all_succeeding_is_zero(self):
        _s, rc = headless_pod_verdict([self._v("a"), self._v("b")])
        assert rc == 0

    def test_ONE_failure_makes_the_whole_pod_partial(self):
        """The property the container's single exit code exists to carry: the
        difference between 'all fine' and 'not all fine' must survive."""
        summary, rc = headless_pod_verdict(
            [self._v("a"), self._v("b", exit_code=1)])
        assert rc == EXIT_PARTIAL
        assert summary["failed"] == ["b"]
        assert summary["succeeded"] == 1

    def test_a_turn_that_exited_0_but_SAYS_it_errored_is_a_failure(self):
        """⚠️ `claude -p` exits 0 because the CLI ran, not because the task
        was done. Trusting the exit code alone would report success for an
        agent that told us plainly it had failed."""
        summary, rc = headless_pod_verdict(
            [self._v("a"), self._v("b", exit_code=0, is_error=True)])
        assert rc == EXIT_PARTIAL
        assert summary["failed"] == ["b"]

    def test_a_timeout_is_a_failure_and_is_reported_separately(self):
        summary, rc = headless_pod_verdict(
            [self._v("a"), self._v("b", exit_code=124, timed_out=True)])
        assert rc == EXIT_PARTIAL
        assert summary["timed_out"] == ["b"]

    def test_an_unparsable_record_does_NOT_invent_a_failure(self):
        """Degrading to the exit code is deliberate: the run may well have
        worked, and inventing a failure is as dishonest as hiding one."""
        _s, rc = headless_pod_verdict([self._v("a", unparsed=True)])
        assert rc == 0

    def test_an_EMPTY_pod_is_never_success(self):
        """'Healthy and doing nothing' is the failure this codebase keeps
        meeting. Zero agents completing zero turns is not a converged pod."""
        _s, rc = headless_pod_verdict([])
        assert rc == EXIT_PARTIAL

    def test_failures_are_NAMED_not_counted(self):
        summary, _rc = headless_pod_verdict(
            [self._v("a", exit_code=1), self._v("b"), self._v("c", exit_code=2)])
        assert summary["failed"] == ["a", "c"], (
            "a count sends the operator hunting through every agent directory")

    def test_the_exit_code_is_disjoint_from_the_other_verdicts(self):
        """125 must never be confused with 42 (auth), 43 (contract) or 124
        (timeout) — 'partial' and 'never started' are different facts."""
        from mcp_hub.seat import EXIT_AUTH, EXIT_CONTRACT, EXIT_TIMEOUT
        assert len({EXIT_PARTIAL, EXIT_AUTH, EXIT_CONTRACT, EXIT_TIMEOUT}) == 4


def test_agent_succeeded_requires_BOTH_signals_clean():
    """Neither signal alone is sufficient, in either direction."""
    assert headless_agent_succeeded({"exit_code": 0})
    assert not headless_agent_succeeded({"exit_code": 1})
    assert not headless_agent_succeeded({"exit_code": 0, "is_error": True})
    assert not headless_agent_succeeded({"exit_code": 0, "timed_out": True})


# ------------------------------------------------------------- the runner


class TestPodRunner:
    def _prepared(self, monkeypatch, tmp_path, scripts):
        """Real sh subprocesses, one per agent, HOME redirected."""
        import mcp_hub.seat as seat
        from mcp_hub.seat import SeatContract

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            seat, "launch_argv",
            lambda c, w, session="seat": ["sh", "-c", scripts[c.identity]])
        out = []
        for ident in scripts:
            wd = tmp_path / ident
            wd.mkdir(parents=True, exist_ok=True)
            out.append((ident, SeatContract(
                identity=ident, project="p", hub_url="h", mode="headless",
                prompt="x", squads="", repo="", brief="b"), wd))
        return out

    def test_EVERY_agent_runs(self, monkeypatch, tmp_path, capfd):
        """🔴 The bug the old dispatch comment predicted: `prepared[0]` only,
        so agents 2..N would silently never run — a pod that looked like it
        worked and did a third of the job."""
        prepared = self._prepared(monkeypatch, tmp_path, {
            "alice": "echo ALICE-RAN", "bob": "echo BOB-RAN",
            "carol": "echo CAROL-RAN"})
        rc = cli._seat_headless_pod(prepared)
        assert rc == 0
        for who in ("alice", "bob", "carol"):
            log = tmp_path / ".claude" / "seat-results" / who / "output.log"
            assert log.exists(), f"{who} never ran at all"
            assert f"{who.upper()}-RAN" in log.read_text()

    def test_one_failure_gives_a_PARTIAL_pod_and_names_it(
            self, monkeypatch, tmp_path, capfd):
        prepared = self._prepared(monkeypatch, tmp_path, {
            "alice": "echo ok", "bob": "exit 3"})
        rc = cli._seat_headless_pod(prepared)
        assert rc == EXIT_PARTIAL
        summary = json.loads(
            (tmp_path / ".claude" / "seat-results" / "_pod"
             / "result.json").read_text())
        assert summary["failed"] == ["bob"]
        assert summary["succeeded"] == 1
        assert "bob" in capfd.readouterr().err

    def test_a_failing_agent_does_not_lose_the_OTHERS_results(
            self, monkeypatch, tmp_path, capfd):
        prepared = self._prepared(monkeypatch, tmp_path, {
            "alice": "echo KEEPME", "bob": "exit 9"})
        cli._seat_headless_pod(prepared)
        log = tmp_path / ".claude" / "seat-results" / "alice" / "output.log"
        assert "KEEPME" in log.read_text()

    def test_agents_run_CONCURRENTLY_not_one_after_another(
            self, monkeypatch, tmp_path, capfd):
        """Sequential would make a 3-agent pod take 3x as long for no gain —
        and the timeout is PER AGENT, so a sequential pod's worst case is
        N x timeout, which is not a bound anyone would recognise as one.

        Decidable: three 1.5s sleeps take ~1.5s concurrently and ~4.5s
        sequentially. The 3.0s threshold sits well clear of both.
        """
        import time
        prepared = self._prepared(monkeypatch, tmp_path, {
            "a": "sleep 1.5", "b": "sleep 1.5", "c": "sleep 1.5"})
        t0 = time.time()
        cli._seat_headless_pod(prepared)
        elapsed = time.time() - t0
        assert elapsed < 3.0, (
            f"three 1.5s turns took {elapsed:.1f}s — they ran sequentially")

    def test_each_agents_output_is_ATTRIBUTED_on_the_shared_stdout(
            self, monkeypatch, tmp_path, capfd):
        """N turns share one container stdout. Unprefixed, `docker logs` is
        unreadable exactly when several agents are working, which is the whole
        point of a pod."""
        prepared = self._prepared(monkeypatch, tmp_path, {
            "alice": "echo hello", "bob": "echo world"})
        cli._seat_headless_pod(prepared)
        out = capfd.readouterr().out
        assert "[alice] hello" in out
        assert "[bob] world" in out

    def test_the_per_agent_ARTIFACT_stays_unprefixed(
            self, monkeypatch, tmp_path, capfd):
        """The prefix is for humans reading a multiplexed stream. The artifact
        is the machine-readable copy and must stay byte-exact, or anything
        parsing a result would have to strip a decoration."""
        prepared = self._prepared(monkeypatch, tmp_path,
                                  {"alice": "echo hello"})
        cli._seat_headless_pod(prepared)
        log = tmp_path / ".claude" / "seat-results" / "alice" / "output.log"
        assert log.read_text() == "hello\n"

    def test_SEAT_ENTRY_ACTUALLY_DISPATCHES_a_headless_pod_to_this_runner(
            self, monkeypatch, tmp_path, capfd):
        """🔴 CAUGHT BY MUTATION, and the reason this test exists.

        Every other test in this class calls `_seat_headless_pod` DIRECTLY, so
        none of them touches the dispatch in `seat_entry_command`. I proved it:
        reverting that dispatch to the old first-agent-only
        `_seat_headless(prepared[0]...)` left `test_EVERY_agent_runs` GREEN —
        the exact regression the runner exists to prevent, invisible to the
        tests written for it.

        ⇒ A test that calls the unit under test directly cannot prove anything
        about how the unit is REACHED. Same family as the argparse bug earlier
        today: whatever the fixture hands in is the layer nobody is testing.
        """
        seen = {}
        monkeypatch.setattr(
            cli, "_seat_headless_pod",
            lambda prepared: seen.setdefault(
                "agents", [c.identity for _s, c, _w in prepared]) and 0 or 0)
        monkeypatch.setattr(
            cli, "_seat_headless",
            lambda *a, **k: seen.setdefault("single", True) and (0, {}))
        # ~/.claude is not a real mount in a test, and the door refuses a
        # headless seat without one — patched so the DISPATCH is what is
        # under test here, not the volume gate (covered separately).
        monkeypatch.setattr(cli.os.path, "ismount", lambda p: True)

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        for k in ("SEAT_IDENTITY", "SEAT_PROMPT"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MCP_HUB_URL", "http://h/mcp")
        monkeypatch.setenv("SEAT_MODE", "headless")
        monkeypatch.setenv("SEAT_BRIEF", "spike it")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "a" * 52)
        monkeypatch.setenv("SEAT_MANIFEST", json.dumps(
            {"squad": "spike", "agents": [{"identity": "alice"},
                                          {"identity": "bob"}]}))

        cli.main(["seat-entry", "--workdir", str(tmp_path / "work")])
        assert seen.get("agents") == ["alice", "bob"], (
            "a headless POD did not reach the pod runner — agents 2..N would "
            "silently never run")
        assert "single" not in seen, "dispatched to the 1:1 runner instead"

    def test_the_summary_orders_by_the_MANIFEST_not_by_finish_time(
            self, monkeypatch, tmp_path, capfd):
        """A summary whose rows move between runs is one nobody can diff."""
        prepared = self._prepared(monkeypatch, tmp_path, {
            "alice": "sleep 0.8; echo a", "bob": "echo b"})
        cli._seat_headless_pod(prepared)
        summary = json.loads(
            (tmp_path / ".claude" / "seat-results" / "_pod"
             / "result.json").read_text())
        assert [r["identity"] for r in summary["results"]] == ["alice", "bob"]
