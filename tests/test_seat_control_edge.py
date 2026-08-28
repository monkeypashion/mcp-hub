"""Seat control, edge leg — the machine that owns the seat carries out intent.

Phase 1 of cards #144/#152. The hub side (tests/test_seat_control_plane.py)
records intent; this is the half that ACTS, and the half that can hurt: it
sends keystrokes to a live agent's terminal.

Everything here goes through an injected runner, exactly like SquadExecutor —
this module must never import subprocess, so no test path can reach a real
shell and no test can type into a real seat.

The rules that matter more than the mapping:

  R1 FAIL CLOSED. No session, no capture, no idea what is on screen → REFUSE
     and say why. The seat-entry lesson, verbatim: never type into a pane you
     cannot read. A blind keystroke that lands on whatever row is default is
     how a seat confirmed its own death, cleanly, exit 0.
  R2 REPORT WHAT WAS OBSERVED, not what was sent. The pane AFTER the action
     is the evidence; "we sent Escape" is an assumption with a number on it.
  R3 A PROMPT IS TYPED AS LITERAL TEXT. Never as a shell string, never
     interpolated into a command line.
"""

from __future__ import annotations

import pytest

from mcp_hub.edge import realize_seat_action


class Runner:
    """Records commands; returns canned (rc, output) per matched prefix."""

    def __init__(self, responses: list[tuple[list[str], int, str]] | None = None,
                 captures: list[str] | None = None):
        self.calls: list[list[str]] = []
        self._responses = responses or []
        # Sequential pane frames, consumed one per capture-pane call — the
        # before-frame and the after-frame genuinely differ once the
        # realizer verifies submission from the pane.
        self._captures = list(captures or [])

    def __call__(self, cmd, cwd=None):  # noqa: ANN001
        self.calls.append(list(cmd))
        if "capture-pane" in cmd and self._captures:
            return 0, self._captures.pop(0)
        for prefix, rc, out in self._responses:
            if cmd[: len(prefix)] == prefix:
                return rc, out
        return 0, ""

    def sent(self) -> list[list[str]]:
        return [c for c in self.calls if "send-keys" in c]


def _ok_capture(pane: str = "● ready\n> ") -> list[tuple[list[str], int, str]]:
    return [(["tmux", "-L", "squad", "capture-pane"], 0, pane)]


def _submitted(text: str) -> list[str]:
    """Before/after frames for a prompt that SUBMITTED: the text appears as
    a transcript line and the input box (the last ❯ line) is empty."""
    return ["● ready\n❯ ", f"❯ {text}\n\n● working\n❯ "]


def _stuck(text: str) -> list[str]:
    """Frames for the measured 2026-08-28 failure: text sits in the box."""
    return ["● ready\n❯ ", f"● ready\n❯ {text}"]


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """The realizer settles with time.sleep; tests record instead of wait."""
    import time as _t
    slept: list[float] = []
    monkeypatch.setattr(_t, "sleep", lambda s: slept.append(s))
    return slept


SEAT = {"identity": "seat-a", "substrate": "tmux", "session": "seat-a"}


# ---------------------------------------------------------------------------
# R1 — fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_no_pane_capture_means_REFUSE_not_send(self):
        """If we cannot read the screen we do not touch the keyboard."""
        r = Runner([(["tmux", "-L", "squad", "capture-pane"], 1, "no server")])
        out = realize_seat_action(
            {"id": 1, "kind": "interrupt", "args": {}}, SEAT, r
        )
        assert out["status"] == "refused", out
        assert not r.sent(), (
            "keystrokes were sent to a pane that could not be read — this is "
            "the seat-entry defect exactly"
        )
        assert "capture" in out["observed"]["why"].lower()

    def test_the_refusal_carries_the_reason_not_just_a_flag(self):
        r = Runner([(["tmux", "-L", "squad", "capture-pane"], 1, "no server")])
        out = realize_seat_action(
            {"id": 1, "kind": "interrupt", "args": {}}, SEAT, r
        )
        assert out["observed"].get("why"), (
            "a refusal with no reason sends the operator hunting; the pane "
            "text or the error is the whole value"
        )

    def test_an_unknown_verb_is_refused_by_the_edge_too(self):
        """Defence in depth: the hub refuses the verb at write time, but the
        edge must not execute anything it does not recognise either — one
        compromised writer must not become one executed keystroke."""
        r = Runner(_ok_capture())
        out = realize_seat_action(
            {"id": 1, "kind": "exec", "args": {"text": "rm -rf /"}}, SEAT, r
        )
        assert out["status"] == "refused", out
        assert not r.sent()


