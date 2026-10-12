"""`squad who --watch`'s quiet poll (v2-fleet F2, j6-squad-who-quiet-poll).

The watcher runs for weeks under systemd, so the two things it must get right
are what reaches the journal (changes only) and what it costs while nothing is
happening (one pass per backed-off wait). Both are observed from OUTSIDE: the
journal from the process's stdout, the waits from a `sleep` wrapper on PATH.
Timings a busy box skews are never the measurement.

Hermetic, and deliberately so — this file drives the REAL squad script:
  * a private tmux socket under a private TMUX_TMPDIR (`mkdtemp`, because a
    socket path much past 100 bytes cannot be bound),
  * a `tmux` wrapper that refuses `-S <socket>`, so the box-wide inventory
    (which walks /tmp/tmux-$UID) can never reach the live fleet,
  * HOME, SQUAD_CONF, MCP_HUB_STATE_DIR and `docker` all inside tmp_path.
Every child runs in its own process group and is stopped by that group's id.
Nothing here reaches the live fleet, a real ~/.mcp-hub, or the hub.
"""
import os
import pty
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SQUAD = ROOT / "squad" / "squad"

BASE_S, CAP_S = 0.3, 1.2        # SQUAD_WHO_INTERVAL / SQUAD_WHO_MAX_INTERVAL
QUIET_S = 6.0                   # nothing moves for this long after the start
LANES = ("alpha", "beta", "gamma")
IDLE_PANE = "echo ready; exec sleep 600"   # a blank pane ends a pass under pipefail
WORKING_PANE = "echo '* Thinking (esc to interrupt)'; exec sleep 600"   # classify_text: working
DOCKER_STUB = "#!/bin/sh\nexit 0\n"      # no container is alive, nothing to capture

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="squad needs tmux")


class Fleet:
    """A throwaway squad: a roster, a private socket, and wrapped tools."""

    def __init__(self, tmp, up=("alpha", "beta"), pane=IDLE_PANE, lanes=LANES,
                 roster_args=None, docker=DOCKER_STUB, trace=False, tools=()):
        self.tmp = Path(tmp)
        self.lanes = tuple(lanes)
        home = self.tmp / "home"
        (home / ".config" / "squad").mkdir(parents=True)
        (home / ".mcp-hub").mkdir()
        self.home = home
        self.hub = home / ".mcp-hub"
        # `who` reads the recap cache with a bare redirect: on a fresh HOME a
        # missing file is a stderr line every pass, which no live box has.
        (home / ".mcp-hub" / "board-recap.cache").write_text("")
        work = self.tmp / "w"
        work.mkdir()
        conf = home / ".config" / "squad" / "squad.conf"
        args = dict(roster_args or {})
        conf.write_text("".join(f"{a}|{work}||{args.get(a, '--continue')}\n" for a in self.lanes),
                        encoding="utf-8")

        self.real_tmux = shutil.which("tmux")
        self.sleeps = self.tmp / "sleeps"
        # Every tmux call the WATCHER makes, and the waits between them, in one
        # file: what a pass costs in client calls is then readable from outside
        # the process, exactly as the waits already are. Only with trace=True —
        # a log line is a write every other test would pay for.
        self.trace = self.tmp / "calls"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        log_tmux = f'printf "%s\\n" "$*" >> {self.trace}\n' if trace else ""
        log_sleep = f'printf "SLEEP %s\\n" "$*" >> {self.trace}\n' if trace else ""
        (bindir / "tmux").write_text(
            "#!/bin/sh\n"
            'case "$1" in -S) exit 1 ;; esac\n'      # never the live fleet's sockets
            f'{log_tmux}'
            f'exec {self.real_tmux} "$@"\n')
        # A `{trace}` in the docker stub is the trace file: a test that counts
        # docker calls writes them where the passes are already separated.
        (bindir / "docker").write_text(docker.replace("{trace}", str(self.trace)))
        # Logging wrappers for the tools a pass must NOT fork per lane. They
        # exec the real tool, so behaviour is unchanged and only the count is
        # observable — the same way the waits are read from `sleep`.
        for tool in tools:
            real = shutil.which(tool)
            assert real, tool
            (bindir / tool).write_text(
                "#!/bin/sh\n"
                f'printf "TOOL {tool} %s\\n" "$*" >> {self.trace}\n'
                f'exec {real} "$@"\n')
            (bindir / tool).chmod(0o755)
        (bindir / "sleep").write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" "$(date +%s.%N)" "$*" >> {self.sleeps}\n'
            f'{log_sleep}'
            f'exec {shutil.which("sleep")} "$@"\n')
        for tool in ("tmux", "docker", "sleep"):
            (bindir / tool).chmod(0o755)

        self.tmuxdir = Path(tempfile.mkdtemp(prefix="whopoll-"))
        self.sock = f"whopoll-{uuid.uuid4().hex[:8]}"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
        self.env.update(HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET=self.sock,
                        TMUX_TMPDIR=str(self.tmuxdir),
                        PATH=f"{bindir}:{os.environ['PATH']}",
                        MCP_HUB_STATE_DIR=str(home / ".mcp-hub"))
        # the roster sessions are started with the REAL tmux and the real PATH:
        # a pane that inherited the wrappers would log the watcher's sleeps
        self.tool_env = {**self.env, "PATH": os.environ["PATH"]}
        self.pane = pane
        self.procs = []
        for lane in up:
            self.up(lane)

    def tmux(self, *args, check=True):
        return subprocess.run([self.real_tmux, "-L", self.sock, *args], check=check,
                              capture_output=True, text=True, env=self.tool_env, timeout=30)

    def up(self, lane):
        self.tmux("new-session", "-d", "-s", lane, self.pane)

    def down(self, lane):
        self.tmux("kill-session", "-t", f"={lane}")

    def watch(self, stdout, stderr, base=BASE_S, cap=CAP_S):
        env = {**self.env, "SQUAD_WHO_INTERVAL": str(base), "SQUAD_WHO_MAX_INTERVAL": str(cap)}
        proc = subprocess.Popen(["bash", str(SQUAD), "who", "--watch"], stdout=stdout,
                                stderr=stderr, stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True)
        self.procs.append(proc)
        return proc

    def waits(self):
        """Every one-argument `sleep` the watcher ran, as (when, seconds)."""
        out = []
        for row in (self.sleeps.read_text().splitlines() if self.sleeps.exists() else []):
            parts = row.split()
            if len(parts) == 2:
                try:
                    out.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    pass
        return out

    def close(self):
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        self.tmux("kill-server", check=False)
        shutil.rmtree(self.tmuxdir, ignore_errors=True)


