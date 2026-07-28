"""The squad settings panel, as a Textual app.

Why a framework rather than the hand-rolled curses version this replaces: the
keyboard, focus, mouse and widget behaviour are exactly what I got wrong by
hand — one line binding ESC to quit made every arrow key exit the program,
because VSCode sends arrows as `ESC [ B` and the leading byte arrives as 27.
That class of defect is not mine to solve; a toolkit owns it.

The model comes from `_settings_model` unchanged: every row already carries its
value, its SOURCE, and — when it can be changed — the exact argv to change it.
That layer is proven and this file only renders it, so the panel cannot offer
an edit the underlying verb cannot perform.
"""
from __future__ import annotations

import subprocess
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import Footer, Header, Label, ListItem, ListView, Select, Static

# Claude's own palette, read out of the Claude Code binary's CSS custom
# properties rather than recalled — `--clay: #d97757`, `--clay-emphasized`,
# `--plum`, `--mineral`, `--peach`, and a 40-step WARM grey ramp (#fcfcfb …
# #0b0b0b) that is what makes it read as Claude rather than as a generic dark
# theme: the greys are not neutral, they lean yellow.
#
#   grep -aoE "#[0-9a-f]{6}" ~/.local/share/claude/versions/<v>
#
# Both variants are registered, so Ctrl+P still switches to any of Textual's
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
# The hues are `squad board`'s, which is a VOCABULARY rather than decoration —
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
    name="claude-light",   # the DEFAULT — see on_mount
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
    width: 36;
    background: $background;
    border-right: solid #d8d8d8;
    background: $surface;
    height: 1fr;
}
#agents > ListItem { height: 2; padding: 0 1; }
#agents > ListItem.-highlight { background: #e8e8e8; color: $foreground; }
.agent-name { text-style: bold; height: 1; color: $primary; }
.agent-class { color: $text-muted; height: 1; }

#detail { padding: 0 2; height: 1fr; }
.section {
    color: $primary;
    text-style: bold;
    height: 1;
    margin: 1 0 0 0;
}
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


class SettingsApp(App):
    """One screen: agents on the left, the selected agent's settings on the right."""

    CSS = CSS
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reload", "Reload"),
    ]

    def __init__(self, agents: list[dict[str, str]], scoped_to: str | None,
                 model_for, squad_bin: str, hub_bin: str):
        super().__init__()
        self.agents = agents
        self.scoped_to = scoped_to
        self._model_for = model_for       # injected so tests need no real repo
        self.squad_bin = squad_bin
        self.hub_bin = hub_bin
        self.agent_ix = 0
        self.model: dict[str, Any] | None = None
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
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            yield ListView(
                *[
                    ListItem(
                        Vertical(
                            Label(a["agent"], classes="agent-name"),
                            Label(a.get("klass", ""), classes="agent-class"),
                        )
                    )
                    for a in self.agents
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
        self.theme = "squad-light"
        self.title = "Squad settings"
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

    def _first_with_settings(self) -> int:
        for i, a in enumerate(self.agents):
            try:
                if self._model_for(a["worktree"]):
                    return i
            except Exception:  # noqa: BLE001
                continue
        return 0

    # ---- rendering the selected agent ----

    async def refresh_detail(self) -> None:
        detail = self.query_one("#detail", VerticalScroll)
        await detail.remove_children()      # AWAITED: mounts below raced it
        self._gen += 1
        if not self.agents:
            await detail.mount(Static("no agents in this workspace", classes="note"))
            return
        agent = self.agents[self.agent_ix]
        try:
            self.model = self._model_for(agent["worktree"])
        except Exception as exc:  # noqa: BLE001
            self.model = None
            await detail.mount(Static(f"could not read settings: {exc}", classes="note"))
            return
        if not self.model:
            # Not a failure — a folder with no hub identity. Saying so beats an
            # empty pane, which reads as broken.
            await detail.mount(Static(
                "No hub identity for this folder. Settings are derived from a git "
                "remote plus ~/.mcp-hub/config.json, so a plain folder added with "
                "`squad add-folder` has none.", classes="note"))
            return
        widgets: list[Any] = []
        for si, section in enumerate(self.model.get("sections", [])):
            head = section["title"]
            if section.get("note"):
                head += f"   {section['note']}"
            widgets.append(Static(f"\u258c{head}", classes="section"))
            for ri, row in enumerate(section.get("rows", [])):
                widgets.append(self._row_widget(si, ri, row))
        await detail.mount_all(widgets)     # one mount: never seen half-built

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
        self._set_status("reloaded")
