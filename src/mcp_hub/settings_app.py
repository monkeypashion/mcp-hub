"""SQUAD BOARD — the fleet dashboard and the settings panel, one screen.

Two apps merged on the operator's call (2026-07-28: "I don't use the squad
board very much... consolidate the two items into one"): the live board — who
needs you, who is working, who is burning the budget — and the per-agent
settings sheet, which was already here. The board half is a RENDERER of
`squad board --json` plus the documented caches (board_data.collect); it never
re-scrapes panes, which is the board's own single-source rule.

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

import subprocess
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)

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
#agents {
    width: 38;
    border-right: solid #d8d8d8;
    background: $surface;
    height: 1fr;
}
#agents > ListItem { height: 2; padding: 0 1; }
#agents > ListItem.-highlight { background: #e8e8e8; color: $foreground; }
.agent-name { text-style: bold; height: 1; color: $primary; }
.agent-live { color: $text-muted; height: 1; }
/* the board vocabulary on the roster: the NAME wears the state */
.st-waiting .agent-name { color: $error; }
.st-working .agent-name { color: $success; }
.st-down    .agent-name { color: $warning; }

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


_STATE_GLYPH = {"waiting": "🔴", "working": "▶", "idle": "·", "down": "✖"}


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


class SettingsApp(App):
    """One screen: the live fleet on the left, the selected agent —
    board detail on top, settings below — on the right."""

    CSS = CSS
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reload", "Reload"),
        ("t", "toggle_theme", "Light/dark"),
        ("n", "next_hand", "Next needs-you"),
    ]

    def __init__(self, agents: list[dict[str, str]], scoped_to: str | None,
                 model_for, squad_bin: str, hub_bin: str,
                 board_for=None, dark: bool | None = None,
                 poll_seconds: float = 3.0):
        super().__init__()
        self.agents = agents
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
            yield ListView(
                *[
                    # Stable per-index ids: the roster is fixed for the app's
                    # life, so these labels are UPDATED on every poll rather
                    # than rebuilt — no churn under the operator's cursor.
                    ListItem(
                        Vertical(
                            Label(a["agent"], classes="agent-name",
                                  id=f"name-{i}"),
                            Label(a.get("klass", ""), classes="agent-live",
                                  id=f"live-{i}"),
                        ),
                        id=f"item-{i}",
                    )
                    for i, a in enumerate(self.agents)
                ],
                id="agents",
            )
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
        # Open on an agent that HAS settings: `squad add-folder` enrols plain
        # directories deliberately and those have no derived identity, so
        # opening on roster row 0 routinely showed an empty panel.
        self.agent_ix = self._first_with_settings()
        lv = self.query_one("#agents", ListView)
        lv.index = self.agent_ix
        lv.focus()
        await self.refresh_detail()
        if self._board_for is not None:
            self._poll_board()                      # first paint: don't wait 3s
            self.set_interval(self._poll_seconds, self._poll_board)

    def _first_with_settings(self) -> int:
        for i, a in enumerate(self.agents):
            try:
                if self._model_for(a["worktree"]):
                    return i
            except Exception:  # noqa: BLE001
                continue
        return 0

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
        live = snap.get("agents") or {}
        for i, a in enumerate(self.agents):
            rec = live.get(a["agent"])
            try:
                item = self.query_one(f"#item-{i}", ListItem)
                line = self.query_one(f"#live-{i}", Label)
            except Exception:  # noqa: BLE001 — mid-teardown query, next poll repaints
                return
            item.remove_class("st-waiting", "st-working", "st-idle", "st-down")
            if not rec:
                line.update(a.get("klass", ""))
                continue
            item.add_class(f"st-{rec.get('state', 'idle')}")
            hand = "🙋 " if (rec.get("next") or {}).get("hand") else ""
            st = rec.get("state", "")
            if st == "waiting":
                tail = f"waiting {_hms(rec.get('waiting_seconds', 0))}"
            elif st == "down":
                tail = "down"
            else:
                tail = st
            bits = [b for b in (
                _STATE_GLYPH.get(st, ""), hand + rec.get("hub", ""),
                rec.get("model", ""), rec.get("ctx", ""), tail,
            ) if b]
            line.update(" ".join(bits))
        # the detail's live section follows the selected agent
        self.call_later(self._refresh_live_section)

    def _live_widgets(self, rec: dict[str, Any] | None) -> list[Any]:
        """The board's view of ONE agent, as widgets. Everything here is a
        rendering of collect()'s record — no scraping, no second source."""
        if rec is None:
            err = (self.board or {}).get("error")
            return [Static(f"live data: {err}", classes="note")] if err else []
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
        if not self.agents:
            return
        rec = (self.board.get("agents") or {}).get(self.agents[self.agent_ix]["agent"])
        key = (self.agent_ix, self._live_render_key(rec), repr(self.board.get("error")))
        if key == self._live_key:
            return
        self._live_key = key
        try:
            live = self.query_one("#live", Vertical)
        except Exception:  # noqa: BLE001 — detail mid-rebuild; refresh_detail will paint it
            return
        self._gen += 1
        await live.remove_children()
        await live.mount_all(self._live_widgets(rec))

    # ---- rendering the selected agent ----

    async def refresh_detail(self) -> None:
        detail = self.query_one("#detail", VerticalScroll)
        await detail.remove_children()      # AWAITED: mounts below raced it
        self._gen += 1
        self._live_key = None
        if not self.agents:
            await detail.mount(Static("no agents in this workspace", classes="note"))
            return
        agent = self.agents[self.agent_ix]
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

    @on(ListView.Highlighted, "#agents")
    async def _agent_changed(self, event: ListView.Highlighted) -> None:
        if event.list_view.index is None or event.list_view.index == self.agent_ix:
            return
        self.agent_ix = event.list_view.index
        await self.refresh_detail()

    @on(Button.Pressed)
    def _answer_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if not bid.startswith("ans-"):
            return
        intent = bid.rsplit("-", 1)[-1]
        if intent not in {"yes", "no", "always"}:
            return
        agent = self.agents[self.agent_ix]["agent"]
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

    async def action_reload(self) -> None:
        await self.refresh_detail()
        if self._board_for is not None:
            self._poll_board()
        self._set_status("reloaded")

    def action_toggle_theme(self) -> None:
        """The detector's answer, overridable in one keystroke — detection can
        be wrong (tmux without passthrough, an emulator that won't answer) and
        a wrong theme you cannot change is worse than no detection."""
        dark_now = self.theme == "claude-dark"
        self.theme = "squad-light" if dark_now else "claude-dark"

    def action_next_hand(self) -> None:
        """Jump the cursor to the next agent that needs the operator — the
        roster keeps FILE ORDER (the cockpit's tab order; re-sorting would
        disagree with it), so needs-you is a key, not a sort."""
        live = self.board.get("agents") or {}
        n = len(self.agents)
        if not n:
            return
        for step in range(1, n + 1):
            i = (self.agent_ix + step) % n
            rec = live.get(self.agents[i]["agent"]) or {}
            if rec.get("state") == "waiting" or (rec.get("next") or {}).get("hand"):
                lv = self.query_one("#agents", ListView)
                lv.index = i
                return
        self._set_status("nobody needs you 🎉")