# ---------------------------------------------------------------------------
# The two verbs
# ---------------------------------------------------------------------------


class TestInterrupt:
    def test_interrupt_sends_escape(self):
        r = Runner(_ok_capture())
        out = realize_seat_action(
            {"id": 1, "kind": "interrupt", "args": {}}, SEAT, r
        )
        assert out["status"] == "done", out
        sent = r.sent()
        assert len(sent) == 1, f"expected exactly one send-keys, got {sent}"
        assert "Escape" in sent[0]

    def test_interrupt_sends_no_enter(self):
        """Escape then Enter would interrupt and then submit whatever was in
        the box — a different action than the one asked for."""
        r = Runner(_ok_capture())
        realize_seat_action({"id": 1, "kind": "interrupt", "args": {}}, SEAT, r)
        assert not any("Enter" in c for c in r.sent())


class TestPrompt:
    def test_prompt_types_the_text_then_enter(self):
        r = Runner(captures=_submitted("status?"))
        out = realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "status?"}}, SEAT, r
        )
        assert out["status"] == "done", out
        sent = r.sent()
        assert any("status?" in c for c in sent)
        assert any("Enter" in c for c in sent)

    def test_the_text_is_passed_as_ONE_literal_argv_element(self):
        """R3. The text must never be split, quoted, or interpolated into a
        command string — a prompt containing a semicolon is a prompt, not a
        second command."""
        r = Runner(captures=_submitted("check; then rm -rf /  # not a command"))
        realize_seat_action(
            {"id": 2, "kind": "prompt",
             "args": {"text": "check; then rm -rf /  # not a command"}},
            SEAT, r,
        )
        typed = [c for c in r.sent() if any("rm -rf" in part for part in c)]
        assert typed, "the literal text never reached send-keys"
        assert "check; then rm -rf /  # not a command" in typed[0], (
            "the prompt text was mangled or split — it must travel as one "
            f"argv element: {typed[0]!r}"
        )

    def test_text_and_enter_are_separate_sends(self):
        """`send-keys "text" Enter` in one call makes tmux interpret the
        literal as a key name when it happens to match one ("Enter", "C-c").
        Typing then submitting is two calls, deliberately."""
        r = Runner(captures=_submitted("Enter"))
        realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "Enter"}}, SEAT, r
        )
        sent = r.sent()
        assert len(sent) == 2, (
            f"expected type-then-submit as two sends, got {sent}"
        )
        assert "-l" in sent[0], (
            "the text send must use tmux's LITERAL flag, or a prompt whose "
            f"text is a key name is executed as that key: {sent[0]!r}"
        )


# ---------------------------------------------------------------------------
# R2 — observed, not assumed
# ---------------------------------------------------------------------------


class TestObserved:
    def test_the_outcome_carries_the_pane_AFTER_the_action(self):
        r = Runner([
            (["tmux", "-L", "squad", "capture-pane"], 0, "● working\n> "),
        ])
        out = realize_seat_action(
            {"id": 1, "kind": "interrupt", "args": {}}, SEAT, r
        )
        assert out["pane_after"] == "● working\n> "

    def test_the_pane_is_captured_AFTER_sending_not_before(self):
        r = Runner(_ok_capture())
        realize_seat_action({"id": 1, "kind": "interrupt", "args": {}}, SEAT, r)
        kinds = [
            "capture" if "capture-pane" in c else "send" if "send-keys" in c
            else "other"
            for c in r.calls
        ]
        assert kinds.count("capture") >= 2, (
            "expected a capture before (to prove the pane is readable) and "
            f"after (the evidence): {kinds}"
        )
        assert kinds.index("send") < len(kinds) - 1 - kinds[::-1].index("capture"), (
            f"the reported pane was captured before the keystroke: {kinds}"
        )


# ---------------------------------------------------------------------------
# Docker seats live behind docker exec
# ---------------------------------------------------------------------------


