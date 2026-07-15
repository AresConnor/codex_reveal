"""Reveal — Codex Log Viewer TUI application."""

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Header, Footer, Label, Tree, TabbedContent
from textual.binding import Binding

from .widgets.session_tree import SessionTree
from .widgets.log_view import LogView
from .models import SessionMeta


CSS = """
Screen {
    layout: vertical;
}

#main-content {
    height: 1fr;
}

#sidebar {
    width: 28;
    border: solid $primary-background;
    background: $surface;
}

#sidebar > .panel-title {
    text-style: bold;
    color: $text;
    background: $primary-background;
    padding: 0 1;
    height: 1;
}

#log-panel {
    width: 1fr;
    border: solid $primary-background;
}

TabbedContent {
    height: 1fr;
}

TabPane {
    height: 1fr;
}

RichLog {
    height: 1fr;
    scrollbar-size: 1 1;
}

#status-bar {
    height: 1;
    background: $primary-background;
    color: $text-muted;
    padding: 0 1;
}

#status-bar Label {
    width: 1fr;
}
"""


class RevealApp(App):
    """Codex log viewer with session browser and live SSE monitoring."""

    TITLE = "Reveal — Codex Log Viewer"
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("tab", "focus_next", "Next Panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev Panel", show=False),
        Binding("l", "tab('live')", "Live SSE", show=True),
        Binding("h", "tab('history')", "History", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="main-content"):
            yield SessionTree(id="sidebar")
            yield LogView(id="log-panel")

        with Horizontal(id="status-bar"):
            yield Label("", id="status-left")
            yield Label("", id="status-center")
            yield Label("", id="status-right")

    def on_mount(self):
        self.set_interval(0.5, self._poll_sse)
        self.set_interval(2.0, self._update_status)
        self._update_status()

    # ── Message handlers ────────────────────────────────────────────

    def on_session_tree_session_selected(self, event: SessionTree.SessionSelected):
        """When user selects a session in the tree, load its history."""
        log_view = self.query_one("#log-panel", LogView)
        log_view.load_session(event.meta)

        # Switch to history tab
        tabs = log_view.query_one(TabbedContent)
        tabs.active = "history"

        name = event.meta.agent_nickname or "root"
        self._set_status(f"Loaded: {name}")

    # ── Actions ──────────────────────────────────────────────────────

    def action_refresh(self):
        """Refresh the session tree and SSE source."""
        tree = self.query_one("#sidebar", SessionTree)
        tree.refresh_sessions()
        log_view = self.query_one("#log-panel", LogView)
        log_view._sse.reset()
        self._set_status("Refreshed")

    def action_tab(self, tab: str):
        """Switch to a tab in the log view."""
        log_view = self.query_one("#log-panel", LogView)
        tabs = log_view.query_one(TabbedContent)
        tabs.active = tab

    # ── Internal ─────────────────────────────────────────────────────

    def _poll_sse(self):
        log_view = self.query_one("#log-panel", LogView)
        log_view.poll_sse()

    def _update_status(self):
        log_view = self.query_one("#log-panel", LogView)
        sse_total = log_view._sse.count_total()

        self.query_one("#status-center", Label).update(
            f" SSE events: {sse_total}"
        )
        self.query_one("#status-right", Label).update(
            " Ctrl+R Refresh  |  L Live  |  H History  |  Ctrl+Q Quit"
        )

    def _set_status(self, msg: str):
        self.query_one("#status-left", Label).update(f" {msg}")
