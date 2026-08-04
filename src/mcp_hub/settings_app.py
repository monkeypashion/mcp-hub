"""SQUAD BOARD — the fleet dashboard and the settings panel, one screen.

Two apps merged on the operator's call (2026-07-28: "I don't use the squad
board very much... consolidate the two items into one"): the live board — who
needs you, who is working, who is burning the budget — and the per-agent
settings sheet, which was already here. The board half is a RENDERER of
`squad board --json` plus the documented caches (board_data.collect); it never
re-scrapes panes, which is the board's own single-source rule.

The left panel is a TREE — machines, then workspaces, then the seats inside
them (2026-08-04: "can't we have a grouping system for remote/local — save
having the separate `w` workspaces view?"). It replaces two surfaces that were
always projections of one structure: a flat roster of this machine's agents,
and a separate keystroke showing every workspace on every box. The join lives
in `fleet_tree.build_tree`, not here, so it is testable without a terminal.

The tree's honesty rule, inherited from the `w` view it absorbed: a REMOTE row
is visibly thinner than a local one. There is no pane to scrape on another
machine, so remote seats carry presence and nothing else, and a fleet snapshot
that has stopped being written reads as "not reporting" rather than as a quiet
fleet. Drift still outranks every status — attention beats information.

Why a framework rather than the hand-rolled curses version this replaces: the
keyboard, focus, mouse and widget behaviour are exactly what I got wrong by
hand — one line binding ESC to quit made every arrow key exit the program,
because VSCode sends arrows as `ESC [ B` and the leading byte arrives as 27.
That class of defect is not mine to solve; a toolkit owns it.

The settings model comes from `_settings_model` unchanged: every row carries
its value, its SOURCE, and — when it can be changed — the exact argv to change
it. That layer is proven and this file only renders it, so the panel cannot
offer an edit the underlying verb cannot perform. The same rule holds for the
board's one action: the answer buttons run `squad answer <agent> <intent>`,
which is fail-closed (it presses nothing unless a matching option is visible
on that agent's screen), so a button here can never do what the verb refuses.

Theme: detected from the TERMINAL, not configured — the panel runs on several
devices whose VSCode themes differ, and a white panel on a dark terminal was
the operator's exact complaint. board_data.terminal_prefers_dark asks via
OSC 11 before the app takes the tty; `t` flips it live if the answer was wrong.
"""
from __future__ import annotations

import pathlib
import subprocess
import time
from typing import Any

from rich.cells import cell_len
from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    Select,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from mcp_hub.fleet_tree import build_tree, structure_key

# Claude's own palette, read out of the Claude Code binary's CSS custom
# properties rather than recalled — `--clay: #d97757`, `--clay-emphasized`,
# `--plum`, `--mineral`, `--peach`, and a 40-step WARM grey ramp (#fcfcfb …
# #0b0b0b) that is what makes it read as Claude rather than as a generic dark
# theme: the greys are not neutral, they lean yellow.
#
#   grep -aoE "#[0-9a-f]{6}" ~/.local/share/claude/versions/<v>
#
# All variants are registered, so Ctrl+P still switches to any of Textual's
# 22 built-ins — this only changes the DEFAULT.
CLAY = "#d97757"
# WHITE, and a selection that is not a colour wash.
#
# Six goes at this. The failure was reaching for someone else's palette each
# time — the built-in light themes are all off-white (#E0E0E0, #EFF1F5,
# #FAFAFA, #fdf6e3) and every one tints its selection with the primary hue, so
# the highlighted row renders as dark text over a blue-ish wash: "kind of blue
# and black at the same time. It's fucking annoying." Correct description of
# what a 25% primary tint under near-black text actually looks like.
#
# So: pure white, near-black text, and NEUTRAL greys for selection and rules.
# The accent is used for emphasis only, never as a background behind text.
# The hues are the board's, which is a VOCABULARY rather than decoration —
# cyan for names and informational sections, green for healthy, yellow for
# down, red for needs-you, magenta for unshipped, dim for everything
# secondary. Darkened from the ANSI originals because the board renders on
# whatever the terminal gives it and this panel is on white, where ansi cyan
# and green are too pale to read.
SQUAD_LIGHT = Theme(
    name="squad-light",
    primary="#0a6c78",        # board cyan — headings and agent names
    secondary="#8839ef",      # board magenta
    accent="#c6613f",         # clay, emphasis only
    warning="#b45309",        # board yellow — down / attention
    error="#b3261e",          # board red — needs you
    success="#2e7d32",        # board green — healthy
    foreground="#1a1a1a",
    background="#ffffff",
    surface="#ffffff",
    panel="#f4f4f4",
    dark=False,
)
CLAUDE_DARK = Theme(
    name="claude-dark",
    primary=CLAY,             # clay
    secondary="#827dbd",      # plum
    accent="#ebc9b7",         # peach
    warning="#eb6834",        # orange-350
    error="#e34948",          # red-400
    success="#629987",        # mineral
    foreground="#e4e3dd",     # gray-90
    background="#1a1a19",     # gray-830
    surface="#20201f",        # gray-800
    panel="#2c2c2a",          # gray-750
    dark=True,
)
CLAUDE_LIGHT = Theme(
    name="claude-light",
    primary="#c6613f",        # clay-emphasized, for contrast on cream
    secondary="#827dbd",
    accent=CLAY,
    warning="#ae461c",        # orange-500
    error="#b93535",          # red-500
    success="#629987",
    foreground="#20201f",     # gray-800
    background="#f9f9f7",     # gray-20
    surface="#ffffff",
    panel="#f0efec",          # gray-50
    dark=False,
)

CSS = """
Screen { layout: vertical; }
#body { height: 1fr; }

/* Explicit heights everywhere. Textual containers default to 1fr, and a 1fr
   child inside an auto-height parent collapses to nothing — which rendered
   exactly one row of the panel and blank space below it. */
/* Wider than the old flat roster: the tree carries three levels of indent
   plus a live tail. 52 is what a `mcp-hub-fireblade-wsl-xport` leaf needs at
   full depth with its context and state still attached — measured, because a
   Tree CLIPS rather than wraps and an overflow here would be silent. */
#fleet {
    width: 52;
    border-right: solid #d8d8d8;
    background: $surface;
    height: 1fr;
    padding: 0 1;
}
#fleet > .tree--guides { color: $panel; }
#fleet > .tree--guides-hover { color: $panel; }
#fleet > .tree--guides-selected { color: $accent; }
#fleet > .tree--cursor { background: #e8e8e8; color: $foreground; text-style: none; }

#detail { padding: 0 2; height: 1fr; }
.section {
    color: $primary;
    text-style: bold;
    height: 1;
    margin: 1 0 0 0;
}
.section-live { color: $error; }
.value-on { color: $success; }
.value-off { color: $text-muted; }
.value-warn { color: $warning; }
.row { height: 2; }
.row-line { height: 1; }
.label { width: 24; height: 1; }
.value-ro { color: $text; height: 1; width: 1fr; }
.source { color: $text-muted; height: 1; padding: 0 0 0 24; }
Select { width: 30; height: 1; }
.note { color: $warning; padding: 1 0; height: auto; }
/* the seats listed inside a workspace's detail pane. The manager's own rows
   moved into the tree, where drift and presence are carried by the label's
   colour — the .ws-machine/.ws-row-here/.ws-row-drift classes went with them
   rather than lingering as CSS nothing selects. */
.ws-row { color: $text; height: 1; width: 1fr; }
.ws-row-open { color: $success; height: 1; width: 1fr; }
#status { dock: bottom; height: 1; padding: 0 2; background: $panel; }

/* live section — height:auto or it is the collapsing-1fr child the comment
   at the top of this block describes, and the whole section renders as
   nothing (found by the first real tmux run, not by the unit tests) */
#live { height: auto; }
.live-question { color: $error; height: auto; padding: 0 0 0 2; }
.live-next { color: $text; height: auto; padding: 0 0 0 2; }
.live-unshipped { color: $secondary; height: 1; width: 1fr; }
.answers { height: 3; padding: 0 0 0 2; }
.answers Button { margin: 0 2 0 0; min-width: 10; }
"""


