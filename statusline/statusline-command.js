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
        break;
      } catch { /* try next candidate */ }
    }
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
        hubSeg = paint(`⚡ ${fleet}`, C.green);
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
});
