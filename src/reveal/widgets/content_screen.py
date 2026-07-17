"""Full-screen virtual content viewer with search."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from .virtual_content import VirtualContentSource, VirtualContentView


class ContentScreen(ModalScreen[None]):
    """Modal full-screen viewer sharing a VirtualContentSource."""

    BINDINGS = [
        Binding("escape", "close_or_search", "Close", show=True),
        Binding("slash", "open_search", "Search", show=True),
        Binding("n", "next_match", "Next", show=False),
        Binding("N", "prev_match", "Prev", show=False),
        Binding("r", "toggle_regex", "Regex", show=False),
    ]

    def __init__(self, source: VirtualContentSource, *, y: int = 0, title: str = "Content") -> None:
        super().__init__()
        self._source = source
        self._initial_y = y
        self._title = title
        self._regex = False
        self._search_open = False

    def compose(self) -> ComposeResult:
        with Vertical(id="content-screen"):
            yield Label(self._title, id="content-title")
            yield VirtualContentView(self._source, max_height=0, id="content-view")
            with Horizontal(id="content-search-bar"):
                yield Label("/", id="content-search-prefix")
                yield Input(placeholder="search (n/N next/prev, r regex)", id="content-search")
                yield Label("", id="content-search-status")
            yield Static("", id="content-footer")

    def on_mount(self) -> None:
        view = self.query_one("#content-view", VirtualContentView)
        view.set_source(self._source)
        view.scroll_to(y=self._initial_y, animate=False)
        self.query_one("#content-search-bar").display = False
        self._update_footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "content-search":
            return
        view = self.query_one("#content-view", VirtualContentView)
        count, err = view.set_search(event.value, regex=self._regex)
        status = self.query_one("#content-search-status", Label)
        if err:
            status.update(f"[red]{err}[/]")
        else:
            status.update(f"{view.current_match}/{count}" if count else "0 matches")
        self._update_footer()

    def action_open_search(self) -> None:
        bar = self.query_one("#content-search-bar")
        bar.display = True
        self._search_open = True
        self.query_one("#content-search", Input).focus()

    def action_close_or_search(self) -> None:
        if self._search_open:
            bar = self.query_one("#content-search-bar")
            bar.display = False
            self._search_open = False
            view = self.query_one("#content-view", VirtualContentView)
            view.set_search("")
            self.query_one("#content-search-status", Label).update("")
            view.focus()
            return
        self.dismiss(None)

    def action_next_match(self) -> None:
        self.query_one("#content-view", VirtualContentView).next_match()
        self._sync_match_status()

    def action_prev_match(self) -> None:
        self.query_one("#content-view", VirtualContentView).prev_match()
        self._sync_match_status()

    def action_toggle_regex(self) -> None:
        self._regex = not self._regex
        inp = self.query_one("#content-search", Input)
        if inp.value:
            view = self.query_one("#content-view", VirtualContentView)
            count, err = view.set_search(inp.value, regex=self._regex)
            status = self.query_one("#content-search-status", Label)
            mode = "re" if self._regex else "text"
            if err:
                status.update(f"[red]{err}[/] ({mode})")
            else:
                status.update(f"{view.current_match}/{count} ({mode})")

    def _sync_match_status(self) -> None:
        view = self.query_one("#content-view", VirtualContentView)
        status = self.query_one("#content-search-status", Label)
        if view.match_count:
            status.update(f"{view.current_match}/{view.match_count}")
        self._update_footer()

    def _update_footer(self) -> None:
        view = self.query_one("#content-view", VirtualContentView)
        first, last, total = view.visible_range()
        self.query_one("#content-footer", Static).update(
            f" lines {first}-{last}/{total}  |  / search  Esc close"
        )
