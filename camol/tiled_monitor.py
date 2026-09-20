"""Bounded native-terminal metadata tiles; selection remains scope-checked."""

from rich.text import Text
from textual.containers import Grid, Vertical
from textual.message import Message
from textual.widgets import Button, Static

from .pane_layout import page, tile_label


class TiledMonitor(Vertical):
    DEFAULT_CSS = """
    TiledMonitor { height: 1fr; min-height: 7; display: none; }
    TiledMonitor > Static { height: 3; color: #83a58d; padding: 0 1; }
    TiledMonitor > Grid { height: 1fr; grid-size: 2 3; grid-gutter: 0 1; padding: 0 1; }
    TiledMonitor Button { width: 100%; height: 100%; min-width: 0; min-height: 3;
        content-align: left middle; border: round #17462a; background: #07100a; color: #dce7df; }
    TiledMonitor Button:focus { border: round #68e892; background: #123a20; }
    """

    class Selected(Message):
        def __init__(self, key, scope):
            super().__init__()
            self.key, self.scope = key, scope

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.snapshot = None
        self.mode = "grid"
        self.number = 1
        self.addresses = {}
        self.page_count = 1

    def compose(self):
        yield Static("", id="monitor-heading", markup=False)
        with Grid():
            for index in range(6):
                yield MonitorTile("", id="monitor-tile-{}".format(index))

    def show_snapshot(self, snapshot, mode=None):
        if mode is not None:
            self.mode = mode
        if self.snapshot is None or self.snapshot["scope"] != snapshot["scope"]:
            self.number = 1
        self.snapshot = snapshot
        self.render_page()

    def unavailable(self, reason):
        # Never leave a healthy-looking interactive tile after a failed read.
        self.addresses = {}
        self.snapshot = None
        for button in self.query(Button):
            button.disabled = True
            button.display = False
            button.address = None
        self.query_one("#monitor-heading", Static).update("MONITOR UNAVAILABLE — " + reason[:180])

    def render_page(self):
        if self.snapshot is None:
            return
        data = page(self.snapshot, self.mode, self.number, self.size.width, self.app.size.height)
        self.number, self.page_count = data["number"], data["pages"]
        grid = self.query_one(Grid)
        grid.styles.grid_size_columns, grid.styles.grid_size_rows = data["columns"], data["rows"]
        self.addresses = {}
        for index, button in enumerate(self.query(Button)):
            button.display = index < len(data["items"])
            button.disabled = not button.display
            button.address = None
            if button.display:
                row = data["items"][index]
                button.label = Text(tile_label(row, self.snapshot["event_cursor"]))
                self.addresses[button.id] = (row["key"], self.snapshot["scope"])
                button.address = self.addresses[button.id]
        self.query_one("#monitor-heading", Static).update(
            "{} | {} | page {}/{} · {} boxes\nrun={} · recorded, not live readiness\nAlt+←/→ pages · Tab/Enter opens · Esc composer".format(
                self.mode.upper(), self.snapshot["basis"], self.number, data["pages"], data["total"], self.snapshot["run_id"]))

    def turn_page(self, direction):
        self.number = max(1, min(self.page_count, self.number + direction))
        self.render_page()

    def on_resize(self):
        self.render_page()


class MonitorTile(Button):
    address = None

    def press(self):
        # Capture at input dispatch, not later when a queued button event could
        # resolve a recycled slot against a refreshed page.
        if not self.disabled and self.address is not None:
            self.post_message(TiledMonitor.Selected(*self.address))
        return self
