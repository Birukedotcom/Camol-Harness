"""Thin read-only terminal monitor; deliberately separate from local run authority."""

import asyncio
import math

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .remote_monitor import display_text, render_snapshot
from .ssh_protocol import SSHTransportError


class RemoteMonitorApp(App):
    TITLE = "Camol remote monitor"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #020604; color: #c7d2ca; }
    #remote-header { height: 3; color: #74e391; background: #07100a; padding: 0 1; }
    #remote-nav { width: 36; min-width: 20; border: solid #205f38; }
    #remote-search { height: 3; }
    #remote-boxes { height: 1fr; background: #020604; }
    #remote-boxes:focus, #remote-search:focus { border: solid #45e07a; }
    #remote-boxes, #remote-detail {
        scrollbar-size-vertical: 1;
        scrollbar-color: #1f713c;
        scrollbar-background: #07100a;
        scrollbar-color-hover: #32a85a;
        scrollbar-color-active: #52e27e;
        scrollbar-background-hover: #07100a;
        scrollbar-background-active: #07100a;
        scrollbar-corner-color: #07100a;
    }
    #remote-boxes > .option-list--option-highlighted { background: #143d25; color: #a0ffb9; }
    #remote-page { height: 2; }
    #remote-detail { width: 1fr; border: solid #205f38; background: #020604; }
    #remote-bottom { height: 2; background: #07100a; color: #74e391; }
    Footer { background: #07100a; }
    FooterKey {
        background: #07100a;
        .footer-key--key { color: #68e892; background: #07100a; }
        .footer-key--description { color: #b7c3ba; background: #07100a; }
    }
    """
    BINDINGS = [Binding("ctrl+c", "leave", "Detach", priority=True),
                Binding("q", "leave", "Detach"), Binding("r", "refresh", "Refresh"),
                Binding("escape", "overview", "Overview", priority=True),
                Binding("ctrl+n", "next_page", "Next page", priority=True),
                Binding("ctrl+p", "previous_page", "Previous page", priority=True)]

    def __init__(self, monitor, *, interval=5):
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 2 <= interval <= 300:
            raise SSHTransportError("POLICY_DENIED", "monitor interval must be within 2..300 seconds")
        super().__init__()
        self.monitor, self.interval = monitor, interval
        self.snapshot = None
        self.selected_box = None
        self.page, self.page_rows, self.query = 0, [], ""
        self._generation, self._wanted, self._closed, self._job = 0, False, False, None
        self.connection = "CONNECTING"

    def compose(self) -> ComposeResult:
        yield Static("CAMOL / REMOTE — read-only", id="remote-header", markup=False)
        with Horizontal():
            with Vertical(id="remote-nav"):
                yield Input(placeholder="Filter boxes / tasks", id="remote-search", max_length=256)
                yield OptionList(id="remote-boxes")
                yield Static("No observation yet", id="remote-page", markup=False)
            yield RichLog(id="remote-detail", wrap=True, markup=False, highlight=False, max_lines=2000, min_width=0)
        yield Static("[ESC OVERVIEW]  [Ctrl+N/P pages]  [Ctrl+C DETACH]", id="remote-bottom", markup=False)
        yield Footer()

    def on_mount(self):
        self.set_interval(self.interval, self.action_refresh)
        self.action_refresh()
        self.query_one("#remote-boxes", OptionList).focus()

    def action_refresh(self):
        if self._closed:
            return
        self._wanted = True
        if self._job is None or self._job.done():
            self._job = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self):
        while self._wanted and not self._closed:
            self._wanted = False
            generation = self._generation
            try:
                snapshot = await self.monitor.refresh(self.selected_box)
                if generation != self._generation or self._closed:
                    continue
                self.snapshot, self.connection = snapshot, "OBSERVED"
                self._paint(render_snapshot(snapshot))
            except asyncio.CancelledError:
                raise
            except (SSHTransportError, OSError, ValueError) as error:
                if generation != self._generation or self._closed:
                    continue
                self.connection = "STALE" if self.snapshot is not None else "UNAVAILABLE"
                # No raw transport errors or fallback to local project state.
                code = getattr(error, "code", "INSPECTION_FAILED")
                old = render_snapshot(self.snapshot) if self.snapshot else "No verified observation retained."
                self._paint(display_text("{} — {}\nRetained data is not a current connection.\n\n".format(self.connection, code)) + old)

    def _paint(self, body):
        name = display_text(self.monitor.target.name, 100)
        selected = display_text((self.snapshot or {}).get("selected_box") or "OVERVIEW", 100)
        if self.snapshot is not None and self.snapshot["selected_box"] != self.selected_box:
            selected += " (requested {} not observed)".format(display_text(self.selected_box or "OVERVIEW", 100))
        at = self.snapshot["observed_at"] if self.snapshot else "never"
        self.query_one("#remote-header", Static).update("CAMOL / REMOTE — {} — READ ONLY\n{} | {} | last observation {}".format(self.connection, name, selected, at))
        detail = self.query_one("#remote-detail", RichLog)
        detail.clear()
        detail.write(Text(body))
        self._paint_rows()

    def _paint_rows(self):
        rows = self.snapshot["rows"] if self.snapshot else []
        words = self.query.casefold().split()
        rows = [row for row in rows if all(word in " ".join(str(value or "") for value in row.values()).casefold() for word in words)]
        pages = max(1, (len(rows) + 49) // 50)
        self.page = min(self.page, pages - 1)
        self.page_rows = rows[self.page * 50:(self.page + 1) * 50]
        options = self.query_one("#remote-boxes", OptionList)
        highlighted = options.highlighted
        highlighted_id = None
        if highlighted is not None and highlighted < options.option_count:
            highlighted_id = options.get_option_at_index(highlighted).id
        options.clear_options()
        options.add_options([Option(Text(display_text("{} | {} | {}".format(row["box_id"], row["status"], row["task_id"] or "unassigned"), 240)), id=row["box_id"]) for row in self.page_rows])
        if self.page_rows:
            options.highlighted = next((i for i, row in enumerate(self.page_rows) if row["box_id"] == highlighted_id), 0)
        self.query_one("#remote-page", Static).update("Page {}/{} | {} matches\n{} boxes observed".format(self.page + 1, pages, len(rows), len(self.snapshot["rows"]) if self.snapshot else 0))

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "remote-search":
            self.query, self.page = event.value[:256], 0
            self._paint_rows()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.selected_box = event.option.id
        self._generation += 1
        self.action_refresh()

    def action_overview(self):
        self.selected_box = None
        self._generation += 1
        self.action_refresh()
        self.query_one("#remote-boxes", OptionList).focus()

    def action_next_page(self):
        self.page += 1
        self._paint_rows()

    def action_previous_page(self):
        self.page = max(0, self.page - 1)
        self._paint_rows()

    async def close_monitor(self):
        self._closed = True
        if self._job is not None and not self._job.done():
            self._job.cancel()
            await asyncio.gather(self._job, return_exceptions=True)

    async def action_leave(self):
        await self.close_monitor()
        self.exit()

    async def on_unmount(self):
        await self.close_monitor()