# Same idiom the board uses for 🟢 / dim / yellow: healthy reads green, an
# absent or default value reads dim, and anything the operator may need to act
# on reads amber. Applied to the VALUE only — never behind text as a
# background, which is what made the earlier selection unreadable.
_ON = {"on", "hearing", "squad"}
_OFF = {"off", "muted", "default", "— none —", "—"}


def _value_class(value: str) -> str:
    v = str(value).strip().lower()
    if v in _ON:
        return "value-on"
    if v == "unknown":
        return "value-warn"
    if v in _OFF:
        return "value-off"
    return "value-ro"


# ONE glyph vocabulary, and every row wears one. `idle` used to be a bare `·`
# and a seat with no board record got the same — so six of eight local seats
# rendered as a faint dot while every remote seat showed ⚡, and the panel read
# as "remote agents are instrumented, local ones aren't" (operator, 2026-08-04).
# 💤 and ⚡ are the hub's own vocabulary from list_agents; using them here means
# one thing looks the same everywhere it appears.
_STATE_GLYPH = {"waiting": "🔴", "working": "▶", "idle": "💤", "down": "✖"}
_GLYPH_STOPPED = "○"        # enrolled, no pane — a fact, not a fault
_GLYPH_NOT_REPORTING = "⚠"  # the instrument is silent; nothing is claimed
_GLYPH_WAKEABLE = "⚡"


def _cell2(glyph: str) -> str:
    """Exactly two cells, whatever the glyph.

    🔴 and 💤 are double-width, ▶ ✖ ○ are single. Mixed, the name column shifts
    by one between states and the tree reads as jitter — so the narrow ones are
    padded rather than the set being restricted to emoji.
    """
    return glyph + " " * max(0, 2 - cell_len(glyph)) if glyph else "  "


def _hms(s: int) -> str:
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _human_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


class SquadCommands(Provider):
    """Every verb the board can run, reachable by typing.

    The board's actions were all bound to single keys or buried in a dropdown,
    which does not scale: `focus` alone is four commands, and the useful ones
    are per-agent. Ctrl+P asks for a name instead of a keystroke.

    The list is built by the app (`palette_commands`), not here, so it can be
    tested without opening a palette — and so a command can never be offered
    for a selection that cannot perform it: the app knows whether the cursor
    is on a local seat, a remote one, or a workspace.
    """

    @property
    def _app(self) -> "SettingsApp":
        return self.app  # type: ignore[return-value]

    async def discover(self) -> Hits:
        # Discovery shows what is possible RIGHT NOW, before any typing —
        # so it is the same list, not a hand-picked subset that could drift.
        for title, help_text, run in self._app.palette_commands()[:12]:
            yield DiscoveryHit(title, run, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for title, help_text, run in self._app.palette_commands():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), run, help=help_text)


