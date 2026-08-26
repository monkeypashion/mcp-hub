#!/usr/bin/env node
// Claude Code status line — compact single line.
// Reads session JSON on stdin, prints one line. Node-based (no jq) for
// Windows reliability.
//
// The hub wakeability segment is read from a small cache file the mcp-hub
// heartbeat daemon writes every ~60s (~/.mcp-hub/status-<agent>.json). We do
// NO network here — just a file read — so the statusline stays instant even
// with refreshInterval polling.

const { execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

let raw = '';
process.stdin.on('data', (c) => (raw += c));
process.stdin.on('end', () => {
  let d = {};
  try { d = JSON.parse(raw); } catch { /* leave d empty */ }

  // ── ANSI helpers ───────────────────────────────────────────────
  const C = {
    reset: '\x1b[0m', dim: '\x1b[2m', bold: '\x1b[1m',
    blue: '\x1b[34m', magenta: '\x1b[35m',
    green: '\x1b[32m', yellow: '\x1b[33m', red: '\x1b[31m', cyan: '\x1b[36m',
  };
  const paint = (s, ...c) => `${c.join('')}${s}${C.reset}`;

  // Focus (do-not-disturb) as `🔕28m`, or '' when not focused. Mirrors the
  // hub's own _fmt_minutes ('45m' / '2h10m') so the same state reads the same
  // in list_agents(), on the board and here.
  //
  // Takes an EXPIRY and derives the remainder, so this is correct against a
  // snapshot of any age and an elapsed focus simply stops rendering — the
  // reason the hub stores an expiry rather than a flag applies identically to
  // every reader of it. A missing field (older daemon) yields '' rather than
  // a guess: absence of the instrument must not print a silencer.
  const fmtFocus = (until) => {
    const left = Math.floor((Number(until) || 0) - Date.now() / 1000);
    if (left <= 0) return '';
    const mins = Math.floor(left / 60);
    if (mins < 60) return `🔕${mins}m`;
    return `🔕${Math.floor(mins / 60)}h${String(mins % 60).padStart(2, '0')}m`;
  };

  // Default usage thresholds (5h): green < 50, yellow 50-79, red >= 80.
  const usageColor = (p) => (p >= 80 ? C.red : p >= 50 ? C.yellow : C.green);
  // ctx is more aggressive by request: red once past 50%.
  const ctxColor = (p) => (p >= 50 ? C.red : C.green);

  // 7d is coloured by PACE, not absolute usage (operator's ask): the question
  // is "have I used more than I should have by this point in the week".
  //
  // The budget is CONTINUOUS (operator's choice, 2026-08-04): the target is
  // exactly the fraction of the window elapsed, moving every refresh, with no
  // daily steps. It renders in brackets after the usage — e.g. "9% (7%)" — and
  // the colour is computed from the SAME number, so the display can never
  // disagree with the colour.
  //
  // Chosen over a calendar-day or elapsed-day ceiling knowing the trade: this
  // is the strictest of the three and reads harshly in the first hours of a
  // window, when one ordinary session can exceed a budget of a few percent.
  // That is tolerable now only because the target is VISIBLE — an unexplained
  // red at 9% was the original complaint; "9% against 7%" is legible.
  //
  // There is no window-START field, so the start is DERIVED as resets_at minus
  // seven days. If that assumption is wrong the whole segment is wrong — which
  // is why the caller falls back to flat thresholds when resets_at is absent
  // rather than guessing an elapsed time.
  const WEEK_SECS = 7 * 24 * 3600;
  // Absolute ceiling: near the cap is red even when inside the target. At the
  // end of the window the target approaches 100%, so without this "nearly out"
  // renders green on the day it matters most.
  const PACE_HARD_RED_PCT = 90;
  // Elapsed fraction of the window as a percentage. Clamped: clock skew or an
  // unexpected window length must not yield a negative or >100 budget.
  const paceTarget = (secsLeft) =>
    Math.max(0, Math.min(100, ((WEEK_SECS - secsLeft) / WEEK_SECS) * 100));
  const paceColor = (used, target) => {
    if (used >= PACE_HARD_RED_PCT) return C.red;
    if (used > target) return C.red;
    if (used > target * 0.9) return C.yellow;  // close to the day's ceiling
    return C.green;
  };

  // Seconds → reset countdown: "3hr43" (hours + padded mins) / "45m" (last
  // hour) / "now". Rounds to the minute first so we never render "3hr60".
  const fmtReset = (secs) => {
    if (secs <= 0) return 'now';
    const totalMin = Math.round(secs / 60);
    const hh = Math.floor(totalMin / 60);
    const mm = totalMin % 60;
    if (hh > 0) return `${hh}hr${String(mm).padStart(2, '0')}`;
    return `${mm}m`;
  };

  // Unix seconds → local 24-hour "Mon 17hrs" (on the hour) / "Mon 17:05hrs".
  const fmtClock = (epoch) => {
    const dt = new Date(epoch * 1000);
    const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const h = dt.getHours();
    const m = dt.getMinutes();
    const t = m === 0 ? `${h}hrs` : `${h}:${String(m).padStart(2, '0')}hrs`;
    return `${days[dt.getDay()]} ${t}`;
  };

  // Bar ~75% of the old 10-cell width. Filled by usage, coloured by the
  // metric's threshold fn.
  const BAR_W = 7;
  const barMetric = (label, p, colorFn, secondary) => {
    const u = Math.round(p);
    const c = colorFn(u);
    const filled = Math.max(0, Math.min(BAR_W, Math.floor((u / 100) * BAR_W)));
    const bar = '█'.repeat(filled) + '░'.repeat(BAR_W - filled);
    let s = `${label} [${paint(bar, c)}] ${paint(u + '%', c)}`;
    if (secondary) s += ` ${secondary}`;
    return s;
  };

  const cwd = (d.workspace && d.workspace.current_dir) || d.cwd || '';

  // ── Hub wakeability (daemon-written cache; no network call) ────
  // Placed FIRST so a line truncated on a narrow terminal never hides a
  // RELAUNCH warning. Silent (no segment) if this isn't a hub agent or the
  // daemon hasn't written a snapshot yet. The glyph/colour is THIS window's
  // own wakeability; the N/M is wakeable/online across the fleet.
  //
  // Agent name resolution mirrors mcp_hub/cli.py: DERIVED identity first
  // (name = <repo>-<hostname> from `git remote get-url origin`; sanitize
  // rule must match cli.py's _sanitize_ident exactly), then the legacy
  // .claude/hub-agent.json marker as fallback for unmigrated agents. No
  // opt-in check needed here: if the daemon never ran, there's no status
  // file and the segment stays silent.
  let hubSeg = '';
  // Resolved agent identity, hoisted out of the hub block so the usage
  // snapshot at the bottom can key its file by the SAME name the daemon
  // uses. Null when this isn't a hub agent — which is why that snapshot
  // writes nothing rather than inventing a filename.
  let agentName = null;
  try {
    const sanitizeIdent = (s) =>
      String(s).toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/^-+|-+$/g, '');
    // Per-worktree identity suffix — mirrors cli.py's _workspace_suffix.
    // Two clones of ONE repo on ONE machine derive the same name, so a
    // transported clone would read its SOURCE's status file and display the
    // source's wakeability as its own. When a suffix is configured we use it
    // EXCLUSIVELY (no fallback to the bare name): showing another agent's
    // status is worse than showing none.
    const workspaceSuffix = () => {
      try {
        const cfg = JSON.parse(
          fs.readFileSync(path.join(os.homedir(), '.mcp-hub', 'config.json'), 'utf8')
        );
        const table = cfg && cfg.workspaces;
        if (!table || typeof table !== 'object') return null;
        const norm = (p) => {
          try { return fs.realpathSync(path.resolve(p)).replace(/[/\\]+$/, ''); }
          catch { return path.resolve(p).replace(/[/\\]+$/, ''); }
        };
        const target = norm(cwd);
        for (const [p, s] of Object.entries(table)) {
          if (typeof s === 'string' && s.trim() && norm(p) === target) return s.trim();
        }
      } catch { /* no config / unreadable → no suffix */ }
      return null;
    };

    const candidates = [];
    try {
      const url = execSync('git remote get-url origin', {
        cwd, stdio: ['ignore', 'pipe', 'ignore'],
      }).toString().trim().replace(/\/$/, '').replace(/\.git$/, '');
      const tail = url.includes('://')
        ? url.split('://')[1].split('/').slice(1).join('/')
        : (url.includes(':') ? url.split(':').slice(1).join(':') : url);
      const parts = tail.split('/').filter(Boolean);
      if (parts.length >= 2) {
        const repo = parts[parts.length - 1];
        const suffix = workspaceSuffix();
        candidates.push(sanitizeIdent(
          suffix ? `${repo}-${os.hostname()}-${suffix}` : `${repo}-${os.hostname()}`
        ));
      }
    } catch { /* not a git repo / no origin → marker fallback */ }
    try {
      const marker = JSON.parse(
        fs.readFileSync(path.join(cwd, '.claude', 'hub-agent.json'), 'utf8')
      );
      if (marker && marker.name) candidates.push(String(marker.name));
    } catch { /* no marker — fine */ }

    let st = null;
    for (const name of candidates) {
      const safe = name.replace(/[^A-Za-z0-9_-]/g, '_');
      try {
        st = JSON.parse(
          fs.readFileSync(
            path.join(os.homedir(), '.mcp-hub', `status-${safe}.json`), 'utf8'
          )
        );
        agentName = name;
        break;
      } catch { /* try next candidate */ }
    }
    // No status file matched — the daemon may simply never have run here.
    // Fall back to the first derived candidate so the usage snapshot still
    // has a name; identity resolution and daemon liveness are separate facts.
    if (!agentName && candidates.length) agentName = candidates[0];
    if (st) {
      const age = Math.floor(Date.now() / 1000) - (st.ts || 0);
      const fleet = `${st.fleet_wakeable}/${st.fleet_total}`;
      if (age > 150) {
        // Snapshot older than a few heartbeat intervals → daemon stopped.
        hubSeg = paint('hub ?', C.dim);
      } else if (!st.online) {
        hubSeg = paint('hub ✖ REGISTER', C.bold, C.red);
      } else if (!st.wakeable) {
        hubSeg = paint(`hub ✖ REBIND ${fleet}`, C.bold, C.red);
      } else {
        // 🔕 sits BESIDE ⚡, never instead of it. They answer different
        // questions — ⚡ is "bound and push-deliverable", 🔕 is "wakes
        // suppressed for N more minutes" — and urgent pierces focus, so a
        // focused agent genuinely IS still wakeable. Replacing ⚡ would make a
        // focused-and-healthy agent render identically to a focused-and-dead
        // one, and leave you unable to tell a lapsed focus from a lost
        // binding when the marker goes. Same additive form list_agents()
        // already ships, so one thing looks the same wherever it appears.
        //
        // Computed from an EXPIRY, not a stored countdown, so a snapshot a
        // minute old shows the real remaining time and an expired focus stops
        // rendering even if the daemon died holding it.
        hubSeg = paint(`⚡${fmtFocus(st.focus_until)} ${fleet}`, C.green);
      }

      // ── Squad segment ────────────────────────────────────────────
      // Only when the snapshot actually carries membership. `squads`
      // ABSENT means an older daemon or a hub without list_squads — it
      // must not render as "no squad", which is a fact about the agent
      // rather than about the instrument.
      //
      // Suppressed on the ✖ states above: those already say the agent
      // is not reachable, and which squad an unreachable agent belongs
      // to is not the thing to fix first.
      if (Array.isArray(st.squads) && age <= 150 && st.online && st.wakeable) {
        const muted = new Set(st.muted || []);
        if (st.squads.length === 0) {
          // Dimmed, not red: faculty seats are legitimately squadless by
          // design, so this is information, not a fault. What makes it
          // actionable is the operator knowing which seats SHOULD have one.
          hubSeg += ' ' + paint('·no squad', C.dim);
        } else {
          const shown = st.squads[0] + (st.squads.length > 1
            ? `+${st.squads.length - 1}` : '');
          // A muted squad is the state most likely to be mistaken for a
          // broken hub ("why am I not hearing anything?"), so it gets a
          // marker rather than looking identical to a listening member.
          const allMuted = st.squads.every((s) => muted.has(s));
          hubSeg += ' ' + paint(
            '·' + shown + (allMuted ? ' 🔇' : (muted.size ? ' 🔇?' : '')),
            allMuted ? C.dim : C.green,
          );
        }
      }
    }
  } catch { /* not a hub agent / no snapshot yet → no hub segment */ }

  // ── Directory + branch ─────────────────────────────────────────
  const shortDir = cwd ? path.basename(cwd) : '';
  let branch = '';
  if (cwd) {
    try {
      branch = execSync('git --no-optional-locks branch --show-current', {
        cwd, stdio: ['ignore', 'pipe', 'ignore'],
      }).toString().trim();
    } catch { /* not a git repo / detached HEAD */ }
  }
  let dirSeg = shortDir ? paint(shortDir, C.blue) : '';
  if (branch) dirSeg += paint(` (${branch})`, C.magenta);

  // ── Model (abbreviated) + effort, one segment to save a separator ──
  // "Opus 4.8" -> "O4.8", "Claude Sonnet 4.6" -> "S4.6", "Fable 5" -> "F5".
  const abbrevModel = (name) => {
    if (!name) return '';
    const tokens = name.trim().split(/\s+/);
    let ver = '';
    let famIdx = tokens.length - 1;
    for (let i = tokens.length - 1; i >= 0; i--) {
      if (/^[\d.]+$/.test(tokens[i])) { ver = tokens[i]; famIdx = i - 1; break; }
    }
    const fam = famIdx >= 0 ? tokens[famIdx] : tokens[tokens.length - 1];
    return ver ? `${fam}${ver}` : fam;  // full family + version, no space
  };
  const model = abbrevModel((d.model && d.model.display_name) || '');
  const EFFORT_ABBR = { low: 'low', medium: 'med', high: 'high', xhigh: 'xhigh', max: 'max' };
  const effort = (d.effort && d.effort.level) || '';
  let modelSeg = model ? paint(model, C.dim) : '';
  if (effort) {
    modelSeg += (modelSeg ? ' ' : '') + paint(EFFORT_ABBR[effort] || effort, C.cyan);
  }

  // ── Metrics ────────────────────────────────────────────────────
  const cw = d.context_window || {};
  const rl = d.rate_limits || {};
  const nowSec = Math.floor(Date.now() / 1000);
  const pct = (label, p) => `${label} ${paint(Math.round(p) + '%', usageColor(Math.round(p)))}`;

  // ── Assemble — hub first so a truncated line never hides a RELAUNCH;
  // dir+branch LAST since it's the most variable-length and would otherwise
  // shove the fixed-position metrics off to the right. ──
  const segs = [];
  if (hubSeg) segs.push(hubSeg);
  if (modelSeg) segs.push(modelSeg);

  // ctx — bar, red past 50%.
  if (cw.used_percentage != null) {
    segs.push(barMetric('ctx', cw.used_percentage, ctxColor));
  }

  // 5h — bar + reset countdown in parens, e.g. "(3hr43)". Always shown; dim
  // normally, yellow in the final hour so an imminent reset stands out.
  const fh = rl.five_hour;
  if (fh && fh.used_percentage != null) {
    let secondary = '';
    if (fh.resets_at) {
      const left = fh.resets_at - nowSec;
      if (left > 0) {
        secondary = paint(`(${fmtReset(left)})`, left <= 3600 ? C.yellow : C.dim);
      }
    }
    segs.push(barMetric('5h', fh.used_percentage, usageColor, secondary));
  }

  // 7d — reset time + usage %, e.g. "Mon 17hrs 41%" (date dim, % pace-
  // coloured). Falls back to "7d 41%" with FLAT thresholds if no reset
  // timestamp is available — without resets_at there is no way to know how far
  // into the window we are, and a pace colour computed from a guessed start
  // would be a confident wrong answer rather than a missing one.
  const sd = rl.seven_day;
  if (sd && sd.used_percentage != null) {
    if (sd.resets_at) {
      const u = Math.round(sd.used_percentage);
      const target = paceTarget(sd.resets_at - nowSec);
      // Floored, not rounded: this is a ceiling you are measured against, and
      // rounding up would overstate the allowance. The colour uses the
      // unrounded value, so the displayed number is never more generous than
      // the one being enforced.
      const t = Math.floor(target);
      segs.push(
        paint(`${fmtClock(sd.resets_at)} `, C.dim)
        + paint(u + '%', paceColor(u, target))
        + paint(` (${t}%)`, C.dim)
      );
    } else {
      segs.push(pct('7d', sd.used_percentage));
    }
  }

  // dir + branch last (variable-length; keeps the metrics anchored left).
  if (dirSeg) segs.push(dirSeg);

  process.stdout.write(segs.join(` ${C.dim}·${C.reset} `) + '\n');

  // ── Usage snapshot — persist what we just rendered ──────────────
  // Claude Code hands us rate_limits on every render and we used to throw it
  // away, so the one number the usage-limits thread needed was on every seat's
  // screen and readable by nobody but a human. Written AFTER stdout: the
  // statusline's job is to render, and a disk problem must never cost a line.
  //
  // Two things a reader MUST honour, both of which make this file lie in the
  // reassuring direction if ignored:
  //
  //  1. `scope: "account"` is not decoration. Rate limits are per-CREDENTIAL,
  //     not per-agent: seats sharing one credential all write the SAME
  //     numbers, so the per-agent filename is a redundant witness of a shared
  //     fact, NOT a partition of it. Summing or averaging across agents
  //     divides a real 100% by the fleet size and reports plenty while work
  //     is stopping.
  //
  //     Do NOT group by the raw (5h, 7d) pair to count credentials, either.
  //     Tried on dev-vm-1 and it reported 10 "credentials" across 12 seats,
  //     which was an artefact: each seat samples at a different instant, so
  //     one account's 7d reads 63-69% across the fleet. Group by the 7d
  //     `resets_at` instead — a window boundary is a property of the ACCOUNT
  //     and identical for every seat sharing it, where the percentage is a
  //     property of the moment you looked.
  //
  //  3. A `resets_at` in the PAST means the value is a FOSSIL, not a reading:
  //     a long-lived session can keep rendering a rate_limits block it
  //     fetched days ago, so `observed_at` is fresh while the number is
  //     ancient. Caught live — one seat showed 7d=96% with a boundary 12.6
  //     days gone, and it was briefly reported as a fleet emergency. Check
  //     BOTH clocks: observed_at says when we looked, resets_at says whether
  //     what we saw was still real.
  //
  //  2. `observed_at` is mandatory because a statusline only renders when a
  //     session is ON SCREEN. This witnesses what a seat last SAW, never what
  //     is true now — a closed or idle seat's file is arbitrarily stale. Treat
  //     missing or stale as NO MEASUREMENT, never as 0%. Same rule as the
  //     fleet board's staleness cutoff: an absent instrument must not read as
  //     a perfect one.
  //
  // Written only when the VALUES change, since renders are frequent and an
  // unchanged rewrite is pure IO; and atomically (tmp + rename), so a reader
  // can never catch a half-written file.
  try {
    if (agentName && (rl.five_hour || rl.seven_day)) {
      const safe = agentName.replace(/[^A-Za-z0-9_-]/g, '_');
      const dir = path.join(os.homedir(), '.mcp-hub', 'usage');
      const dest = path.join(dir, `${safe}.json`);
      const win = (w) => (w && w.used_percentage != null)
        ? { used_percentage: w.used_percentage, resets_at: w.resets_at ?? null }
        : null;
      // The PLAN a percentage was measured under. Without it, a stored 96%
      // from early August and a live 70% from today are percentages of two
      // different denominators and nothing says so — this account went Pro →
      // Max at an unrecorded date, and an hour of token↔percentage
      // calibration was built across that seam before it surfaced.
      //
      // ⚠️ This reads a CREDENTIALS file, so it takes exactly two fields BY
      // NAME and never iterates the object. Both are plan identifiers, not
      // secrets; the tokens beside them must never reach a file that lands in
      // ~/.mcp-hub, which is not treated as secret storage. Nulls when
      // unreadable — an unknown plan is a fact, and guessing one would
      // reintroduce the exact ambiguity this field exists to remove.
      let plan = null;
      try {
        const cred = JSON.parse(fs.readFileSync(
          path.join(os.homedir(), '.claude', '.credentials.json'), 'utf8'
        )).claudeAiOauth || {};
        plan = {
          subscription_type: cred.subscriptionType ?? null,
          rate_limit_tier: cred.rateLimitTier ?? null,
        };
        if (plan.subscription_type == null && plan.rate_limit_tier == null) plan = null;
      } catch { /* no credentials file (container seat, other login) → null */ }

      const payload = {
        agent: agentName,
        scope: 'account',
        plan,
        five_hour: win(rl.five_hour),
        seven_day: win(rl.seven_day),
      };
      // Compare the VALUES only — observed_at changes every render and would
      // defeat the write-on-change guard entirely.
      let prev = null;
      try {
        const old = JSON.parse(fs.readFileSync(dest, 'utf8'));
        delete old.observed_at;
        prev = JSON.stringify(old);
      } catch { /* no file yet / unreadable → write it */ }
      if (prev !== JSON.stringify(payload)) {
        fs.mkdirSync(dir, { recursive: true });
        const tmp = `${dest}.${process.pid}.tmp`;
        fs.writeFileSync(
          tmp,
          JSON.stringify({ observed_at: nowSec, ...payload }) + '\n'
        );
        fs.renameSync(tmp, dest);
      }
    }
  } catch { /* usage snapshot is best-effort; never break the statusline */ }
});
