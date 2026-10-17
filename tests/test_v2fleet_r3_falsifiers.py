"""v2-fleet revision 3 falsifiers (Vortex project v2-fleet, built in mcp-hub's repo).

F2 again (j6-squad-who-quiet-poll), at the LIVE FLEET'S SIZE. r2's build (b-28) made
`squad who --watch` quiet: changes only into the journal, a poll that backs off from
SQUAD_WHO_INTERVAL to SQUAD_WHO_MAX_INTERVAL while nothing moves. features-json's check at
46 lanes failed T15 all the same: alone, the watcher cost 0.208 of a core (limit 0.1) and a
lane going down took 81 s to reach the journal (limit 60). A quiet pass costs ~0.25 s of CPU
per lane, because every pass scans every pane. The backoff spread the scan; it did not make
it cheaper. r3 makes the PASS cheaper. A longer cap cannot pass these: the cap enters both
bounds.

T15 at fleet size, as measured here (one scratch fleet of 46 lanes, the DEFAULT intervals):
  * CPU per quiet pass: the watcher's own CPU plus its reaped children's plus its private
    tmux server's, between two consecutive waits. PROCESS CPU TIME, never wall time, so a
    loaded box does not decide the verdict.
  * 0.1 core at the cap:      cpu_per_pass <= 0.1 * cap.  (The real ratio is
    cpu / (cap + pass wall); leaving the pass's wall out makes the bound stricter and keeps a
    loaded box's slow wall clock out of it.)
  * a change within 60 s:     cap + 2 * cpu_per_pass <= 60.  The worst case is a change just
    after its lane was read: the rest of that pass, the wait at the cap, and the next pass up
    to that lane. A pass is fork- and CPU-bound, so its CPU time stands in for its wall time
    on an unloaded box.
  * a change is journaled within two passes of happening (lane down, lane up, and a pane's
    text going idle -> working), naming the lane and its new state.
  * quiet passes write nothing (stdout and stderr).

How it runs without real time: the watcher waits between passes with ONE external
`sleep <seconds>` per pass (r2's contract). The `sleep` on PATH here is a wrapper that
records the requested seconds and, with no fork of its own, the watcher's and the tmux
server's CPU counters from /proc, then returns at once. So the default waits (10 s doubling
to the cap) are read exactly, the passes run back to back, and each pass's CPU is the
difference between two records. SQUAD_BOARD_USAGE_TTL=0 charges the board-usage refresh
(outside the who arm) to every pass, where a live box pays it about every other pass at the
cap: conservative.

Guards (pass on the base, b-28's F2 code, and on any good build): the first pass names all
46 lanes; quiet passes write nothing; changes are journaled within two passes; the waits back
off to a stable cap and drop back on a change; one-shot `squad who` still reports every lane.
The build's byte-level guard on squad/squad and its frozen-paths list proved the build's scope and stay in its record, not here.

Hermetic: tmp dirs only. squad runs on a PRIVATE tmux socket under a private short
TMUX_TMPDIR, with HOME, SQUAD_CONF, MCP_HUB_STATE_DIR and a fake docker in tmp dirs; the tmux
wrapper refuses `-S <socket>` so the box-wide inventory never lists the live fleet's sockets.
The watcher runs in its own process group and is stopped by that group's id. Nothing here
reaches the live fleet, a real ~/.mcp-hub, or the hub. tmux is required (fail, not skip).
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
import os
import re
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

N_LANES = 46                       # the live fleet's size at features-json's check (hub 35318)
LANES = tuple(f"lane{i:02d}" for i in range(N_LANES))
DOWN_AT_START = "lane45"           # comes up during the run
GOES_DOWN = "lane01"
GOES_WORKING = "lane02"            # its pane's text changes: idle -> working
CORE_LIMIT = 0.1                   # T15: under 0.1 of a core
MINUTE_S = 60.0                    # T15: a change within a minute
QUIET_WAITS = 6                    # waits recorded before anything changes (reaches a 45 s cap twice)
PHASE_DEADLINE_S = 900.0           # a phase that cannot finish fails by assertion, never hangs
IDLE_PANE = "echo ready; exec sleep 3600"   # a blank pane ends a pass under pipefail
CLK = os.sysconf("SC_CLK_TCK")


def _boot_now():
    # /proc/uptime is CLOCK_BOOTTIME; the sleep wrapper stamps its records from it with no fork
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _fleet(tmp, lanes, up):
    home = tmp / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    # who reads the recap cache with a bare redirect; on a fresh HOME a missing file is a
    # stderr line every pass, which no live box has.
    (home / ".mcp-hub" / "board-recap.cache").write_text("")
    work = tmp / "w"
    work.mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("".join(f"{a}|{work}||--continue\n" for a in lanes), encoding="utf-8")
    bindir = tmp / "bin"
    bindir.mkdir()
    real_tmux = shutil.which("tmux")
    # Refuses `-S <socket>`: the box-wide inventory walks /tmp/tmux-$UID, the LIVE fleet's sockets.
    (bindir / "tmux").write_text('#!/bin/sh\ncase "$1" in -S) exit 1 ;; esac\n'
                                 f'exec {real_tmux} "$@"\n')
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n")
    # The wait recorder: `W <boottime> <args> | <watcher stat> | <server stat>`, builtins only
    # (no fork), then return at once. Its own few CPU ticks land in the next pass: counted.
    waits, pids = tmp / "waits", tmp / "pids"
    (bindir / "sleep").write_text(
        "#!/bin/sh\n"
        f'read -r up _ < /proc/uptime\n'
        f'w=; s=; [ -r {pids} ] && read -r w s < {pids}\n'
        'ws=; ss=\n'
        '[ -n "$w" ] && [ -r "/proc/$w/stat" ] && read -r ws < "/proc/$w/stat"\n'
        '[ -n "$s" ] && [ -r "/proc/$s/stat" ] && read -r ss < "/proc/$s/stat"\n'
        f'printf "W %s %s | %s | %s\\n" "$up" "$*" "$ws" "$ss" >> {waits}\n'
        "exit 0\n")
    for f in ("tmux", "docker", "sleep"):
        (bindir / f).chmod(0o755)
    # A short private TMUX_TMPDIR: a socket path past ~100 bytes cannot be bound.
    tmpdir = Path(tempfile.mkdtemp(prefix="v2f3-"))
    sock = f"r3who-{uuid.uuid4().hex[:8]}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
    env.update(HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET=sock, TMUX_TMPDIR=str(tmpdir),
               PATH=f"{bindir}:{os.environ['PATH']}", MCP_HUB_STATE_DIR=str(home / ".mcp-hub"),
               LANG="C.UTF-8", SQUAD_BOARD_USAGE_TTL="0")
    for k in ("SQUAD_WHO_INTERVAL", "SQUAD_WHO_MAX_INTERVAL"):
        env.pop(k, None)           # the DEFAULT intervals
    t = [real_tmux, "-L", sock]
    tenv = {**env, "PATH": os.environ["PATH"]}
    for a in up:
        subprocess.run(t + ["new-session", "-d", "-s", a, IDLE_PANE], check=True, env=tenv)
    return env, t, tenv, tmpdir, waits, pids


def _cpu_ticks(stat):
    if not stat.strip():
        return None
    f = stat.rsplit(")", 1)[1].split()
    return [int(x) for x in f[11:15]]     # utime stime cutime cstime


def _waits(log):
    """[(boottime, seconds, watcher ticks incl. reaped children, server ticks)] per wait."""
    out = []
    for ln in (log.read_text().splitlines() if log.exists() else []):
        head, _, rest = ln.partition(" | ")
        parts = head.split()
        if len(parts) != 3 or parts[0] != "W":
            continue                     # a sleep with other arguments is not a wait
        ws, _, ss = rest.partition(" | ")
        w, s = _cpu_ticks(ws), _cpu_ticks(ss)
        try:
            out.append((float(parts[1]), float(parts[2]),
                        sum(w) if w else None, (s[0] + s[1]) if s else None))
        except ValueError:
            pass
    return out


def _wait_for(log, n, deadline, proc=None):
    """True once n waits are recorded; False at the deadline or as soon as the watcher exits."""
    while time.time() < deadline:
        if len(_waits(log)) >= n:
            return True
        if proc is not None and proc.poll() is not None:
            return len(_waits(log)) >= n
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def fleet_run(tmp_path_factory):
    # Not a skip: a verdict image without tmux would pass with nothing run.
    assert shutil.which("tmux"), "these falsifiers need tmux on PATH (apt-get install tmux)"
    tmp = tmp_path_factory.mktemp("r3who")
    up = [a for a in LANES if a != DOWN_AT_START]
    env, t, tenv, tmpdir, waits, pids = _fleet(tmp, LANES, up)
    srv = subprocess.run(t + ["display-message", "-p", "#{pid}"], capture_output=True, text=True,
                         env=tenv, check=True).stdout.strip()
    out, err = tmp / "stdout", tmp / "stderr"
    rec = {"lanes_up": up}
    proc = None
    try:
        with open(out, "wb") as fo, open(err, "wb") as fe:
            proc = subprocess.Popen(["bash", str(SQUAD), "who", "--watch"], stdout=fo, stderr=fe,
                                    stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        pids.write_text(f"{proc.pid} {srv}\n")
        # first pass
        rec["first_ok"] = _wait_for(waits, 1, time.time() + PHASE_DEADLINE_S, proc)
        rec["first"] = out.read_bytes()
        # quiet passes
        rec["quiet_ok"] = _wait_for(waits, QUIET_WAITS, time.time() + PHASE_DEADLINE_S, proc)
        rec["quiet"] = out.read_bytes()
        rec["quiet_err"] = err.read_bytes()
        # three changes at once: one lane down, one up, one pane's text idle -> working
        rec["t_change"] = _boot_now()
        n_before = len(_waits(waits))
        subprocess.run(t + ["kill-session", "-t", f"={GOES_DOWN}"], check=True, env=tenv)
        subprocess.run(t + ["new-session", "-d", "-s", DOWN_AT_START, IDLE_PANE], check=True, env=tenv)
        tty = subprocess.run(t + ["display-message", "-p", "-t", GOES_WORKING, "#{pane_tty}"],
                             capture_output=True, text=True, env=tenv, check=True).stdout.strip()
        with open(tty, "w") as f:      # the pane's own terminal: its screen text changes, nothing else
            f.write("\r\n* Thinking (esc to interrupt)\r\n")
        # the pass under way may miss them; the one after it may not
        rec["after_ok"] = _wait_for(waits, n_before + 2, time.time() + PHASE_DEADLINE_S, proc)
        rec["after"] = out.read_bytes()
        rec["settle_ok"] = _wait_for(waits, n_before + 3, time.time() + PHASE_DEADLINE_S, proc)
        rec["final"] = out.read_bytes()
        rec["final_err"] = err.read_bytes()
        rec["alive"] = proc.poll() is None
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=20)
        subprocess.run(t + ["kill-server"], capture_output=True, env=tenv)
        shutil.rmtree(tmpdir, ignore_errors=True)
    rec["waits"] = _waits(waits)
    return rec


def _lines(b):
    return [x for x in b.decode(errors="replace").splitlines() if x.strip()]


def _quiet(rec):
    """The waits recorded before any change, and the CPU (s) of each quiet pass between them."""
    qw = [w for w in rec["waits"] if w[0] < rec["t_change"]]
    assert rec["quiet_ok"] and len(qw) >= QUIET_WAITS, (
        f"only {len(qw)} passes completed before the deadline (needed {QUIET_WAITS})")
    assert all(w[2] is not None and w[3] is not None for w in qw), "CPU counters not recorded"
    cpu = [((b[2] - a[2]) + (b[3] - a[3])) / CLK for a, b in zip(qw, qw[1:])]
    return qw, cpu


def _cap(rec):
    qw, _ = _quiet(rec)
    secs = [w[1] for w in qw]
    cap = max(secs)
    assert secs.count(cap) >= 2 and secs[-1] == cap, f"the waits never settled at a cap: {secs}"
    return cap


# ======================================================================================
# Falsifiers: fail on the base (b-28's pass costs ~0.25 s per lane), pass with a cheaper pass
# ======================================================================================

def test_falsifier_t15_quiet_pass_cpu_at_the_cap_under_a_tenth_of_a_core(fleet_run):
    """T15 at 46 lanes: CPU per quiet pass (watcher + reaped children + its tmux server) <= 0.1 * cap."""
    _, cpu = _quiet(fleet_run)
    cap = _cap(fleet_run)
    per_pass = sum(cpu) / len(cpu)
    assert per_pass <= CORE_LIMIT * cap, (
        f"{N_LANES} lanes: a quiet pass costs {per_pass:.2f} s CPU ({per_pass / N_LANES:.3f} s a lane; "
        f"passes {[round(c, 2) for c in cpu]}); at the {cap:g} s cap that is {per_pass / cap:.3f} core "
        f"(limit {CORE_LIMIT}): the pass must get cheaper, not the cap longer")


def test_falsifier_t15_a_change_reaches_the_journal_within_a_minute_at_the_cap(fleet_run):
    """T15 at 46 lanes: worst case cap + 2 passes (pass time from its CPU) <= 60 s."""
    _, cpu = _quiet(fleet_run)
    cap = _cap(fleet_run)
    per_pass = sum(cpu) / len(cpu)
    worst = cap + 2 * per_pass
    assert worst <= MINUTE_S, (
        f"{N_LANES} lanes: cap {cap:g} s + 2 x {per_pass:.2f} s pass = {worst:.1f} s worst case for a "
        f"change to reach the journal (limit {MINUTE_S:g} s)")


# ======================================================================================
# Guards: pass on the base and on any good build
# ======================================================================================

def test_guard_first_pass_names_every_lane_at_fleet_size(fleet_run):
    assert fleet_run["first_ok"], "the watcher never finished a first pass"
    first = _lines(fleet_run["first"])
    for a in LANES:
        assert any(re.search(rf"\b{a}\b", ln) for ln in first), f"{a} not on the first pass"
    assert len(first) <= N_LANES + 2, f"first pass wrote {len(first)} lines for {N_LANES} lanes"
    down = [ln for ln in first if re.search(rf"\b{DOWN_AT_START}\b", ln)]
    assert down and "down" in down[0], down


def test_guard_quiet_passes_write_nothing(fleet_run):
    _quiet(fleet_run)
    q = _lines(fleet_run["quiet"])
    f = _lines(fleet_run["first"])
    assert fleet_run["quiet"] == fleet_run["first"], f"{len(q) - len(f)} lines journaled while nothing changed: {q[len(f):][:5]}"
    assert fleet_run["quiet_err"] == b"", fleet_run["quiet_err"][:400]


def test_guard_changes_are_journaled_within_two_passes_naming_lane_and_state(fleet_run):
    assert fleet_run["alive"], "the watcher died"
    assert fleet_run["after_ok"], "the watcher did not complete two passes after the changes"
    new = _lines(fleet_run["after"][len(fleet_run["quiet"]):])
    want = {GOES_DOWN: "down", DOWN_AT_START: "idle", GOES_WORKING: "working"}
    for lane, state in want.items():
        hits = [ln for ln in new if re.search(rf"\b{lane}\b", ln)]
        assert hits and state in hits[-1], f"{lane} -> {state} not journaled within two passes: {new}"
    others = [ln for ln in _lines(fleet_run["final"][len(fleet_run["quiet"]):])
              if not any(re.search(rf"\b{x}\b", ln) for x in want)]
    assert not others, f"lanes that did not change were journaled: {others[:5]}"
    assert b"\x1b[2J" not in fleet_run["final"], "screen-clearing codes written to the journal"
    assert fleet_run["final_err"] == b"", fleet_run["final_err"][:400]


def test_guard_waits_back_off_to_a_cap_and_drop_back_on_a_change(fleet_run):
    qw, _ = _quiet(fleet_run)
    secs = [w[1] for w in qw]
    assert all(b >= a for a, b in zip(secs, secs[1:])), f"waits shrank with nothing changing: {secs}"
    cap = _cap(fleet_run)
    assert secs[0] < cap, f"no backoff: {secs}"
    assert fleet_run["settle_ok"], "the watcher did not settle after the changes"
    after = [w[1] for w in fleet_run["waits"] if w[0] >= fleet_run["t_change"]]
    assert all(v <= cap for v in after), f"a wait past the cap: {after}"
    assert any(v == secs[0] for v in after[:2]), f"no drop back to the base wait {secs[0]:g} after a change: {after}"


def test_guard_one_shot_who_still_reports_every_lane(tmp_path):
    assert shutil.which("tmux"), "these falsifiers need tmux on PATH (apt-get install tmux)"
    lanes = ("alpha", "beta", "gamma")
    env, t, tenv, tmpdir, _, _ = _fleet(tmp_path, lanes, ("alpha", "beta"))
    try:
        p = subprocess.run(["bash", str(SQUAD), "who"], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=120)
    finally:
        subprocess.run(t + ["kill-server"], capture_output=True, env=tenv)
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert p.returncode == 0, p.stderr
    rows = {ln.split()[1]: ln for ln in p.stdout.splitlines() if len(ln.split()) > 1 and ln.split()[1] in lanes}
    assert set(rows) == set(lanes), p.stdout
    assert "idle" in rows["alpha"] and "idle" in rows["beta"] and "down" in rows["gamma"]
