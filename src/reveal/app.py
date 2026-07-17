"""Reveal — Codex Log Viewer TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Label, TabbedContent

from .models import AgentScope, Scope
from .sources.rollout import SessionCatalog
from .widgets.log_view import LogView
from .widgets.session_tree import SessionTree

CSS = """
Screen {
    layout: vertical;
}

#main-content {
    height: 1fr;
}

#sidebar {
    width: 32;
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
    width: 1fr;
    height: 1fr;
}

TabPane {
    width: 1fr;
    height: 1fr;
}

RichLog {
    width: 1fr;
    height: 1fr;
    scrollbar-size: 1 1;
}

#response-feed {
    height: 1fr;
}

#feed-scroll {
    height: 1fr;
}

.follow-indicator {
    height: 1;
    background: $warning;
    color: $text;
    padding: 0 1;
}

.response-card {
    border: solid $primary;
    margin: 0 0 1 0;
    padding: 0 1;
    height: auto;
}

.response-card.status-active {
    border: solid $warning;
}

.response-card.status-completed {
    border: solid $success;
}

.response-card.status-failed,
.response-card.status-incomplete {
    border: solid $error;
}

.card-header {
    height: auto;
    layout: horizontal;
}

.card-body {
    height: auto;
}

.card-title {
    width: 1fr;
    padding: 0 1;
}

.card-collapse-icon {
    width: 2;
}

.response-item {
    border-top: solid $surface-lighten-1;
    padding: 0 0 0 1;
    height: auto;
}

.item-summary {
    padding: 0 1;
    height: auto;
}

.item-body {
    padding: 0 1 1 2;
    height: auto;
}

.item-view-switch {
    height: 1;
}

.item-view-switch Button {
    width: auto;
    min-width: 8;
    height: 1;
}

.item-view-status {
    width: 1fr;
    height: 1;
    padding: 0 1;
    content-align: right middle;
}

.item-semantic,
.item-tool-result,
.item-raw-header,
.item-raw-host,
.item-raw-inline,
.item-range {
    height: auto;
}

.item-raw-virtual {
    height: 18;
    border: solid $surface-lighten-1;
}

#content-screen {
    background: $surface;
    padding: 1;
}

#content-view {
    height: 1fr;
    border: solid $primary;
}

#content-search-bar {
    height: 3;
}

#content-footer {
    height: 1;
    color: $text-muted;
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
        Binding("d", "tab('diagnostics')", "Diagnostics", show=True),
        Binding("v", "toggle_raw", "Toggle Raw", show=False),
        Binding("[", "cards_prev", "Fewer Cards", show=False),
        Binding("]", "cards_next", "More Cards", show=False),
        Binding("end", "follow_end", "Follow End", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.catalog = SessionCatalog()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-content"):
            yield SessionTree(catalog=self.catalog, id="sidebar")
            yield LogView(catalog=self.catalog, id="log-panel")
        with Horizontal(id="status-bar"):
            yield Label("", id="status-left")
            yield Label("", id="status-center")
            yield Label("", id="status-right")

    def on_mount(self) -> None:
        self.set_interval(0.5, self._poll_sse)
        self.set_interval(2.0, self._update_status)
        self._update_status()

    def on_session_tree_scope_selected(self, event: SessionTree.ScopeSelected) -> None:
        """Scope changes filter Live; History only loads for agent nodes."""
        log_view = self.query_one("#log-panel", LogView)
        log_view.apply_scope(event.scope)
        # Do NOT force tab switch
        if isinstance(event.scope, AgentScope):
            meta = self.catalog.get_thread(event.scope.thread_id)
            name = (meta.agent_nickname if meta else None) or event.scope.thread_id[:12]
            self._set_status(f"Scope: agent {name}")
        else:
            self._set_status(f"Scope: {type(event.scope).__name__}")

    def on_session_tree_session_selected(self, event: SessionTree.SessionSelected) -> None:
        # History load is handled via apply_scope for AgentScope; keep handler for safety.
        pass

    def action_refresh(self) -> None:
        tree = self.query_one("#sidebar", SessionTree)
        tree.refresh_sessions()
        log_view = self.query_one("#log-panel", LogView)
        log_view.refresh_attribution()
        self._set_status("Refreshed catalog/attribution")

    def action_toggle_raw(self) -> None:
        self.query_one("#log-panel", LogView).toggle_raw_mode()

    def action_cards_prev(self) -> None:
        self.query_one("#log-panel", LogView).cycle_card_limit(-1)

    def action_cards_next(self) -> None:
        self.query_one("#log-panel", LogView).cycle_card_limit(1)

    def action_follow_end(self) -> None:
        self.query_one("#log-panel", LogView).resume_follow()

    def action_tab(self, tab: str) -> None:
        log_view = self.query_one("#log-panel", LogView)
        tabs = log_view.query_one(TabbedContent)
        tabs.active = tab

    def _poll_sse(self) -> None:
        log_view = self.query_one("#log-panel", LogView)
        log_view.poll_sse()

    def _update_status(self) -> None:
        log_view = self.query_one("#log-panel", LogView)
        self.query_one("#status-center", Label).update(
            f" Live events: {log_view._stream.events_seen}  cards≤{log_view.card_limit}"
        )
        self.query_one("#status-right", Label).update(
            " Ctrl+R Refresh | End Follow | L/H/D tabs | Ctrl+Q Quit"
        )

    def _set_status(self, msg: str) -> None:
        self.query_one("#status-left", Label).update(f" {msg}")