class TestDockerSeat:
    def test_a_container_seat_is_driven_through_docker_exec(self):
        r = Runner([(["docker", "exec"], 0, "● ready\n> ")])
        out = realize_seat_action(
            {"id": 1, "kind": "interrupt", "args": {}},
            {"identity": "dt-poc", "substrate": "docker", "session": "seat"},
            r,
        )
        assert out["status"] == "done", out
        assert all(c[0] == "docker" for c in r.calls), (
            f"a container seat was driven with host tmux: {r.calls}"
        )
        assert any("dt-poc" in c for c in r.calls), "the container was not named"

    def test_container_capture_failure_also_refuses(self):
        r = Runner([(["docker", "exec"], 1, "no such container")])
        out = realize_seat_action(
            {"id": 1, "kind": "interrupt", "args": {}},
            {"identity": "dt-poc", "substrate": "docker", "session": "seat"},
            r,
        )
        assert out["status"] == "refused"
        assert not r.sent()


# ---------------------------------------------------------------------------
# The runner is the only door out
# ---------------------------------------------------------------------------


def test_module_never_imports_subprocess():
    """SquadExecutor's rule, applied here because this module sends
    KEYSTROKES: if no test path can reach a shell, no test can type into a
    real agent's terminal by accident."""
    import inspect

    from mcp_hub import edge

    src = inspect.getsource(edge.realize_seat_action)
    assert "subprocess" not in src, (
        "realize_seat_action reaches for subprocess directly — every command "
        "must go through the injected runner"
    )


@pytest.mark.parametrize("kind", ["answer", "restart"])
def test_phase_two_verbs_are_not_executed(kind):
    r = Runner(_ok_capture())
    out = realize_seat_action(
        {"id": 1, "kind": kind, "args": {"answer": "yes"}}, SEAT, r
    )
    assert out["status"] == "refused", out
    assert not r.sent(), f"{kind} was executed despite being phase 2"


# ---------------------------------------------------------------------------
# The pass leg — isolation is the property under test
# ---------------------------------------------------------------------------


class FakeApi:
    """A hub that can be told to fail for one seat."""

    def __init__(self, actions=None, watched=(), fail_actions_for=(),
                 fail_view_for=()):
        self._actions = actions or {}
        self._watched = set(watched)
        self._fail_actions = set(fail_actions_for)
        self._fail_view = set(fail_view_for)
        self.reported: list[tuple] = []
        self.panes: list[tuple] = []

    def pull_seat_actions(self, seat):
        if seat in self._fail_actions:
            raise RuntimeError("hub 500")
        return self._actions.get(seat, [])

    def report_seat_action(self, seat, action_id, report):
        self.reported.append((seat, action_id, report["status"]))

    def seat_watched(self, seat):
        if seat in self._fail_view:
            raise RuntimeError("hub 500")
        return seat in self._watched

    def push_seat_pane(self, seat, pane):
        self.panes.append((seat, pane))


def _pl(seat, desired="running", substrate="worktree"):
    return {"seat": seat, "desired": desired, "substrate": substrate}


