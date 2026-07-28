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
from textual.widgets import Footer, Header, Label, ListItem, ListView, Select, Static

CSS = """
Screen { layout: vertical; }
#body { height: 1fr; }

/* Explicit heights everywhere. Textual containers default to 1fr, and a 1fr
   child inside an auto-height parent collapses to nothing — which rendered
   exactly one row of the panel and blank space below it. */
#agents {
    width: 36;
    border-right: solid $panel-lighten-2;
    background: $surface;
    height: 1fr;
}
#agents > ListItem { height: 2; padding: 0 1; }
#agents > ListItem.-highlight { background: $accent 40%; }
.agent-name { text-style: bold; height: 1; }
.agent-class { color: $text-muted; height: 1; }

#detail { padding: 0 2; height: 1fr; }
.section {
    color: $accent;
    text-style: bold;
    height: 1;
    margin: 1 0 0 0;
}
.row { height: 2; }
.row-line { height: 1; }
.label { width: 24; height: 1; }
.value-ro { color: $text; height: 1; width: 1fr; }
.source { color: $text-muted; height: 1; padding: 0 0 0 24; }
Select { width: 30; height: 1; }
#note { color: $warning; padding: 1 0; height: auto; }
#status { dock: bottom; height: 1; padding: 0 2; background: $panel; }
"""



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

    def on_mount(self) -> None:
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
        self.refresh_detail()

    def _first_with_settings(self) -> int:
        for i, a in enumerate(self.agents):
            try:
                if self._model_for(a["worktree"]):
                    return i
            except Exception:  # noqa: BLE001
                continue
        return 0

    # ---- rendering the selected agent ----

    def refresh_detail(self) -> None:
        detail = self.query_one("#detail", VerticalScroll)
        detail.remove_children()
        if not self.agents:
            detail.mount(Static("no agents in this workspace", id="note"))
            return
        agent = self.agents[self.agent_ix]
        try:
            self.model = self._model_for(agent["worktree"])
        except Exception as exc:  # noqa: BLE001
            self.model = None
            detail.mount(Static(f"could not read settings: {exc}", id="note"))
            return
        if not self.model:
            # Not a failure — a folder with no hub identity. Saying so beats an
            # empty pane, which reads as broken.
            detail.mount(Static(
                "No hub identity for this folder. Settings are derived from a git "
                "remote plus ~/.mcp-hub/config.json, so a plain folder added with "
                "`squad add-folder` has none.", id="note"))
            return
        for si, section in enumerate(self.model.get("sections", [])):
            head = section["title"]
            if section.get("note"):
                head += f"   {section['note']}"
            detail.mount(Static(head, classes="section"))
            for ri, row in enumerate(section.get("rows", [])):
                detail.mount(self._row_widget(si, ri, row))

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
                allow_blank=False, id=f"sel-{si}-{ri}", compact=True,
            )
        else:
            control = Static(str(row["value"]), classes="value-ro")
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
    def _agent_changed(self, event: ListView.Highlighted) -> None:
        if event.list_view.index is None or event.list_view.index == self.agent_ix:
            return
        self.agent_ix = event.list_view.index
        self.refresh_detail()

    @on(Select.Changed)
    def _value_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK or not self.model:
            return
        sel_id = event.select.id or ""
        try:
            _, si, ri = sel_id.split("-")
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
        self.refresh_detail()

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def action_reload(self) -> None:
        self.refresh_detail()
        self._set_status("reloaded")