class SettingsApp(App):
    """One screen: the live fleet on the left, the selected agent —
    board detail on top, settings below — on the right."""

    COMMANDS = App.COMMANDS | {SquadCommands}
    CSS = CSS
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reload", "Reload"),
        ("t", "toggle_theme", "Light/dark"),
        ("n", "next_hand", "Next needs-you"),
        ("e", "expand_all", "Expand/collapse"),
    ]

    def __init__(self, agents: list[dict[str, str]], scoped_to: str | None,
                 model_for, squad_bin: str, hub_bin: str,
                 board_for=None, dark: bool | None = None,
                 poll_seconds: float = 3.0, workspaces_for=None,
                 presence_ping=None, presence_seconds: float = 60.0,
                 fleet_for=None, listings_for=None, this_machine: str = "",
                 workspaces_seconds: float = 30.0, now=None):
        super().__init__()
        self.agents = agents
        # Passed in rather than derived here: `mcp-hub identity` owns machine
        # naming, and a second derivation is a second chance to disagree —
        # squad deriving from basename while the cli derived from the git
        # remote is exactly how a clone's statusline came to read `hub ?`.
        self._this_machine = this_machine
        self._workspaces_for = workspaces_for  # injected; None → local only
        self._fleet_for = fleet_for            # injected; None → no remote rows
        self._listings_for = listings_for      # injected; reads a local ws file
        self._presence_ping = presence_ping    # injected; None disables it
        # Well inside the hub's 180s open-now window, so a single dropped
        # ping never blinks the workspace out of the manager's view.
        self._presence_seconds = presence_seconds
        # The registry is a network round trip and workspaces do not move
        # every three seconds; presence inside it does, but the hub's own
        # open-now window is 180s, so 30 is ten times finer than the fact.
        self._workspaces_seconds = workspaces_seconds
        self._now = now or time.time
        self.scoped_to = scoped_to
        self._model_for = model_for       # injected so tests need no real repo
        self._board_for = board_for       # injected so tests need no real fleet
        self._prefers_dark = dark
        self._poll_seconds = poll_seconds
        self.squad_bin = squad_bin
        self.hub_bin = hub_bin
        self.agent_ix = 0
        self.model: dict[str, Any] | None = None
        self.board: dict[str, Any] = {"agents": {}, "counts": {}, "error": None}
        # Until the registry answers, the tree is this machine's roster and
        # says so. An empty dict here would render as "no workspaces anywhere",
        # which is an assertion nothing has made yet.
        self.workspaces: dict[str, Any] = {"rows": [], "machines": [], "note": ""}
        self.fleet: dict[str, Any] = {"ts": 0, "agents": []}
        self.tree_model: dict[str, Any] = {"machines": []}
        self.selected: dict[str, Any] | None = None
        self._pending_key: str | None = None
        self._structure: tuple | None = None
        self._presence_error: str | None = None
        self._live_key: tuple | None = None
        # Bumped every redraw and baked into each widget id. remove_children()
        # is ASYNCHRONOUS, so a widget from the previous render can still be in
        # the tree when the next one mounts — a fixed id then raises
        # DuplicateIds and takes the whole app down. Clicking from one
        # no-settings agent to another did exactly that ("it crashed the whole
        # thing", 2026-07-28). Awaiting the removal fixes the timing; this makes
        # a collision impossible even if the timing changes again.
        self._gen = 0

    # ---- layout ----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            tree: Tree[dict] = Tree("fleet", id="fleet")
            tree.show_root = False
            tree.guide_depth = 2
            yield tree
            yield VerticalScroll(id="detail")
        yield Static("", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme(CLAUDE_DARK)
        self.register_theme(CLAUDE_LIGHT)
        self.register_theme(SQUAD_LIGHT)
        # The terminal's own answer wins; light is the fallback because the
        # panel predates detection and light is what it always was.
        self.theme = "claude-dark" if self._prefers_dark else "squad-light"
        self.title = "SQUAD BOARD"
        where = self.scoped_to.rsplit("/", 1)[-1] if self.scoped_to else "this machine"
        self.sub_title = f"{len(self.agents)} agent(s) · {where}"
        self._rebuild_tree()
        self.query_one("#fleet", Tree).focus()
        # Open on an agent that HAS settings: `squad add-folder` enrols plain
        # directories deliberately and those have no derived identity, so
        # opening on roster row 0 routinely showed an empty panel.
        #
        # AFTER a refresh, not now: a Tree maps nodes to lines only once it has
        # laid out, so move_cursor() during on_mount is silently a no-op and
        # the panel opened on the machine heading instead.
        self.call_after_refresh(self._initial_select)
        if self._board_for is not None:
            self._poll_board()                      # first paint: don't wait 3s
            self.set_interval(self._poll_seconds, self._poll_board)
        if self._workspaces_for is not None:
            self._poll_workspaces()
            self.set_interval(self._workspaces_seconds, self._poll_workspaces)
        # Presence needs BOTH a reporter and a workspace to report. Unscoped
        # (`mcp-hub board` with no --workspace) there is genuinely nothing
        # open to name, and inventing one would put a phantom row in the
        # manager on every machine that ever opened a bare board.
        if self._presence_ping is not None and self.scoped_to:
            self._push_presence()                   # claim the row immediately
            self.set_interval(self._presence_seconds, self._push_presence)

    def _first_with_settings(self) -> int:
        for i, a in enumerate(self.agents):
            try:
                if self._model_for(a["worktree"]):
                    return i
            except Exception:  # noqa: BLE001
                continue
        return 0

    async def _initial_select(self) -> None:
        self._select_first_with_settings()
        await self.refresh_detail()

    def _select_first_with_settings(self) -> None:
        ix = self._first_with_settings()
        want = self.agents[ix]["agent"] if ix < len(self.agents) else None
        for node in self._agent_nodes():
            if node.data and node.data.get("agent") == want:
                self._move_to(node)
                return
        # No local seat at all (a board opened on a box with an empty roster):
        # land on whatever the tree's first row is rather than nothing.
        tree = self.query_one("#fleet", Tree)
        if tree.root.children:
            self.selected = tree.root.children[0].data

    # ---- the tree ----

    def _palette(self) -> dict[str, str]:
        """Tree labels are rich markup, which cannot read `$success` — so the
        active theme's colours are resolved to hex here and the whole tree is
        relabelled when the theme flips. A hard-coded green would be the exact
        defect the three custom themes exist to avoid."""
        t = self.current_theme
        return {
            "primary": t.primary, "success": t.success, "warning": t.warning,
            "error": t.error, "accent": t.accent or t.primary,
            "secondary": t.secondary or t.primary,
        }

    def _machine_label(self, m: dict[str, Any], p: dict[str, str]) -> str:
        if m["unknown"]:
            head = "(machine unknown)"
        else:
            head = f"{m['machine']}  · {'this machine' if m['local'] else 'remote'}"
        # Only the EXCEPTIONAL facts. Seat and open counts used to ride here
        # too and pushed the label past the panel — where a Tree clips rather
        # than wraps, so the overflow would have been silent. They are one
        # keypress away in the detail pane, and the tree shows them by simply
        # being expanded.
        bits = []
        if m["drift_count"]:
            bits.append(f"⚠ {m['drift_count']} drift")
        # A snapshot that stopped being written must not read as a quiet box.
        if m["stale"]:
            bits.append("not reporting")
        colour = p["primary"] if m["local"] else p["secondary"]
        label = f"[b {colour}]{escape(head)}[/]"
        return f"{label}  [dim]{escape(' · '.join(bits))}[/]" if bits else label

    def _workspace_label(self, w: dict[str, Any], p: dict[str, str]) -> str:
        # Three states, not two: nobody has it open · someone does · YOU are
        # looking at it right now. The board knows its own --workspace, so the
        # third is free and is the one the operator is standing in.
        # ○ rather than a blank: every row in the tree wears a mark, or the
        # ones that do read as the only real entries.
        glyph = "◉" if w["here"] else ("●" if w["open_now"] else "○")
        # Drift says what it IS, in words — the same guarantee the `w` view
        # made, moved to the surface that replaced it. The PATH is deliberately
        # not here: it is what made the old rows wrap, and the detail pane has
        # room for it.
        if w["error"]:
            tail, colour = f"⚠ {w['error']}", p["warning"]
        elif not w["on_disk"]:
            tail, colour = "ghost — registered, no file", p["warning"]
        elif w["registered"] is False:
            tail, colour = "not registered", p["warning"]
        elif w["registered"] is None:
            tail, colour = "registration unknown", p["warning"]
        elif w["here"]:
            tail, colour = "", p["accent"]
        elif w["open_now"]:
            tail, colour = "", p["success"]
        else:
            tail, colour = "", ""
        if w["squad"]:
            tail = f"{tail}  [{w['squad']}]" if tail else f"[{w['squad']}]"
        name = f"{glyph} {w['name']}"
        style = f"b {colour}" if (w["here"] and colour) else colour
        head = f"[{style}]{escape(name)}[/]" if colour else escape(name)
        return f"{head}  [dim]{escape(tail)}[/]" if tail else head

    def _agent_label(self, a: dict[str, Any], p: dict[str, str]) -> str:
        if a["local"]:
            rec = a.get("rec") or {}
            st = rec.get("state", "")
            colour = {"waiting": p["error"], "working": p["success"],
                      "down": p["warning"]}.get(st, "")
            if st == "waiting":
                tail = f"waiting {_hms(rec.get('waiting_seconds', 0))}"
            else:
                # A stopped seat says so with its glyph; `faculty`/`squad` moved
                # to the detail pane rather than costing two cells on every row.
                tail = st
            # The raised hand rides the row even when the state glyph already
            # says `waiting` — it is what `n` jumps between, and a marker you
            # cannot see is not a marker.
            hand = "🙋 " if a["hand"] else ""
            glyph = _STATE_GLYPH.get(st, _GLYPH_STOPPED)
            wake = _GLYPH_WAKEABLE if rec.get("wakeable") else ""
            # Context percentage but NOT the model name: an agent name is
            # already ~22 cells and `Sonnet` pushed the longest real seat past
            # the panel. The model is one row down in the live section.
            bits = [b for b in (rec.get("ctx", ""), tail) if b]
        else:
            # Presence and nothing else — there is no pane to scrape on
            # another box, so the row is thinner ON PURPOSE.
            colour, hand = "", ""
            wake = _GLYPH_WAKEABLE if a.get("wakeable") else ""
            if a.get("stale"):
                glyph, bits = _GLYPH_NOT_REPORTING, ["not reporting"]
            else:
                # `hub only` stays on the row: a remote `idle` is a weaker
                # claim than a local one (a snapshot, not a scraped pane), and
                # the two must not read identically.
                glyph = _STATE_GLYPH.get(a.get("state", ""), _GLYPH_STOPPED)
                bits = ["hub only"]
        # Two fixed columns, then a separator — without it a present ⚡ (already
        # two cells) ran straight into the name while an absent one left a gap,
        # so the name started in a different place depending on the wake state.
        name = f"{_cell2(glyph)}{_cell2(wake)} {hand}{a['agent']}"
        head = f"[{colour}]{escape(name)}[/]" if colour else escape(name)
        tail = " ".join(b for b in bits if b)
        return f"{head}  [dim]{escape(tail)}[/]" if tail else head

    def _label_for(self, data: dict[str, Any], p: dict[str, str]) -> str:
        kind = data.get("kind")
        if kind == "machine":
            return self._machine_label(data, p)
        if kind == "workspace":
            return self._workspace_label(data, p)
        return self._agent_label(data, p)

    @staticmethod
    def _identity(data: dict[str, Any] | None) -> tuple | None:
        """WHAT is selected, independent of where it currently hangs.

        A node's key encodes its parent, so a seat moves key the moment it is
        attributed to a workspace — which is exactly what happens ~a second
        after launch, when the registry poll lands and the tree restructures.
        Matching on the key alone dropped the selection there and the detail
        pane went blank on every open (found by rendering the real board, not
        by any unit test).
        """
        if not data:
            return None
        kind = data.get("kind")
        if kind == "agent":
            return ("agent", data.get("agent"))
        if kind == "workspace":
            return ("workspace", data.get("machine"), data.get("name"))
        return ("machine", data.get("machine"))

    def _all_nodes(self) -> list[TreeNode]:
        try:
            tree = self.query_one("#fleet", Tree)
        except Exception:  # noqa: BLE001 — queried before mount or mid-teardown
            return []
        out: list[TreeNode] = []

        def walk(node: TreeNode) -> None:
            for child in node.children:
                out.append(child)
                walk(child)

        walk(tree.root)
        return out

    def _agent_nodes(self) -> list[TreeNode]:
        return [n for n in self._all_nodes()
                if n.data and n.data.get("kind") == "agent"]

    def _build_model(self) -> dict[str, Any]:
        return build_tree(
            roster=self.agents,
            board=self.board,
            workspaces=self.workspaces,
            fleet=self.fleet,
            this_machine=self.workspaces.get("this_machine")
            or self._this_machine,
            scoped_to=self.scoped_to,
            listings_for=self._listings_for,
            now=self._now(),
        )

    def _default_expansion(self, model: dict[str, Any]) -> set[str]:
        """This machine open, its workspaces open, other boxes folded.

        The operator acts on the box they are sitting at; a tree that opens
        with forty remote leaves showing buries it.
        """
        keys: set[str] = set()
        for m in model.get("machines", []):
            if not m["local"]:
                continue
            keys.add(m["key"])
            keys.update(w["key"] for w in m["workspaces"])
        return keys

    def _rebuild_tree(self) -> None:
        """Rebuild structure. Only called when `structure_key` actually moved —
        the poll relabels in place, because a rebuild collapses the operator's
        expansions and drops their cursor."""
        tree = self.query_one("#fleet", Tree)
        model = self._build_model()
        first_paint = self._structure is None
        keep = {n.data["key"] for n in self._all_nodes()
                if n.data and n.is_expanded}
        cursor = (self.selected or {}).get("key")
        cursor_ident = self._identity(self.selected)
        self.tree_model = model
        self._structure = structure_key(model)
        tree.clear()
        tree.root.expand()
        by_key: dict[str, TreeNode] = {}
        p = self._palette()
        for m in model.get("machines", []):
            mn = tree.root.add(self._machine_label(m, p), data=m, expand=False)
            by_key[m["key"]] = mn
            for w in m["workspaces"]:
                wn = mn.add(self._workspace_label(w, p), data=w, expand=False)
                by_key[w["key"]] = wn
                for a in w["agents"]:
                    by_key[a["key"]] = wn.add_leaf(
                        self._agent_label(a, p), data=a)
            for a in m["loose"]:
                by_key[a["key"]] = mn.add_leaf(self._agent_label(a, p), data=a)
        want = self._default_expansion(model) if first_paint else keep
        for key in want:
            node = by_key.get(key)
            if node is not None and node.allow_expand:
                node.expand()
        node = by_key.get(cursor or "")
        if node is None and cursor_ident is not None:
            # Same thing, new position — follow it rather than lose it.
            node = next((n for n in self._all_nodes()
                         if self._identity(n.data) == cursor_ident), None)
        if node is not None:
            self._move_to(node)
        elif cursor is not None:
            # The selection is genuinely GONE — an agent retired, a workspace
            # deleted. Say so rather than silently landing on a neighbour and
            # letting the right-hand pane look like it is still about the old
            # one.
            self._set_status("previous selection is no longer in the tree")
            self.selected = None

    def _relabel_tree(self) -> None:
        """Same nodes, new text. Keyed by the node's own data dict, which the
        rebuilt model replaces wholesale — so the label and the data can never
        disagree about which agent a row is."""
        model = self._build_model()
        self.tree_model = model
        p = self._palette()
        fresh: dict[str, dict[str, Any]] = {}
        for m in model.get("machines", []):
            fresh[m["key"]] = m
            for w in m["workspaces"]:
                fresh[w["key"]] = w
                for a in w["agents"]:
                    fresh[a["key"]] = a
            for a in m["loose"]:
                fresh[a["key"]] = a
        for node in self._all_nodes():
            data = node.data or {}
            new = fresh.get(data.get("key", ""))
            if new is None:
                continue
            node.data = new
            node.set_label(self._label_for(new, p))
            if self.selected and self.selected.get("key") == new["key"]:
                self.selected = new

    def _sync_tree(self) -> None:
        """One entry point for every data change: rebuild if the SHAPE moved,
        otherwise relabel."""
        if self._structure is None:
            self._rebuild_tree()
            return
        if structure_key(self._build_model()) != self._structure:
            self._rebuild_tree()
        else:
            self._relabel_tree()

    def _move_to(self, node: TreeNode) -> None:
        """Put the cursor on a node, opening whatever hides it.

        The cursor move is deferred a frame ON PURPOSE. A Tree maps nodes to
        lines only when it lays out, so a node inside a just-expanded branch
        still reports line -1 and `move_cursor` is silently a no-op — which is
        how a jump to a remote seat moved nothing at all while reporting
        success. The selection is set NOW so callers see it immediately; the
        cursor catches up on the next refresh.
        """
        parent = node.parent
        while parent is not None:
            if parent.allow_expand:
                parent.expand()
            parent = parent.parent
        self._pending_key = (node.data or {}).get("key")
        self.selected = node.data
        if node.data and node.data.get("roster_ix") is not None:
            self.agent_ix = node.data["roster_ix"]
        self.call_after_refresh(self._settle_cursor, node)

    def _settle_cursor(self, node: TreeNode) -> None:
        if node.line >= 0:
            self.query_one("#fleet", Tree).move_cursor(node)
        self._pending_key = None

    # ---- the workspace registry ----

    def _poll_workspaces(self) -> None:
        self.run_worker(self._collect_workspaces, thread=True,
                        group="workspace-poll", exclusive=True)

    def _collect_workspaces(self) -> None:
        try:
            data = self._workspaces_for()
        except Exception as exc:  # noqa: BLE001 — the tree must never crash
            data = {"rows": [], "machines": [], "note": f"workspace data: {exc}"}
        self.call_from_thread(self._apply_workspaces, data)

    def _apply_workspaces(self, data: dict[str, Any]) -> None:
        self.workspaces = data
        if self._fleet_for is not None:
            try:
                self.fleet = self._fleet_for() or {"ts": 0, "agents": []}
            except Exception:  # noqa: BLE001 — a missing cache is "not reporting"
                self.fleet = {"ts": 0, "agents": []}
        self._sync_tree()
        self.call_later(self.refresh_detail)

    # ---- the live board ----

    def _poll_board(self) -> None:
        """Collect on a WORKER THREAD — the scan captures a pane per agent and
        the UI must never wait on it (group of its own: a poll must not cancel
        an in-flight settings write, or vice versa)."""
        self.run_worker(self._collect_board, thread=True,
                        group="board-poll", exclusive=True)

    def _collect_board(self) -> None:
        try:
            snap = self._board_for()
        except Exception as exc:  # noqa: BLE001 — a dashboard never takes the app down
            snap = {"agents": {}, "counts": {}, "error": str(exc)[:120]}
        self.call_from_thread(self._apply_board, snap)

    # ---- presence: the one fact only this process knows ----

    def _push_presence(self) -> None:
        """Report "this workspace is open" — on its OWN worker group.

        Sharing `board-poll` would make the two cancel each other: both are
        exclusive, and a 60s network call landing mid-scan would drop the
        board's refresh on the floor.
        """
        self.run_worker(self._send_presence, thread=True,
                        group="presence-ping", exclusive=True)

    def _send_presence(self) -> None:
        try:
            self._presence_ping(self.scoped_to)
        except Exception as exc:  # noqa: BLE001
            # Deliberately quiet in the UI: the hub being unconfigured is
            # already stated, in a full sentence, at the top of the `w` view.
            # Saying it a second time on a 60s timer would be noise, and a
            # dashboard never takes the app down.
            self._presence_error = str(exc)[:160]
        else:
            self._presence_error = None

    def _apply_board(self, snap: dict[str, Any]) -> None:
        self.board = snap
        counts = snap.get("counts") or {}
        if counts:
            bits = []
            if counts.get("waiting") or counts.get("hands"):
                need = max(counts.get("waiting", 0), counts.get("hands", 0))
                bits.append(f"🔴 {need} need you")
            bits.append(f"▶ {counts.get('working', 0)}")
            bits.append(f"idle {counts.get('idle', 0)}")
            if counts.get("down"):
                bits.append(f"✖ {counts.get('down', 0)}")
            self.sub_title = " · ".join(bits)
        elif snap.get("error"):
            self.sub_title = "live data unavailable"
        # A poll changes what rows SAY, almost never which rows exist — so the
        # default path relabels and the tree keeps the operator's expansions
        # and cursor exactly where they left them.
        self._sync_tree()
        # the detail's live section follows the selected agent
        self.call_later(self._refresh_live_section)

    def _live_widgets(self, rec: dict[str, Any] | None,
                      seat: dict[str, Any] | None = None) -> list[Any]:
        """The board's view of ONE agent, as widgets. Everything here is a
        rendering of collect()'s record — no scraping, no second source."""
        if rec is None:
            err = (self.board or {}).get("error")
            if err:
                return [Static(f"live data: {err}", classes="note")]
            if seat and seat.get("klass"):
                # No pane, so nothing to report — but the row's ○ should not be
                # the only place this is said, and the seat's CLASS moved here
                # when it left the label.
                return [
                    Static("▌LIVE   not running", classes="section section-live"),
                    self._fact("Class", seat["klass"], "value-off",
                               "faculty seats are on-demand — `up` never starts them"
                               if seat["klass"] == "faculty"
                               else "started by `squad up`"),
                ]
            return []
        g = self._gen
        out: list[Any] = [Static("▌LIVE   the board's answer: what is it doing, what does it need",
                                 classes="section section-live")]

        st = rec.get("state", "?")
        act = rec.get("action") or ""
        state_line = st if st != "waiting" else f"waiting {_hms(rec.get('waiting_seconds', 0))}"
        out.append(Vertical(
            Horizontal(Label("State", classes="label"),
                       Static(state_line, classes=_value_class(
                           {"working": "on", "down": "unknown"}.get(st, st))),
                       classes="row-line"),
            Static(f"● {act}" if act else "", classes="source"),
            classes="row",
        ))

        # Model and context left the tree label when it outgrew the panel, so
        # they must land somewhere — an instrument that is merely moved is
        # fine, one that is dropped is a regression nobody notices.
        model, ctx = rec.get("model", ""), rec.get("ctx", "")
        if model or ctx:
            out.append(Vertical(
                Horizontal(Label("Model", classes="label"),
                           Static(" · ".join(b for b in (model, ctx) if b),
                                  classes="value-ro"),
                           classes="row-line"),
                Static("model, and context used", classes="source"),
                classes="row",
            ))
        # The tree's wake column is one glyph; this is the board's own phrase,
        # verbatim — `✖ REGISTER` says something ⚡-or-nothing cannot.
        if rec.get("hub"):
            out.append(self._fact(
                "Hub", str(rec["hub"]),
                "value-on" if str(rec["hub"]).startswith("⚡") else "value-warn",
                "as the statusline reports it"))

        q = rec.get("question") or ""
        if st == "waiting":
            if q:
                out.append(Static(f"❓ {q}", classes="live-question"))
            # squad answer is FAIL-CLOSED: it presses a digit only when a
            # visible option matches the intent, so these buttons cannot
            # answer a dialog that is not on that agent's screen.
            #
            # classes= for the container, NOT a fixed id. The live section is
            # torn down and remounted on content change, removal is
            # asynchronous, and a fixed id here is exactly the DuplicateIds
            # crash the _gen discipline exists to prevent — the operator's
            # first click on a waiting agent found it (2026-07-29: "as soon
            # as it loaded, the page crashed"). The buttons keep their
            # gen-baked ids for the Pressed handler.
            out.append(Horizontal(
                Button("yes", id=f"ans-{g}-yes", variant="success", compact=True),
                Button("no", id=f"ans-{g}-no", variant="error", compact=True),
                Button("always", id=f"ans-{g}-always", variant="warning", compact=True),
                classes="answers",
            ))

        nxt = rec.get("next")
        if nxt:
            src = {"hub": "open DECISION card", "bio": "bio next:",
                   "recap": "recap"}.get(nxt.get("source", ""), nxt.get("source", ""))
            age = f" · {nxt['age']} ago" if nxt.get("age") else ""
            hand = "🙋 " if nxt.get("hand") else ""
            text = nxt.get("ask") or nxt.get("text") or ""
            net = nxt.get("net")
            net_s = f"  [net {net:+d}]" if isinstance(net, int) else ""
            klass = "live-question" if nxt.get("hand") else "live-next"
            out.append(Static(f"{hand}{text}{net_s}", classes=klass))
            out.append(Static(f"{src}{age}", classes="source"))

        branch = rec.get("branch") or ""
        dirty = int(rec.get("dirty") or 0)
        ahead = int(rec.get("unpushed") or 0)
        if branch or dirty or ahead:
            bits = [branch] if branch else []
            if dirty:
                bits.append(f"{dirty} dirty")
            if ahead:
                bits.append(f"{ahead} unpushed")
            klass = "live-unshipped" if (dirty or ahead) else "value-ro"
            out.append(Vertical(
                Horizontal(Label("Git", classes="label"),
                           Static(" · ".join(bits), classes=klass),
                           classes="row-line"),
                Static("unshipped work is invisible to everyone, including its author"
                       if (dirty or ahead) else "", classes="source"),
                classes="row",
            ))

        utot, uhr = int(rec.get("usage_today") or 0), int(rec.get("usage_hour") or 0)
        if utot or uhr:
            out.append(Vertical(
                Horizontal(Label("Usage today", classes="label"),
                           Static(f"{_human_tok(utot)} · last hr {_human_tok(uhr)}",
                                  classes="value-ro"),
                           classes="row-line"),
                Static("output tokens, read from the transcripts", classes="source"),
                classes="row",
            ))
        return out

    @staticmethod
    def _live_render_key(rec: dict[str, Any] | None) -> tuple:
        """What the live section DISPLAYS, not the raw record. waiting_seconds
        ticks on every scan, so keying on repr(rec) rebuilt the section — and
        its buttons — every 3s for precisely the agents whose buttons the
        operator is about to click. Key on the rendered forms instead: the
        timer only changes this key when the displayed minute does."""
        if rec is None:
            return (None,)
        nxt = rec.get("next") or {}
        return (
            rec.get("state"), _hms(rec.get("waiting_seconds", 0)),
            rec.get("action"), rec.get("question"),
            rec.get("model"), rec.get("ctx"), rec.get("hub"),
            nxt.get("source"), nxt.get("age"), nxt.get("hand"),
            nxt.get("text"), nxt.get("ask"), nxt.get("net"),
            rec.get("branch"), rec.get("dirty"), rec.get("unpushed"),
            rec.get("usage_today"), rec.get("usage_hour"),
        )

    async def _refresh_live_section(self) -> None:
        """Rebuild #live ONLY when its rendered content changed, and AWAIT the
        teardown. remove_children() is asynchronous: unawaited, the previous
        render's widgets are still in the tree when the next ones mount, and
        any fixed id collides — DuplicateIds took the whole app down the first
        time the operator selected a waiting agent (2026-07-29). Same defect,
        same cure as refresh_detail's, documented on _gen."""
        sel = self.selected or {}
        if sel.get("kind") != "agent" or not sel.get("local"):
            return
        rec = (self.board.get("agents") or {}).get(sel["agent"])
        key = (sel["key"], self._live_render_key(rec), repr(self.board.get("error")))
        if key == self._live_key:
            return
        self._live_key = key
        try:
            live = self.query_one("#live", Vertical)
        except Exception:  # noqa: BLE001 — detail mid-rebuild; refresh_detail will paint it
            return
        self._gen += 1
        await live.remove_children()
        await live.mount_all(self._live_widgets(rec, sel))

    # ---- rendering the selected agent ----

    async def refresh_detail(self) -> None:
        detail = self.query_one("#detail", VerticalScroll)
        await detail.remove_children()      # AWAITED: mounts below raced it
        self._gen += 1
        self._live_key = None
        sel = self.selected or {}
        kind = sel.get("kind")
        if kind == "machine":
            await detail.mount_all(self._machine_widgets(sel))
            return
        if kind == "workspace":
            await detail.mount_all(self._workspace_widgets(sel))
            return
        if kind == "agent" and not sel.get("local"):
            await detail.mount_all(self._remote_agent_widgets(sel))
            return
        if kind != "agent" or not self.agents:
            await detail.mount(Static(
                "nothing selected — pick a machine, a workspace or a seat "
                "on the left", classes="note"))
            return
        agent = self.agents[sel["roster_ix"]]
        # the live section mounts first (empty shell; _refresh_live_section
        # fills it) so the board's answer is always above the settings sheet
        widgets: list[Any] = [Vertical(id="live")]
        try:
            self.model = self._model_for(agent["worktree"])
        except Exception as exc:  # noqa: BLE001
            self.model = None
            widgets.append(Static(f"could not read settings: {exc}", classes="note"))
            await detail.mount_all(widgets)
            await self._refresh_live_section()
            return
        if not self.model:
            # Not a failure — a folder with no hub identity. Saying so beats an
            # empty pane, which reads as broken.
            widgets.append(Static(
                "No hub identity for this folder. Settings are derived from a git "
                "remote plus ~/.mcp-hub/config.json, so a plain folder added with "
                "`squad add-folder` has none.", classes="note"))
            await detail.mount_all(widgets)
            await self._refresh_live_section()
            return
        for si, section in enumerate(self.model.get("sections", [])):
            head = section["title"]
            if section.get("note"):
                head += f"   {section['note']}"
            widgets.append(Static(f"▌{head}", classes="section"))
            for ri, row in enumerate(section.get("rows", [])):
                widgets.append(self._row_widget(si, ri, row))
        await detail.mount_all(widgets)     # one mount: never seen half-built
        await self._refresh_live_section()

    def _row_widget(self, si: int, ri: int, row: dict[str, Any]) -> Vertical:
        edit = row.get("edit")
        if edit:
            choices = list(edit["choices"])
            # The current value MUST be selectable, or the control opens blank
            # and the panel stops showing what the setting actually is. Values
            # and choices share one vocabulary by construction; this keeps the
            # widget honest if one ever drifts from the other.
            if row["value"] not in choices:
                choices = [row["value"], *choices]
            control: Any = Select(
                [(c, c) for c in choices], value=row["value"],
                allow_blank=False, id=f"sel-{self._gen}-{si}-{ri}", compact=True,
            )
        else:
            control = Static(str(row["value"]), classes=_value_class(row["value"]))
        source = row.get("source", "")
        if edit:
            source = f"{source} · applies {edit['applies']}"
        return Vertical(
            Horizontal(Label(str(row["label"]), classes="label"), control,
                       classes="row-line"),
            Static(source, classes="source"),
            classes="row",
        )

    # ---- events ----

    @on(Tree.NodeHighlighted, "#fleet")
    async def _node_changed(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if not data:
            return
        # Expanding a branch re-validates the cursor and fires a highlight for
        # wherever it currently is — which, mid-jump, is the row we are leaving.
        # Honouring it would land the detail pane on the wrong thing.
        if self._pending_key and data.get("key") != self._pending_key:
            return
        # Key comparison, not identity: every poll replaces the data dicts
        # wholesale, so `is` would repaint the detail pane three times a
        # second for a selection that never moved.
        if self.selected and self.selected.get("key") == data.get("key"):
            self.selected = data
            return
        self.selected = data
        if data.get("roster_ix") is not None:
            self.agent_ix = data["roster_ix"]
        await self.refresh_detail()

    @on(Button.Pressed)
    def _answer_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        sel = self.selected or {}
        if bid.startswith("wsreg-"):
            path = sel.get("path", "")
            if not path:
                return
            self._set_status(f"registering {sel.get('name', path)}…")
            self.run_worker(
                lambda: self._apply(self.hub_bin, ["workspaces", "register", path],
                                    f"register {sel.get('name', '')}", "hub"),
                thread=True, exclusive=True,
            )
            return
        if not bid.startswith("ans-"):
            return
        intent = bid.rsplit("-", 1)[-1]
        if intent not in {"yes", "no", "always"}:
            return
        if sel.get("kind") != "agent" or not sel.get("local"):
            return
        agent = sel["agent"]
        self._set_status(f"answering {agent}: {intent}…")
        self.run_worker(
            lambda: self._apply(self.squad_bin, ["answer", agent, intent],
                                f"answer {agent}", intent),
            thread=True, exclusive=True,
        )

    @on(Select.Changed)
    def _value_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK or not self.model:
            return
        sel_id = event.select.id or ""
        try:
            _, _gen, si, ri = sel_id.split("-")
            row = self.model["sections"][int(si)]["rows"][int(ri)]
        except (ValueError, IndexError, KeyError):
            return
        edit = row.get("edit")
        # The widget id is a claim; resolve it against the model actually
        # rendered and refuse anything that is not an editable row.
        if not edit or event.value == row["value"] or event.value not in edit["choices"]:
            return
        argv = [event.value if a == "{}" else a for a in edit["argv"]]
        exe = self.hub_bin if edit.get("bin") == "mcp-hub" else self.squad_bin
        self._set_status(f"applying {row['label']} → {event.value}…")
        self.run_worker(
            lambda: self._apply(exe, argv, row["label"], event.value),
            thread=True, exclusive=True,
        )

    def _apply(self, exe: str, argv: list[str], label: str, value: str) -> None:
        """Runs on a WORKER THREAD. A mute is a network round trip to the hub,
        and doing it on the UI thread is what froze the previous panel — "it
        got stuck" — with the old value on screen and no way to tell whether
        anything was happening."""
        try:
            subprocess.run([exe, *argv], check=True, capture_output=True,
                           text=True, timeout=20)
            msg = f"{label} → {value}"
        except subprocess.TimeoutExpired:
            msg = f"{label}: timed out — is the hub reachable?"
        except subprocess.CalledProcessError as exc:
            msg = (exc.stderr or exc.stdout or "failed").strip().splitlines()[-1][:90]
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:90]
        # Re-read rather than trust the value we sent: a write can be rejected,
        # clamped or normalised, and on failure the widget has already moved.
        self.call_from_thread(self._after_apply, msg)

    def _after_apply(self, msg: str) -> None:
        self._set_status(msg)
        self.call_later(self.refresh_detail)

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    @staticmethod
    def _short_dir(path: str | None) -> str:
        """The parent directory, home-shortened. `~` is worth 11 cells here."""
        if not path:
            return "definition only — nothing materialized"
        parent = str(pathlib.PurePosixPath(path).parent)
        home = str(pathlib.Path.home())
        if parent == home:
            return "~"
        if parent.startswith(home + "/"):
            return "~/" + parent[len(home) + 1:]
        return parent

    @staticmethod
    def _fact(label: str, value: str, klass: str = "value-ro",
              source: str = "") -> Vertical:
        return Vertical(
            Horizontal(Label(label, classes="label"),
                       Static(value, classes=klass), classes="row-line"),
            Static(source, classes="source"),
            classes="row",
        )

    def _machine_widgets(self, m: dict[str, Any]) -> list[Any]:
        head = "(machine unknown)" if m["unknown"] else m["machine"]
        out: list[Any] = [
            Static(f"▌MACHINE   {head}", classes="section section-live")]
        if m["unknown"]:
            out.append(Static(
                "Agents whose name matches no enrolled machine. Identity is "
                "`<repo>-<hostname>`, so this is a box the hub has never been "
                "told about — enrol it with `mcp-hub machines enrol`. They are "
                "shown rather than dropped: nothing gets lost track of.",
                classes="note"))
        out.append(self._fact(
            "Role", "this machine" if m["local"] else "remote",
            "value-on" if m["local"] else "value-ro",
            "local seats carry live pane data; remote seats carry presence only"))
        out.append(self._fact("Seats", str(m["agent_count"])))
        out.append(self._fact("Workspaces", str(len(m["workspaces"]))))
        if m["open_count"]:
            out.append(self._fact("Open now", str(m["open_count"]), "value-on",
                                  "a board is running in front of a human"))
        if m["drift_count"]:
            out.append(self._fact(
                "Drift", f"{m['drift_count']} workspace(s)", "value-warn",
                "registered with no file, or a file nobody registered"))
        if m["stale"]:
            out.append(Static(
                "The fleet snapshot (~/.mcp-hub/fleet-board.json) has not been "
                "written recently, so this box is NOT REPORTING — which is not "
                "the same as quiet. Every remote state here reads unknown "
                "until a daemon writes it again.", classes="note"))
        return out

    def _workspace_widgets(self, w: dict[str, Any]) -> list[Any]:
        """One workspace, three truth columns, drift loud.

        The columns are the `w` view's, kept verbatim when that view was
        absorbed into the tree; what changed is that the PATH now has a pane
        wide enough to hold it instead of overflowing a 46-cell row.
        """
        out: list[Any] = [
            Static(f"▌WORKSPACE   {w['name']}", classes="section section-live")]
        if w["error"]:
            out.append(Static(f"⚠ this file will not parse: {w['error']}",
                              classes="note"))
        local = w["machine"] == self.tree_model.get("this_machine")
        out.append(self._fact(
            "Machine",
            f"{w['machine'] or '(unknown)'}"
            f"{'  · this machine' if local else '  · remote'}"))
        reg = w["registered"]
        out.append(self._fact(
            "Registered",
            {True: "yes", False: "no", None: "unknown"}[reg],
            {True: "value-on", False: "value-warn", None: "value-warn"}[reg],
            "the hub holds a definition for it" if reg is True
            else ("nothing declared it — `mcp-hub workspaces register`"
                  if reg is False else
                  "the hub did not answer, so this is not an accusation"),
        ))
        out.append(self._fact(
            "On disk", "yes" if w["on_disk"] else "no",
            "value-on" if w["on_disk"] else "value-warn",
            "" if w["on_disk"] else
            "ghost — a definition nothing has materialized here"))
        if w["here"]:
            open_v, open_k, open_s = "you are here", "value-on", \
                "this board is scoped to it"
        elif w["open_now"]:
            open_v, open_k, open_s = "yes", "value-on", \
                "a board pinged within the hub's 180s window"
        else:
            open_v, open_k, open_s = "no", "value-off", \
                "no board has claimed it recently"
        out.append(self._fact("Open now", open_v, open_k, open_s))
        if w["squad"]:
            out.append(self._fact("Squad", w["squad"], "value-on",
                                  "broadcasts scoped here reach these seats"))
        out.append(self._fact(
            "Folder", self._short_dir(w["path"]) if w["path"] else "—",
            "value-ro", w["path"] or "no file on any machine that reported"))
        out.append(self._fact(
            "Seats", str(len(w["agents"])) if w["agents"] else "none attributed",
            "value-ro" if w["agents"] else "value-off",
            "" if w["agents"] else
            "a remote workspace only lists its folders once REGISTERED, so its "
            "seats sit under the machine instead"))
        for a in w["agents"]:
            out.append(Static(
                f"  {a['agent']}" + ("" if a["local"] else "   (presence only)"),
                classes="ws-row" if a["local"] else "ws-row-open"))
        if reg is False and w["path"] and \
                w["machine"] == self.tree_model.get("this_machine"):
            # Offered only where it can actually be done: registering another
            # machine's file would name a path this box cannot verify.
            out.append(Horizontal(
                Button("register this workspace", id=f"wsreg-{self._gen}",
                       variant="warning", compact=True),
                classes="answers"))
        return out

    def _remote_agent_widgets(self, a: dict[str, Any]) -> list[Any]:
        out: list[Any] = [
            Static(f"▌SEAT   {a['agent']}", classes="section section-live")]
        out.append(Static(
            "Presence only. This seat is on another machine, where there is no "
            "pane to scrape — the hub knows it exists and whether it can be "
            "woken, and claiming more than that is how 'delivered live' came "
            "to be reported about an agent that could not receive.",
            classes="note"))
        out.append(self._fact("Machine", a["machine"] or "(unknown)"))
        if a.get("project"):
            out.append(self._fact("Project", a["project"]))
        if a.get("stale"):
            out.append(self._fact("State", "not reporting", "value-warn",
                                  "the fleet snapshot has gone stale"))
        else:
            out.append(self._fact(
                "State", a.get("state", "unknown"),
                "value-on" if a.get("state") == "working" else "value-ro",
                "from the daemons' fleet snapshot, not from a pane"))
            out.append(self._fact(
                "Wakeable", "yes" if a.get("wakeable") else "no",
                "value-on" if a.get("wakeable") else "value-warn",
                "bound for channel-push wake" if a.get("wakeable")
                else "queued messages wait for its next turn boundary"))
            out.append(self._fact("Sessions", str(a.get("sessions", 0))))
        if a.get("next"):
            out.append(Static(a["next"], classes="live-next"))
            out.append(Static("its bio `next:`", classes="source"))
        return out

    # ---- the command palette ----

    def run_verb(self, exe: str, argv: list[str], label: str,
                 value: str = "") -> None:
        """Run one squad/hub verb on a worker thread and report what it said.

        Everything the palette does goes through here — the same path the
        dropdowns use, so a palette command cannot do anything the panel's
        own controls could not, and cannot freeze the UI doing it.
        """
        self._set_status(f"{label}…")
        self.run_worker(
            lambda: self._apply(exe, argv, label, value),
            thread=True, exclusive=True,
        )

    def _goto(self, key: str):
        def run() -> None:
            for node in self._all_nodes():
                if (node.data or {}).get("key") == key:
                    self._move_to(node)
                    self.call_later(self.refresh_detail)
                    return
        return run

    def palette_commands(self) -> list[tuple[str, str, Any]]:
        """(title, help, callback) for everything runnable right now.

        Ordered by how close it is to hand: the selection's own actions first,
        then navigation, then the app's. A command is only listed when the
        current selection can actually perform it — offering `answer` for a
        remote seat would be a button that lies.
        """
        out: list[tuple[str, str, Any]] = []
        sel = self.selected or {}
        agent = sel.get("agent") if sel.get("kind") == "agent" else None

        if agent:
            where = "this machine" if sel.get("local") else sel.get("machine", "")
            for mins, word in ((30, "30m"), (60, "1h"), (120, "2h")):
                out.append((
                    f"Focus {word} — {agent}",
                    f"do not disturb on {where}; urgent still gets through",
                    (lambda m=mins: self.run_verb(
                        self.hub_bin, ["focus", str(m), "--agent", agent],
                        f"focus {agent}", f"{m}m")),
                ))
            out.append((
                f"Focus off — {agent}", "start hearing normal messages again",
                lambda: self.run_verb(
                    self.hub_bin, ["focus", "--off", "--agent", agent],
                    f"focus {agent}", "off"),
            ))
        if agent and sel.get("local"):
            if sel.get("state") == "waiting":
                for intent in ("yes", "no", "always"):
                    out.append((
                        f"Answer {intent} — {agent}",
                        "fail-closed: presses nothing unless that option is "
                        "on the agent's screen",
                        (lambda i=intent: self.run_verb(
                            self.squad_bin, ["answer", agent, i],
                            f"answer {agent}", i)),
                    ))
            for verb, why in (("restart", "relaunch it, keeping its history"),
                              ("stop", "close its pane"),
                              ("start", "launch it")):
                out.append((
                    f"{verb.capitalize()} agent — {agent}", why,
                    (lambda v=verb: self.run_verb(
                        self.squad_bin, [v, agent], f"{v} {agent}", agent)),
                ))

        if sel.get("kind") == "workspace" and sel.get("registered") is False \
                and sel.get("path") \
                and sel.get("machine") == self.tree_model.get("this_machine"):
            out.append((
                f"Register workspace — {sel['name']}",
                "declare it on the hub so it stops reading as drift",
                lambda p=sel["path"]: self.run_verb(
                    self.hub_bin, ["workspaces", "register", p],
                    f"register {sel['name']}", "hub"),
            ))
        out.append((
            "Register every unregistered workspace here",
            "`mcp-hub workspaces register --all` for this machine",
            lambda: self.run_verb(
                self.hub_bin, ["workspaces", "register", "--all"],
                "register all workspaces", "hub"),
        ))

        for node in self._agent_nodes():
            data = node.data or {}
            out.append((
                f"Go to {data.get('agent', '')}",
                f"{data.get('machine', '')}"
                f"{'' if data.get('local') else ' · presence only'}",
                self._goto(data.get("key", "")),
            ))
        for m in self.tree_model.get("machines", []):
            for w in m["workspaces"]:
                out.append((
                    f"Go to workspace {w['name']}",
                    f"{w['machine']}"
                    f"{' · drift' if w['drift'] else ''}",
                    self._goto(w["key"]),
                ))

        out.append(("Next needs-you", "jump to the next raised hand",
                    self.action_next_hand))
        out.append(("Expand or collapse the tree", "everything, or this box only",
                    self.action_expand_all))
        out.append(("Reload", "re-poll the board and the registry",
                    lambda: self.run_worker(self.action_reload())))
        out.append(("Light / dark", "override the terminal's answer",
                    self.action_toggle_theme))
        return out

    async def action_reload(self) -> None:
        await self.refresh_detail()
        if self._board_for is not None:
            self._poll_board()
        if self._workspaces_for is not None:
            self._poll_workspaces()
        self._set_status("reloaded")

    def action_toggle_theme(self) -> None:
        """The detector's answer, overridable in one keystroke — detection can
        be wrong (tmux without passthrough, an emulator that won't answer) and
        a wrong theme you cannot change is worse than no detection."""
        dark_now = self.theme == "claude-dark"
        self.theme = "squad-light" if dark_now else "claude-dark"
        # Tree labels carry resolved hex, not CSS variables, so they do not
        # follow a theme change on their own — they would keep the old theme's
        # green until the next structural rebuild.
        self._relabel_tree()

    def action_expand_all(self) -> None:
        """Everything open, or everything back to the default fold.

        The default hides other machines, which is right for acting and wrong
        for surveying; one key covers the second case.
        """
        nodes = [n for n in self._all_nodes() if n.allow_expand]
        if any(not n.is_expanded for n in nodes):
            for n in nodes:
                n.expand()
            self._set_status("expanded")
            return
        want = self._default_expansion(self.tree_model)
        for n in nodes:
            if n.data and n.data.get("key") in want:
                n.expand()
            else:
                n.collapse()
        self._set_status("collapsed to this machine")

    def action_next_hand(self) -> None:
        """Jump the cursor to the next seat that needs the operator.

        Tree ORDER, not roster order — the roster's file order is preserved
        inside each workspace (it is the cockpit's tab order and re-sorting
        would disagree with it), but the cursor now walks what is on screen.
        A hand is a board fact, so only local seats can raise one.
        """
        nodes = self._agent_nodes()
        if not nodes:
            return
        keys = [n.data.get("key") for n in nodes]
        try:
            start = keys.index((self.selected or {}).get("key"))
        except ValueError:
            start = -1
        for step in range(1, len(nodes) + 1):
            node = nodes[(start + step) % len(nodes)]
            if (node.data or {}).get("hand"):
                self._move_to(node)
                return
        self._set_status("nobody needs you 🎉")