@pytest.fixture
def fleet(tmp_path):
    made = []

    def build(**kw):
        made.append(Fleet(tmp_path / f"f{len(made)}", **kw))
        return made[-1]

    yield build
    for one in made:
        one.close()


def lines(blob):
    text = blob.decode(errors="replace") if isinstance(blob, bytes) else blob
    return [ln for ln in text.splitlines() if ln.strip()]


def wait_until(predicate, timeout=25.0, tick=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(tick)
    return None


# ----------------------------------------------------------------------------
# one long run, read many ways (a watcher start is the expensive part)
# ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if not shutil.which("tmux"):
        pytest.skip("squad needs tmux")
    tmp = tmp_path_factory.mktemp("run")
    f = Fleet(tmp)
    rec = {}
    out, err = tmp / "out", tmp / "err"
    try:
        with open(out, "wb") as fo, open(err, "wb") as fe:
            proc = f.watch(fo, fe)
        assert wait_until(lambda: out.read_bytes()), "the watcher journaled nothing"
        time.sleep(0.4)
        rec["first"] = out.read_bytes()

        time.sleep(QUIET_S)
        # A loaded box makes each pass slower, so a fixed quiet window can
        # hold fewer than the four waits the backoff test reads. Quiet is
        # quiet for as long as it lasts: wait for the waits, not the clock.
        wait_until(lambda: len(f.waits()) >= 5, timeout=30.0)
        rec["quiet"] = out.read_bytes()
        rec["quiet_err"] = err.read_bytes()
        rec["t_down"] = time.time()

        f.down("beta")
        seen = wait_until(lambda: b"beta" in out.read_bytes()[len(rec["quiet"]):])
        rec["down_seen"] = bool(seen)
        time.sleep(CAP_S * 2)
        wait_until(lambda: len([w for w, _ in f.waits() if w > rec["t_down"]]) >= 2, timeout=30.0)
        rec["down"] = out.read_bytes()
        rec["t_up"] = time.time()

        f.up("beta")
        seen = wait_until(lambda: b"beta" in out.read_bytes()[len(rec["down"]):])
        rec["up_seen"] = bool(seen)
        time.sleep(CAP_S * 2)
        wait_until(lambda: len([w for w, _ in f.waits() if w > rec["t_up"]]) >= 2, timeout=30.0)
        rec["final"] = out.read_bytes()
        rec["err"] = err.read_bytes()
        rec["alive"] = proc.poll() is None
        rec["waits"] = f.waits()
        rec["sleeps_raw"] = f.sleeps.read_text() if f.sleeps.exists() else ""
    finally:
        f.close()
    return rec


def test_the_first_pass_names_every_lane_and_its_state(run):
    assert run["alive"], "the watcher died"
    first = lines(run["first"])
    assert len(first) == len(LANES), first
    assert first == ["who: alpha idle", "who: beta idle", "who: gamma down"], first


def test_a_quiet_fleet_journals_nothing_at_all(run):
    assert run["quiet"] == run["first"], lines(run["quiet"][len(run["first"]):])


def test_stderr_stays_empty_throughout(run):
    assert run["quiet_err"] == b"", run["quiet_err"][:400]
    assert run["err"] == b"", run["err"][:400]


def test_a_lane_going_down_journals_one_line_naming_both_states(run):
    assert run["down_seen"], "beta going down never reached the journal"
    new = lines(run["down"][len(run["quiet"]):])
    assert new == ["who: beta idle -> down"], new


def test_the_same_lane_coming_back_journals_the_return(run):
    assert run["up_seen"], "beta coming back never reached the journal"
    new = lines(run["final"][len(run["down"]):])
    assert new == ["who: beta down -> idle"], new


def test_no_screen_clearing_or_header_ever_reaches_the_journal(run):
    blob = run["final"]
    assert b"\x1b[2J" not in blob and b"\x1b[H" not in blob
    assert b"squad who \xe2\x80\x94" not in blob, "the terminal header reached the journal"
    for row in lines(blob):
        assert row.startswith("who: "), row


def test_the_wait_doubles_while_quiet_and_stops_at_the_cap(run):
    quiet = [s for when, s in run["waits"] if when < run["t_down"]]
    assert len(quiet) >= 4, quiet
    assert quiet[0] == BASE_S, f"the first wait is not the base interval: {quiet}"
    assert quiet[:4] == [BASE_S, BASE_S * 2, CAP_S, CAP_S], quiet
    assert all(s <= CAP_S for s in [s for _, s in run["waits"]]), run["waits"]


def test_every_change_drops_the_wait_back_to_the_base(run):
    for label, since in (("down", run["t_down"]), ("up", run["t_up"])):
        after = [s for when, s in run["waits"] if when > since]
        assert len(after) >= 2, (label, after)
        # the pass already under way when the lane moved may still sleep the
        # backed-off wait; the pass that SAW the move waits the base again
        assert BASE_S in after[:2], (label, after)


def test_every_wait_is_one_sleep_with_one_argument(run):
    """The falsifiers read the backoff from these calls, so the loop must wait
    with a single `sleep <seconds>` — a second sleep, or a flag, and the poll's
    cost stops being readable from outside the process."""
    logged = [row.split() for row in lines(run["sleeps_raw"])]
    assert logged, "no waits logged at all"
    assert all(len(row) == 2 for row in logged), logged[:5]
    assert len(logged) == len(run["waits"])


# ----------------------------------------------------------------------------
# what is NOT a state change
# ----------------------------------------------------------------------------

def test_context_and_model_churn_is_not_a_state_change(fleet, tmp_path):
    """ctx% and the model letter move constantly and reach the TAB, not the
    journal — counting them as state would make every pass a change again."""
    f = fleet(up=("alpha",), pane="echo 'Opus5 high · ctx [....] 14%'; exec cat")
    out, err = tmp_path / "ctx-out", tmp_path / "ctx-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: out.read_bytes()), "nothing journaled"
        time.sleep(0.4)
        before = out.read_bytes()
        assert "who: alpha idle" in lines(before), lines(before)
        # the pane's own statusline redraws with a new ctx % and model
        # send-keys is a pane-target command: no "=" prefix, as the script's
        # own note says
        f.tmux("send-keys", "-t", "alpha", "-l", "Fable5 high · ctx [......] 44%")
        f.tmux("send-keys", "-t", "alpha", "Enter")
        time.sleep(CAP_S * 3)
        after = out.read_bytes()
        assert proc.poll() is None, "the watcher died"
    assert after == before, lines(after[len(before):])
    assert err.read_bytes() == b""


def test_a_fleet_with_no_tmux_server_at_all_is_reported_not_fatal(fleet, tmp_path):
    """The known edge: with no server on the squad socket, who_badge's
    `list-clients | sort | paste` fails under pipefail. The watcher is the one
    caller that must outlive its whole fleet being down."""
    f = fleet(up=())
    out, err = tmp_path / "down-out", tmp_path / "down-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(lines(out.read_bytes())) >= len(LANES)), \
            f"out={out.read_bytes()!r} err={err.read_bytes()!r}"
        time.sleep(CAP_S * 3)
        assert proc.poll() is None, f"the watcher exited: {err.read_bytes()!r}"
    assert lines(out.read_bytes()) == [f"who: {a} down" for a in LANES]
    assert wait_until(lambda: len(f.waits()) >= 2, timeout=30.0), "the watcher stopped polling"


# ----------------------------------------------------------------------------
# a terminal still gets the board
# ----------------------------------------------------------------------------

def test_a_terminal_still_gets_the_cleared_screen_and_the_whole_board(fleet, tmp_path):
    f = fleet()
    primary, secondary = pty.openpty()
    err = tmp_path / "tty-err"
    try:
        with open(err, "wb") as fe:
            proc = f.watch(secondary, fe)
            os.close(secondary)
            blob = b""
            deadline = time.time() + 25.0
            # selectors, not select.select: under the full suite this fd can
            # sit above FD_SETSIZE, where select() refuses it outright
            sel = selectors.DefaultSelector()
            sel.register(primary, selectors.EVENT_READ)
            # two whole frames: a terminal watcher REPAINTS the board, it does
            # not diff — the clock in the header is the proof it is live
            # gamma is the board's last row, so twice means two whole frames
            while time.time() < deadline and blob.count(b"gamma") < 2:
                if not sel.select(timeout=0.5):
                    continue
                try:
                    chunk = os.read(primary, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                blob += chunk
            sel.close()
            assert proc.poll() is None, err.read_bytes()
    finally:
        os.close(primary)
        f.close()
    text = blob.decode(errors="replace")
    assert text.count("\x1b[2J") >= 2, text[:400]
    assert len(re.findall(r"squad who — \d\d:\d\d:\d\d", text)) >= 2
    for lane in LANES:
        assert len(re.findall(rf"\b{lane}\b", text)) >= 2, lane
    assert "who: alpha idle" not in text, "the journal form leaked onto a terminal"


# ----------------------------------------------------------------------------
# the one-shot pass, and the defaults the operator inherits
# ----------------------------------------------------------------------------

def test_one_shot_who_is_untouched(fleet):
    f = fleet()
    proc = subprocess.run(["bash", str(SQUAD), "who"], capture_output=True, text=True,
                          env=f.env, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    rows = {row.split()[1]: row for row in proc.stdout.splitlines()
            if len(row.split()) > 1 and row.split()[1] in LANES}
    assert set(rows) == set(LANES), proc.stdout
    assert "idle" in rows["alpha"] and "down" in rows["gamma"]
    assert "\x1b[" in proc.stdout, "the board lost its colour"
    assert "who: " not in proc.stdout, "one shot must print the board, not the diff"
    assert not f.sleeps.exists(), "a single pass must not wait at all"


def test_the_defaults_are_env_overridable_and_leave_room_inside_a_minute():
    src = SQUAD.read_text()
    base = re.findall(r"\$\{SQUAD_WHO_INTERVAL:-([0-9.]+)\}", src)
    cap = re.findall(r"\$\{SQUAD_WHO_MAX_INTERVAL:-([0-9.]+)\}", src)
    assert base and cap, (base, cap)
    # T15 wants every lane's state within a minute: the cap plus one pass has
    # to fit inside it, so the cap alone must leave room
    assert all(0 < float(x) <= 50 for x in cap), cap
    assert all(float(b) < float(c) for b in base for c in cap), (base, cap)


def test_the_loop_waits_exactly_once_per_pass():
    """One `sleep` in the loop, and the float doubling done by awk — bash
    cannot double 0.4 and a fractional interval is what the tests use."""
    src = SQUAD.read_text()
    arm = src.split("  who)      ", 1)[1].split("\n  board)", 1)[0]
    watch = arm.split('if [ -n "$wmode" ]; then', 1)[1]
    assert len(re.findall(r"(?m)^\s*sleep ", watch)) == 1, "not exactly one sleep per pass"
    assert "awk" in watch and "SQUAD_WHO_MAX_INTERVAL" in watch


# ----------------------------------------------------------------------------
# what a quiet pass COSTS — read from outside, in tmux client calls
#
# The backoff spread the scan over fewer passes; it did not make a pass
# cheaper. At the live fleet's 46 lanes a pass was ~10 forks a lane — 8 s of
# CPU, 0.18 of a core held while nothing happened, and ~80 s for one lane
# going down to reach the journal (features-json, hub 35318). CPU is measured
# at fleet size by the r3 falsifiers; what is asserted HERE is the shape that
# makes it cheap, which a 3-lane fleet shows just as well: the per-pass work
# does not scale with the roster.
# ----------------------------------------------------------------------------

def tmux_passes(trace):
    """The watcher's tmux calls, grouped into the passes the waits separate.

    `-L <socket>` leads every one of them (squad's tm()); it is stripped so a
    row reads as the tmux command it is. The box-wide inventory never appears:
    it calls `tmux -S`, which the wrapper refuses before it logs anything.
    """
    done, cur = [], []
    for row in trace.splitlines():
        if row.startswith("SLEEP "):
            done.append(cur)
            cur = []
        elif row.strip():
            parts = row.split(maxsplit=2)
            cur.append(parts[2] if parts[:1] == ["-L"] and len(parts) == 3 else row)
    return done


def verbs(calls):
    return sorted(c.split()[0] for c in calls)


@pytest.mark.parametrize("n", (2, 14))
def test_a_quiet_pass_makes_the_same_few_tmux_calls_at_any_fleet_size(fleet, tmp_path, n):
    """ONE list-panes for every lane's liveness, command and title · ONE call
    that captures every lane's screen · ONE list-clients for the badges. Three,
    whether the roster holds two lanes or forty-six — a count that grows per
    lane is the 46-lane failure coming back."""
    lanes = tuple(f"lane{i:02d}" for i in range(n))
    f = fleet(lanes=lanes, up=lanes, trace=True)
    out, err = tmp_path / f"n{n}-out", tmp_path / f"n{n}-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(f.waits()) >= 4, timeout=40.0), err.read_bytes()
        assert proc.poll() is None, err.read_bytes()
    passes = tmux_passes(f.trace.read_text())
    assert len(passes) >= 4, passes
    bulk = ["capture-pane", "list-clients", "list-panes"]
    # the first pass sets every tab's title (nothing is cached to compare to)
    # and is the only one that does
    assert verbs(passes[0]) == sorted(bulk + ["set"] * n), passes[0]
    for i, one in enumerate(passes[1:4], start=1):
        assert verbs(one) == bulk, (i, one)
    assert lines(out.read_bytes()) == [f"who: {a} idle" for a in lanes]
    assert err.read_bytes() == b""


def test_the_capture_is_one_call_naming_every_lane(fleet, tmp_path):
    """The shape of that one call: a tmux command SEQUENCE, one capture-pane
    per live lane, so 46 lanes cost one client and not 46."""
    lanes = tuple(f"lane{i:02d}" for i in range(5))
    f = fleet(lanes=lanes, up=lanes[:4], trace=True)
    out, err = tmp_path / "cap-out", tmp_path / "cap-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(f.waits()) >= 2, timeout=40.0)
        assert proc.poll() is None, err.read_bytes()
    caps = [c for one in tmux_passes(f.trace.read_text()) for c in one
            if c.startswith("capture-pane")]
    assert caps, f.trace.read_text()
    for call in caps:
        for lane in lanes[:4]:
            assert f" -t {lane} " in f"{call} ", (lane, call)
        # the lane that is down is not captured, and nothing is captured twice
        assert "lane04" not in call, call
        assert call.count("capture-pane") == 4, call
    assert err.read_bytes() == b""


def test_the_tab_title_is_written_only_when_it_changes(fleet, tmp_path):
    """who_badge runs every pass — the badge is the signal for an operator who
    is not reading this output — but a title that has not moved is not re-sent.
    It was a show AND a set per lane per pass: 92 tmux clients a pass at fleet
    size, for a string that changes when the operator is needed.
    """
    f = fleet(trace=True)
    out, err = tmp_path / "tab-out", tmp_path / "tab-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(f.waits()) >= 3, timeout=40.0)
        mark = len(f.waits())
        # 🔴 is the one state the TITLE spells out, so a dialog on alpha's
        # screen is a title that has to be re-sent (idle, working and down all
        # render the same string — and re-sending that would be the churn this
        # cache exists to stop)
        tty = f.tmux("display-message", "-p", "-t", "alpha", "#{pane_tty}").stdout.strip()
        with open(tty, "w") as pane:
            pane.write("\r\nDo you want to proceed?\r\n")
        assert wait_until(lambda: len(f.waits()) >= mark + 3, timeout=40.0)
        assert proc.poll() is None, err.read_bytes()
    passes = tmux_passes(f.trace.read_text())
    # the badge pass itself still happens every pass (its list-clients proves it)
    for i, one in enumerate(passes):
        assert verbs(one).count("list-clients") == 1, (i, one)
    sets = [[c for c in one if c.startswith("set ")] for one in passes]
    # first pass: every lane, including the down one — a down SQUAD lane keeps
    # its tab (only down faculty is skipped), and nothing is cached yet
    assert len(sets[0]) == len(LANES), sets[0]
    assert all("set-titles-string" in c for c in sets[0]), sets[0]
    moved = [i for i, one in enumerate(sets) if one]
    assert moved[0] == 0 and len(moved) == 2, sets
    assert sets[moved[1]] == [c for c in sets[moved[1]] if "-t alpha " in c], sets[moved[1]]
    assert "🔴" in sets[moved[1]][0], sets[moved[1]]
    assert err.read_bytes() == b""


# ----------------------------------------------------------------------------
# cheap must not mean blind
# ----------------------------------------------------------------------------

def test_a_pane_whose_text_turns_working_is_journaled(fleet, tmp_path):
    """Skipping the harvest for a lane whose screen did not change must still
    NOTICE one that did: nothing else moves here — same session, same command,
    same title, two new lines of pane text. And the gate cannot be elapsed
    time: these passes are a third of a second apart, the falsifiers' are
    milliseconds."""
    f = fleet(up=("alpha", "beta"))
    out, err = tmp_path / "txt-out", tmp_path / "txt-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(lines(out.read_bytes())) >= len(LANES))
        wait_until(lambda: len(f.waits()) >= 4, timeout=40.0)
        before = out.read_bytes()
        mark = len(f.waits())
        tty = f.tmux("display-message", "-p", "-t", "alpha", "#{pane_tty}").stdout.strip()
        with open(tty, "w") as pane:       # the pane's own terminal, nothing else
            pane.write("\r\n* Thinking (esc to interrupt)\r\n")
        assert wait_until(lambda: len(f.waits()) >= mark + 3, timeout=40.0)
        assert proc.poll() is None, err.read_bytes()
        after = out.read_bytes()
    assert lines(after[len(before):]) == ["who: alpha idle -> working"], \
        lines(after[len(before):])
    assert err.read_bytes() == b""


def test_a_lane_vanishing_mid_pass_never_moves_another_lane(fleet, tmp_path):
    """A target that dies between the scan and the capture ABORTS tmux's
    command sequence, so the lanes behind it in that sequence get no screen at
    all. They must be re-read one at a time — never credited with a blank
    capture, which classifies `idle`, and never left to end the watcher under
    pipefail. The flapped lane is FIRST in roster order, so the whole rest of
    the fleet sits behind it, and every other lane is `working`: a blank
    capture mistaken for a screen would show up as a move to idle."""
    lanes = tuple(f"n{i}" for i in range(6))
    f = fleet(lanes=lanes, up=lanes, pane=WORKING_PANE)
    out, err = tmp_path / "gone-out", tmp_path / "gone-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(lines(out.read_bytes())) >= len(lanes), timeout=40.0), \
            f"out={out.read_bytes()!r} err={err.read_bytes()!r}"
        assert lines(out.read_bytes()) == [f"who: {a} working" for a in lanes], \
            lines(out.read_bytes())
        for _ in range(8):
            f.down("n0")
            time.sleep(0.12)
            f.up("n0")
            time.sleep(0.12)
        wait_until(lambda: len(f.waits()) >= 3, timeout=40.0)
        assert proc.poll() is None, f"the watcher died: {err.read_bytes()!r}"
        journal = lines(out.read_bytes())
    stray = [ln for ln in journal[len(lanes):] if "n0" not in ln]
    assert not stray, f"lanes that never changed were journaled: {stray}"
    assert err.read_bytes() == b""


def test_a_container_seat_still_answers_from_its_own_substrate(fleet, tmp_path):
    """The bulk scan is a HOST-pane scan, and a seat whose claude lives in
    docker has no host session at all: asking tmux about one reported a
    running, ⚡-on-the-hub seat as `down` (2026-08-05). It keeps
    container_alive / capture_for, per lane, exactly as before."""
    docker = ("#!/bin/sh\n"
              'case "$1" in\n'
              "  ps)   echo seatbox ;;\n"
              "  exec) printf 'ready\\n* Thinking (esc to interrupt)\\n' ;;\n"
              "esac\n"
              "exit 0\n")
    lanes = ("alpha", "seat")
    f = fleet(lanes=lanes, up=("alpha",), roster_args={"seat": "@docker:seatbox"},
              docker=docker)
    out, err = tmp_path / "doc-out", tmp_path / "doc-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(lines(out.read_bytes())) >= 2, timeout=40.0), \
            f"out={out.read_bytes()!r} err={err.read_bytes()!r}"
        wait_until(lambda: len(f.waits()) >= 3, timeout=40.0)
        assert proc.poll() is None, err.read_bytes()
    assert lines(out.read_bytes()) == ["who: alpha idle", "who: seat working"], \
        lines(out.read_bytes())
    assert err.read_bytes() == b""


# ----------------------------------------------------------------------------
# r4: a quiet pass must not fork PER LANE
#
# r3 made the pass bulk — one list-panes, one capture, one list-clients — but
# left one question asked per COMMS lane: "is this lane's hub cache fresh, and
# what does it say?". That answer cost five forks each (`find`, `cat`, two
# `grep`s and a `date`), and at the live fleet's 31 comms lanes it was 0.67 s
# of CPU a pass — two thirds of the whole pass (measured 2026-10-12). Bash can
# read a 1 KB file and match a pattern itself; only the mtimes needed a
# process, and ONE `stat` answers for the whole directory.
#
# What is asserted here is the SHAPE that makes it cheap, read from outside in
# tool invocations. The CPU itself is measured at the live fleet's shape by the
# r4 falsifiers.
# ----------------------------------------------------------------------------

COMMS = "--dangerously-load-development-channels server:hub"
FRESH_STATUS = '{"online": true, "wakeable": true, "focus_until": 0}'


def tool_calls(trace, name):
    """[[args, ...] per pass] for one logged tool, passes split by the waits."""
    done, cur = [], []
    for row in trace.splitlines():
        if row.startswith("SLEEP "):
            done.append(cur)
            cur = []
        elif row.startswith(f"TOOL {name} "):
            cur.append(row[len(f"TOOL {name} "):])
    return done


def comms_fleet(fleet, n, **kw):
    """n comms lanes, each with a FRESH status cache the glyph can be read from."""
    lanes = tuple(f"lane{i:02d}" for i in range(n))
    f = fleet(lanes=lanes, up=lanes, roster_args={a: f"{COMMS} --continue" for a in lanes},
              trace=True, **kw)
    for lane in lanes:
        (f.hub / f"status-{lane}.json").write_text(FRESH_STATUS)
    return f, lanes


@pytest.mark.parametrize("n", (3, 12))
def test_a_quiet_pass_reads_every_status_cache_with_one_stat_and_no_other_fork(
        fleet, tmp_path, n):
    """The lever, at two fleet sizes: one `stat` for the directory, and NOT a
    `find`, a `cat` or a `date` per lane. The counts must be the same at 3
    lanes and at 12 — a count that grows per lane is the 46-lane failure."""
    f, lanes = comms_fleet(fleet, n, tools=("find", "cat", "stat", "date"))
    out, err = tmp_path / f"s{n}-out", tmp_path / f"s{n}-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(f.waits()) >= 5, timeout=60.0), err.read_bytes()
        assert proc.poll() is None, err.read_bytes()
    trace = f.trace.read_text()
    # pass 0 primes the tab titles and reads the hand-flag caches; judge the
    # quiet passes after it
    quiet = slice(1, 5)
    for args in tool_calls(trace, "find")[quiet]:
        assert not [a for a in args if "status-" in a], args
    for args in tool_calls(trace, "cat")[quiet]:
        assert not [a for a in args if "status-" in a], args
    stats = tool_calls(trace, "stat")[quiet]
    for args in stats:
        status_stats = [a for a in args if "status-" in a]
        assert len(status_stats) <= 1, status_stats
    assert any(args for args in stats), "the status caches were never stat'ed at all"
    # the only `date` in a pass is the harness's own clock in the sleep wrapper
    for args in tool_calls(trace, "date")[quiet]:
        assert [a for a in args if a != "+%s.%N"] == [], args
    assert lines(out.read_bytes()) == [f"who: {a} idle" for a in lanes]
    assert err.read_bytes() == b""


def test_the_glyph_still_follows_the_file_and_the_clock(fleet, tmp_path):
    """Cheap must not mean stale, and the glyph is NOT cached on an mtime: a
    file rewritten to offline reads ✖ on the next pass, a cache nobody rewrote
    ages into `?`, and a focus that has elapsed stops showing."""
    lanes = ("fresh", "stale", "offline", "focused", "elapsed", "nofile")
    f = fleet(lanes=lanes, up=lanes,
              roster_args={a: f"{COMMS} --continue" for a in lanes})
    now = int(time.time())
    (f.hub / "status-fresh.json").write_text(FRESH_STATUS)
    stale = f.hub / "status-stale.json"
    stale.write_text(FRESH_STATUS)
    os.utime(stale, (now - 200, now - 200))        # past `find -mmin -3`
    (f.hub / "status-offline.json").write_text('{"online": false, "wakeable": false}')
    (f.hub / "status-focused.json").write_text(
        '{"online": true, "wakeable": true, "focus_until": %d.5}' % (now + 1800))
    (f.hub / "status-elapsed.json").write_text(
        '{"online": true, "wakeable": true, "focus_until": %d.0}' % (now - 60))
    proc = subprocess.run(["bash", str(SQUAD), "who"], capture_output=True, text=True,
                          env=f.env, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    glyphs = {}
    for row in proc.stdout.splitlines():
        parts = row.split()
        if len(parts) > 2 and parts[1] in lanes:
            glyphs[parts[1]] = parts[2]
    assert glyphs == {"fresh": "⚡", "stale": "?", "offline": "✖",
                      "focused": "⚡🔕", "elapsed": "⚡", "nofile": "?"}, proc.stdout


def test_the_glyph_reads_each_key_not_any_later_true(fleet, tmp_path):
    """`*'"online"'*true*` matched ANY `true` after the key, so a lane the
    daemon reported offline-but-wakeable read ⚡, and so did one whose body
    carried "true" in some other field. The glyph and `ls` read each key."""
    lanes = ("offwake", "onnowake", "fresh")
    f = fleet(lanes=lanes, up=lanes,
              roster_args={a: f"{COMMS} --continue" for a in lanes})
    (f.hub / "status-offwake.json").write_text('{"online": false, "wakeable": true}')
    (f.hub / "status-onnowake.json").write_text(
        '{"online": true, "wakeable": false, "agent": "trueblue"}')
    (f.hub / "status-fresh.json").write_text(FRESH_STATUS)
    proc = subprocess.run(["bash", str(SQUAD), "who"], capture_output=True, text=True,
                          env=f.env, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    glyphs = {}
    for row in proc.stdout.splitlines():
        parts = row.split()
        if len(parts) > 2 and parts[1] in lanes:
            glyphs[parts[1]] = parts[2]
    assert glyphs == {"offwake": "✖", "onnowake": "⚠", "fresh": "⚡"}, proc.stdout
    ls = subprocess.run(["bash", str(SQUAD), "ls"], capture_output=True, text=True,
                        env=f.env, stdin=subprocess.DEVNULL, timeout=60)
    assert ls.returncode == 0, ls.stderr
    rows = {r.split()[0]: r for r in ls.stdout.splitlines()
            if r.split()[:1] and r.split()[0] in lanes}
    assert "✖ offline" in rows["offwake"], ls.stdout
    assert "online" in rows["onnowake"] and "⚡" not in rows["onnowake"], ls.stdout
    assert "⚡ online" in rows["fresh"], ls.stdout


def test_one_docker_ps_a_pass_however_many_container_seats(fleet, tmp_path):
    """Liveness was `docker ps --filter name=^<c>$` per seat, and a docker
    client start is ~50 ms of CPU: one listing answers for all of them, and
    nothing is remembered past the pass."""
    docker = ("#!/bin/sh\n"
              'printf "TOOL docker %s\\n" "$*" >> {trace}\n'
              'case "$1" in\n'
              '  ps)   printf "ctr-s0\\nctr-s1\\nctr-s2\\n" ;;\n'
              '  exec) shift; shift; case "$1" in\n'
              '          tmux) echo ready ;;\n'
              '          find) echo "$2" ;;\n'
              f'          cat) echo \'{FRESH_STATUS}\' ;;\n'
              '        esac ;;\n'
              'esac\nexit 0\n')
    lanes = ("host", "s0", "s1", "s2")
    f = fleet(lanes=lanes, up=("host",), docker=docker, trace=True,
              roster_args={"host": f"{COMMS} --continue",
                           **{a: f"@docker:ctr-{a}" for a in ("s0", "s1", "s2")}})
    (f.hub / "status-host.json").write_text(FRESH_STATUS)
    out, err = tmp_path / "ps-out", tmp_path / "ps-err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(f.waits()) >= 4, timeout=60.0), err.read_bytes()
        assert proc.poll() is None, err.read_bytes()
    calls = tool_calls(f.trace.read_text(), "docker")[1:4]
    assert calls, f.trace.read_text()
    for args in calls:
        listings = [a for a in args if a.startswith("ps")]
        assert len(listings) == 1, listings
        assert "--filter" not in listings[0], listings[0]
    # the seats are seen as UP by that one listing, not reported down
    assert lines(out.read_bytes()) == ["who: host idle", "who: s0 idle",
                                       "who: s1 idle", "who: s2 idle"], \
        lines(out.read_bytes())
    assert err.read_bytes() == b""


def test_the_hand_flags_are_not_re_read_on_every_pass(fleet, tmp_path):
    """🙋 comes out of caches OTHER processes write and moves on the order of
    minutes; re-reading it cost a transcript scan and two python starts every
    pass. 🔴 (a dialog on screen) is read from the pane every pass and is a
    different signal — this one is slowed, that one is not."""
    f, lanes = comms_fleet(fleet, 3, tools=("python3",))
    out, err = tmp_path / "hand-out", tmp_path / "hand-err"
    env_extra = {"SQUAD_BOARD_USAGE_TTL": "0"}      # charge the scan to every pass
    f.env.update(env_extra)
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = f.watch(fo, fe)
        assert wait_until(lambda: len(f.waits()) >= 7, timeout=90.0), err.read_bytes()
        assert proc.poll() is None, err.read_bytes()
    per_pass = [len(args) for args in tool_calls(f.trace.read_text(), "python3")[:7]]
    assert per_pass[0] >= 1, "the first pass must read them at all"
    assert sum(1 for n in per_pass if n == 0) >= 3, per_pass
    assert err.read_bytes() == b""


def test_the_default_base_wait_leaves_a_pass_room_inside_the_bounds():
    """r4 judges a quiet pass against the BASE wait, not the cap: every change
    drops the poll back to the base and the live fleet changes about every
    40 s, so that is where the watcher lives. The two bounds are
    pass <= 0.1 * base and cap + 2 * pass <= 60 s."""
    src = SQUAD.read_text()
    base = float(re.findall(r"\$\{SQUAD_WHO_INTERVAL:-([0-9.]+)\}", src)[0])
    cap = float(re.findall(r"\$\{SQUAD_WHO_MAX_INTERVAL:-([0-9.]+)\}", src)[0])
    assert base < cap
    budget = 0.1 * base
    assert budget >= 1.0, f"a {base:g} s base gives a pass only {budget:g} s of CPU"
    assert cap + 2 * budget <= 60.0, (cap, budget)