class TestSeatControlPass:
    def test_a_pending_action_is_realized_and_reported(self):
        from mcp_hub.edge import seat_control_pass

        api = FakeApi(actions={"seat-a": [
            {"id": 7, "kind": "interrupt", "args": {}, "status": "pending"},
        ]})
        out = seat_control_pass(api, [_pl("seat-a")], Runner(_ok_capture()))
        assert api.reported == [("seat-a", 7, "done")]
        assert out["realized"][0]["status"] == "done"

    def test_a_non_pending_action_is_left_alone(self):
        """Re-running an already-done action would replay a keystroke."""
        from mcp_hub.edge import seat_control_pass

        api = FakeApi(actions={"seat-a": [
            {"id": 7, "kind": "interrupt", "args": {}, "status": "done"},
            {"id": 8, "kind": "interrupt", "args": {}, "status": "expired"},
        ]})
        r = Runner(_ok_capture())
        seat_control_pass(api, [_pl("seat-a")], r)
        assert api.reported == []
        assert not r.sent(), "a settled action was replayed into the pane"

    def test_seats_not_desired_running_are_untouched(self):
        from mcp_hub.edge import seat_control_pass

        api = FakeApi(actions={"seat-a": [
            {"id": 7, "kind": "interrupt", "args": {}, "status": "pending"},
        ]})
        r = Runner(_ok_capture())
        seat_control_pass(api, [_pl("seat-a", desired="reclaimed")], r)
        assert api.reported == []
        assert not r.calls, "a reclaimed seat was driven"

    def test_one_seats_failure_does_not_stop_the_others(self):
        """The isolation property. A wedged seat must not cost the rest
        their actions — same reason edge is its own systemd unit."""
        from mcp_hub.edge import seat_control_pass

        api = FakeApi(
            actions={"seat-b": [
                {"id": 9, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            fail_actions_for=["seat-a"],
        )
        out = seat_control_pass(
            api, [_pl("seat-a"), _pl("seat-b")], Runner(_ok_capture())
        )
        assert api.reported == [("seat-b", 9, "done")], (
            "seat-b lost its action because seat-a failed"
        )
        assert any("seat-a" in e for e in out["errors"]), (
            "the failure was swallowed — an isolated failure with no reader "
            "is the defect, not the fix"
        )

    def test_panes_stream_only_while_watched(self):
        from mcp_hub.edge import seat_control_pass

        api = FakeApi(watched=["seat-a"])
        seat_control_pass(
            api, [_pl("seat-a"), _pl("seat-b")], Runner(_ok_capture("live"))
        )
        assert [s for s, _ in api.panes] == ["seat-a"], (
            f"an unwatched seat was streamed: {api.panes}"
        )

    def test_an_unreadable_pane_pushes_nothing(self):
        """An empty pane would read as 'the seat is showing nothing', which
        is a measurement. Absence is not."""
        from mcp_hub.edge import seat_control_pass

        api = FakeApi(watched=["seat-a"])
        r = Runner([(["tmux", "-L", "squad", "capture-pane"], 1, "")])
        seat_control_pass(api, [_pl("seat-a")], r)
        assert api.panes == [], f"a failed capture was published: {api.panes}"

    def test_a_view_failure_does_not_stop_action_realization(self):
        from mcp_hub.edge import seat_control_pass

        api = FakeApi(
            actions={"seat-a": [
                {"id": 7, "kind": "prompt", "args": {"text": "hi"},
                 "status": "pending"},
            ]},
            fail_view_for=["seat-a"],
        )
        out = seat_control_pass(api, [_pl("seat-a")],
                                Runner(captures=_submitted("hi")))
        assert api.reported == [("seat-a", 7, "done")]
        assert out["errors"], "the view failure was not reported"


# ---------------------------------------------------------------------------
# Lane seats — interactive squad lanes enrolled with spec.substrate == "lane".
# No placement EVER (a placement means the edge owns lifecycle, and lane
# lifecycle is squad's); discovered via the machine-scoped seats route and
# driven through squad's own tmux socket. The defect this exists to close,
# measured 2026-08-28: every lane that actually freezes was off the control
# plane, so "stop everyone" interrupted 0 of 7 and fell through to telling.
# ---------------------------------------------------------------------------


class LaneApi(FakeApi):
    def __init__(self, *args, seats=None, fail_seats=False, **kw):
        super().__init__(*args, **kw)
        self._seats = seats or []
        self._fail_seats = fail_seats
        self.seats_pulled_for: list[str] = []

    def pull_seats(self, machine):
        self.seats_pulled_for.append(machine)
        if self._fail_seats:
            raise RuntimeError("hub 500")
        return self._seats


def _lane(identity, session=None):
    spec = {"substrate": "lane"}
    if session:
        spec["session"] = session
    return {"identity": identity, "machine": "m1", "spec": spec}


class TestLaneSeats:
    def test_a_lane_seat_with_no_placement_is_driven(self):
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(
            actions={"vps-hetzner-dev-vm-1": [
                {"id": 3, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            seats=[_lane("vps-hetzner-dev-vm-1")],
        )
        out = seat_control_pass(api, [], Runner(_ok_capture()), machine="m1")
        assert api.reported == [("vps-hetzner-dev-vm-1", 3, "done")]
        assert out["realized"][0]["seat"] == "vps-hetzner-dev-vm-1"

    def test_the_lane_is_driven_through_squad_host_tmux(self):
        """The door is squad's own socket on the HOST — never docker, and
        the session is the lane's name."""
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(
            actions={"vps-hetzner-dev-vm-1": [
                {"id": 3, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            seats=[_lane("vps-hetzner-dev-vm-1")],
        )
        r = Runner(_ok_capture())
        seat_control_pass(api, [], r, machine="m1")
        sent = r.sent()[0]
        assert sent[:3] == ["tmux", "-L", "squad"]
        assert "docker" not in sent
        assert sent[sent.index("-t") + 1] == "vps-hetzner-dev-vm-1"

    def test_spec_session_overrides_the_identity(self):
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(
            actions={"lane-a": [
                {"id": 3, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            seats=[_lane("lane-a", session="other-session")],
        )
        r = Runner(_ok_capture())
        seat_control_pass(api, [], r, machine="m1")
        s0 = r.sent()[0]
        assert s0[s0.index("-t") + 1] == "other-session"

    def test_a_placement_shadowed_identity_is_not_double_driven(self):
        """One identity, a placement AND a lane row: the placement owns the
        pane door. The lane leg typing into a same-named host session is the
        stale-shadow variant of the dt-poc double-registration collision."""
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(
            actions={"seat-a": [
                {"id": 3, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            seats=[_lane("seat-a")],
        )
        r = Runner(_ok_capture())
        seat_control_pass(api, [_pl("seat-a")], r, machine="m1")
        assert len(api.reported) == 1, "the action was realized twice"
        assert len(r.sent()) == 1, "two panes were typed into for one action"

    def test_a_stopped_placement_still_shadows_the_lane_leg(self):
        """Any placement row is the authority on the identity's door — a
        stopped seat must not become drivable through a same-named host
        session."""
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(
            actions={"seat-a": [
                {"id": 3, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            seats=[_lane("seat-a")],
        )
        r = Runner(_ok_capture())
        seat_control_pass(api, [_pl("seat-a", desired="stopped")], r,
                          machine="m1")
        assert api.reported == []
        assert not r.sent()

    def test_non_lane_discovered_seats_are_ignored(self):
        """A docker seat with no placement is NOT drivable via discovery —
        without a placement the edge cannot know its door, and guessing is
        the blind-keystroke class."""
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(
            actions={"docker-seat": [
                {"id": 3, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            seats=[{"identity": "docker-seat", "machine": "m1",
                    "spec": {"image": "img:tag"}}],
        )
        r = Runner(_ok_capture())
        seat_control_pass(api, [], r, machine="m1")
        assert api.reported == []
        assert not r.calls

    def test_lane_discovery_failure_does_not_stop_placement_seats(self):
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(
            actions={"seat-a": [
                {"id": 7, "kind": "interrupt", "args": {}, "status": "pending"},
            ]},
            fail_seats=True,
        )
        out = seat_control_pass(api, [_pl("seat-a")], Runner(_ok_capture()),
                                machine="m1")
        assert api.reported == [("seat-a", 7, "done")]
        assert any("lane discovery" in e for e in out["errors"])

    def test_no_machine_means_no_discovery(self):
        """Callers that predate the lane leg (or tests driving placements
        only) must not need a pull_seats on their api object."""
        from mcp_hub.edge import seat_control_pass

        api = FakeApi()  # has no pull_seats at all
        out = seat_control_pass(api, [], Runner(_ok_capture()))
        assert out["errors"] == []

    def test_a_watched_lane_streams_its_pane(self):
        from mcp_hub.edge import seat_control_pass

        api = LaneApi(watched=["lane-a"], seats=[_lane("lane-a")])
        seat_control_pass(api, [], Runner(_ok_capture("live")), machine="m1")
        assert [s for s, _ in api.panes] == ["lane-a"]


# ---------------------------------------------------------------------------
# Submission is WITNESSED, never assumed — the 2026-08-28 live-fire finding.
# Two console prompts at an idle lane reported "done" while the text sat in
# the input box unsubmitted, and the lane's next wake discarded the draft.
# ---------------------------------------------------------------------------


class TestPromptSubmissionWitness:
    def test_text_left_in_the_input_box_is_FAILED_not_done(self):
        r = Runner(captures=_stuck("status?"))
        out = realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "status?"}}, SEAT, r
        )
        assert out["status"] == "failed", out
        assert "input box" in out["observed"]["why"]
        assert "status?" in out["pane_after"]  # the evidence travels

    def test_text_visible_nowhere_is_FAILED_unverified(self):
        """An empty box is not proof of submission — fire 2's after-frame was
        captured before the TUI had rendered the keys at all."""
        r = Runner(captures=["● ready\n❯ ", "● ready\n❯ "])
        out = realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "status?"}}, SEAT, r
        )
        assert out["status"] == "failed", out
        assert "unverified" in out["observed"]["why"]

    def test_text_as_a_transcript_line_is_done(self):
        r = Runner(captures=_submitted("status?"))
        out = realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "status?"}}, SEAT, r
        )
        assert out["status"] == "done", out

    def test_a_long_prompt_is_matched_on_its_first_line_prefix(self):
        """The transcript wraps and clips long text; the witness must not
        demand the whole string on one line."""
        text = "first line of a long prompt that will surely wrap\nsecond line"
        after = "❯ first line of a long prompt that\n  will surely wrap\n❯ "
        r = Runner(captures=["● ready\n❯ ", after])
        out = realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": text}}, SEAT, r
        )
        assert out["status"] == "done", out

    def test_the_realizer_settles_before_enter_and_before_capture(
        self, no_real_sleep
    ):
        """Measured: text and Enter in the same instant left the text
        unsubmitted on an idle lane; a 1s pause before Enter submitted."""
        from mcp_hub.edge import SEAT_SETTLE_SECONDS

        r = Runner(captures=_submitted("status?"))
        realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "status?"}}, SEAT, r
        )
        assert no_real_sleep.count(SEAT_SETTLE_SECONDS) >= 2, no_real_sleep
        assert SEAT_SETTLE_SECONDS >= 0.5

    def test_the_pause_sits_between_text_and_enter(self):
        order: list[str] = []

        class OrderRunner(Runner):
            def __call__(self, cmd, cwd=None):  # noqa: ANN001
                if "send-keys" in cmd:
                    order.append("Enter" if cmd[-3:-2] == ["Enter"] or "Enter" in cmd else "text")
                return super().__call__(cmd, cwd)

        r = OrderRunner(captures=_submitted("status?"))
        realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "status?"}}, SEAT, r,
            pause=lambda s: order.append("pause"),
        )
        assert order.index("text") < order.index("pause") < order.index("Enter"), order


# ---------------------------------------------------------------------------
# The argv SHAPE — tmux parses it, the fake runner does not. Three console
# fires (2026-08-28) typed the prompt plus `-tmcp-hub-dev-vm-1` into OTHER
# lanes' panes because `-t <session>` trailed the `-l` literal.
# ---------------------------------------------------------------------------


class TestTmuxArgvShape:
    @pytest.mark.parametrize("seat", [
        SEAT,
        {"identity": "dt-poc", "substrate": "docker", "session": "seat"},
        {"identity": "lane-a", "substrate": "lane", "session": "lane-a"},
    ])
    def test_the_literal_text_is_the_LAST_argv_element(self, seat):
        r = Runner(
            [(["docker", "exec"], 0, "● ready\n❯ ")],
            captures=_submitted("hello there"),
        )
        realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "hello there"}},
            seat, r,
        )
        literal = [c for c in r.sent() if "-l" in c][0]
        assert literal[-1] == "hello there", (
            "something follows the literal text — tmux would TYPE it: "
            f"{literal!r}"
        )
        assert literal.index("-t") < literal.index("-l"), (
            f"-t must be parsed before -l switches to literal mode: {literal!r}"
        )

    def test_the_target_follows_the_subcommand_on_every_tmux_call(self):
        r = Runner(captures=_submitted("hi"))
        realize_seat_action(
            {"id": 2, "kind": "prompt", "args": {"text": "hi"}}, SEAT, r
        )
        for c in r.calls:
            sub = c.index("send-keys") if "send-keys" in c else c.index("capture-pane")
            assert c[sub + 1] == "-t" and c[sub + 2] == SEAT["session"], c

    def test_interrupt_is_not_subject_to_the_prompt_witness(self):
        """Escape leaves nothing to find in the pane; its witness is the
        pane itself, unchanged from phase 1."""
        r = Runner(_ok_capture())
        out = realize_seat_action(
            {"id": 1, "kind": "interrupt", "args": {}}, SEAT, r
        )
        assert out["status"] == "done", out
