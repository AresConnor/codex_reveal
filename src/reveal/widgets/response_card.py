"""Real Textual ResponseCard and semantic item widgets."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Label, Static, Button

from ..models import (
    AttributionConfidence,
    ItemKind,
    ResponseItemState,
    ResponseState,
    ResponseStatus,
)
from ..routing import display_item_number
from .virtual_content import (
    EMBEDDED_VIEWPORT_ROWS,
    VirtualContentSource,
    VirtualContentView,
    is_large_content,
)

CST = timezone(timedelta(hours=8))

_MODEL_COLORS = {"sol": "cyan", "luna": "magenta", "pro": "green", "mini": "blue", "nano": "dim"}


def _model_color(model: str) -> str:
    ml = (model or "").lower()
    for key, color in _MODEL_COLORS.items():
        if key in ml:
            return color
    return "blue"


def _one_line(text: str, limit: int = 120) -> str:
    line = " ".join((text or "").split())
    if len(line) > limit:
        return line[: limit - 1] + "…"
    return line


def _local_item_time(item: ResponseItemState) -> str:
    """Return the local time of the first event belonging to an item."""
    if not item.events:
        return "--:--:--.---"
    local = datetime.fromtimestamp(item.events[0].timestamp, tz=CST)
    return local.strftime("%H:%M:%S.%f")[:-3]


class ResponseItemWidget(Vertical):
    """One semantic output item inside a response card."""

    can_focus = True
    expanded: reactive[bool] = reactive(False)
    view_mode: reactive[str] = reactive("text")

    class Toggled(Message):
        def __init__(self, item_id: str, expanded: bool) -> None:
            super().__init__()
            self.item_id = item_id
            self.expanded = expanded

    class OpenFull(Message):
        def __init__(self, source: VirtualContentSource, title: str) -> None:
            super().__init__()
            self.source = source
            self.title = title

    def __init__(self, item: ResponseItemState, response_id: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.item = item
        self.response_id = response_id
        self._user_expanded: bool | None = None

    def compose(self) -> ComposeResult:
        yield Label(self._summary_text(), classes="item-summary")
        with Vertical(classes="item-body"):
            with Horizontal(classes="item-view-switch"):
                yield Button("Text", classes="item-view-text", compact=True)
                yield Button("Raw SSE", classes="item-view-raw", compact=True)
                yield Label("", classes="item-view-status")
            yield Static("", classes="item-semantic")
            yield Static("", classes="item-tool-result")
            yield Static("", classes="item-raw-header")
            yield Vertical(classes="item-raw-host")

    def on_mount(self) -> None:
        self._apply_expanded()
        self._apply_view_mode()
        self.refresh_from_state(self.item)

    def watch_expanded(self, value: bool) -> None:
        self._apply_expanded()
        if value:
            self._render_expanded_content()

    def watch_view_mode(self, value: str) -> None:
        self._apply_view_mode()
        if self.expanded:
            self._render_expanded_content()

    def _apply_expanded(self) -> None:
        try:
            body = self.query_one(".item-body")
        except Exception:
            return
        body.display = self.expanded

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._user_expanded = self.expanded
        self.post_message(self.Toggled(self.item.item_id, self.expanded))

    def set_expanded(self, value: bool, *, user: bool = False) -> None:
        if user:
            self._user_expanded = value
        elif self._user_expanded is not None and not user:
            # Expand All / Collapse All overrides until user toggles again? Plan:
            # card-level Expand All / Collapse All for raw item details.
            pass
        self.expanded = value
        if user:
            self._user_expanded = value

    def on_click(self, event) -> None:
        try:
            summary = self.query_one(".item-summary", Label)
        except Exception:
            return
        if event.widget is summary:
            self.toggle()
            event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "item-view-text" in event.button.classes:
            self.view_mode = "text"
            event.stop()
        elif "item-view-raw" in event.button.classes:
            self.view_mode = "raw"
            event.stop()

    def refresh_from_state(self, item: ResponseItemState) -> None:
        self.item = item
        try:
            self.query_one(".item-summary", Label).update(self._summary_text())
        except Exception:
            return
        self._apply_view_mode()
        if self.expanded:
            self._render_expanded_content()

    def _apply_view_mode(self) -> None:
        if not self.is_mounted:
            return
        is_thinking = self.item.kind == ItemKind.THINKING
        switch = self.query_one(".item-view-switch", Horizontal)
        semantic = self.query_one(".item-semantic", Static)
        raw_header = self.query_one(".item-raw-header", Static)
        raw_host = self.query_one(".item-raw-host", Vertical)
        text_button = self.query_one(".item-view-text", Button)
        raw_button = self.query_one(".item-view-raw", Button)
        status = self.query_one(".item-view-status", Label)

        switch.display = is_thinking
        semantic.display = not is_thinking or self.view_mode == "text"
        raw_header.display = not is_thinking or self.view_mode == "raw"
        raw_host.display = not is_thinking or self.view_mode == "raw"
        text_button.variant = "primary" if self.view_mode == "text" else "default"
        raw_button.variant = "primary" if self.view_mode == "raw" else "default"
        status.update("[dim]done[/]" if self.item.done else "[yellow]streaming[/]")

    def _render_expanded_content(self) -> None:
        """Render only this item's assembled content and verbatim SSE events."""
        if not self.is_mounted:
            return
        semantic = self.query_one(".item-semantic", Static)
        tool = self.query_one(".item-tool-result", Static)
        raw_header = self.query_one(".item-raw-header", Static)
        host = self.query_one(".item-raw-host", Vertical)

        semantic_text = self._semantic_text()
        if self.item.kind == ItemKind.TOOL_CALL:
            semantic.update(semantic_text or "[dim](empty)[/]")
        elif self.item.kind == ItemKind.THINKING:
            semantic.update(self._thinking_renderable(semantic_text))
        else:
            semantic.update(Text(semantic_text) if semantic_text else Text("(empty)", style="dim"))

        if self.item.kind == ItemKind.TOOL_CALL:
            tool.display = True
            tool.update(self._tool_result_text())
        else:
            tool.display = False

        self._apply_view_mode()
        if self.item.kind == ItemKind.THINKING and self.view_mode != "raw":
            return

        raw_rows = self._raw_pairs()
        raw_header.update(f"[dim]Raw SSE ({len(self.item.events)} events)[/]")
        host.remove_children()
        raw_text = self._raw_text(raw_rows)
        if is_large_content(raw_text):
            src = VirtualContentSource.from_text(raw_text)
            view = VirtualContentView(src, max_height=EMBEDDED_VIEWPORT_ROWS, classes="item-raw-virtual")
            host.mount(view)
            first, last, total = 1, min(EMBEDDED_VIEWPORT_ROWS, src.total_display_lines()), src.total_display_lines()
            host.mount(Label(f"[dim]raw lines {first}-{last}/{total} (fullscreen: f)[/]", classes="item-range"))
        else:
            host.mount(Static(raw_text.replace("[", "\\["), classes="item-raw-inline"))

    def _summary_text(self) -> str:
        local_time = _local_item_time(self.item)
        kind = self.item.kind
        if kind == ItemKind.THINKING:
            preview = _one_line(self.item.assembled_text) or f"Thinking  events={len(self.item.events)}"
            return f"{local_time}  Thinking  {preview}"
        if kind == ItemKind.TOOL_CALL:
            name = self.item.tool_name or "?"
            status = "running"
            if self.item.tool_result:
                status = self.item.tool_result.status
            elif self.item.done:
                status = "result unavailable"
            args = _one_line(self.item.tool_input, 80)
            return f"{local_time}  Tool Call: {name}  [{status}]  {args}"
        if kind == ItemKind.ANSWER:
            preview = _one_line(self.item.assembled_text) or "Answer"
            return f"{local_time}  Answer  {preview}"
        return f"{local_time}  Other  {self.item.protocol_type}  events={len(self.item.events)}"

    def _semantic_text(self) -> str:
        if self.item.kind == ItemKind.TOOL_CALL:
            parts = [f"[bold]Tool[/] {self.item.tool_name or '?'}", f"call_id={self.item.call_id or '?'}"]
            if self.item.tool_input:
                parts.append(self.item.tool_input)
            return "\n".join(parts)
        if self.item.kind == ItemKind.THINKING:
            if self.item.summary_parts:
                return "\n".join(
                    self.item.summary_parts[i]
                    for i in sorted(self.item.summary_parts)
                    if self.item.summary_parts[i]
                )
            return self.item.assembled_text or ""
        return self.item.assembled_text or ""

    def _thinking_renderable(self, text: str) -> Text:
        """Render multi-part reasoning summaries clearly; note encrypted body."""
        render = Text()
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            render.append("(empty summary)", style="dim")
        elif len(lines) == 1:
            render.append(lines[0])
        else:
            for i, line in enumerate(lines, start=1):
                if i > 1:
                    render.append("\n")
                render.append(f"{i}. ", style="dim")
                render.append(line)
        if self.item.has_encrypted_content:
            render.append("\n\n")
            render.append(
                "(provider encrypted reasoning body; plaintext summary only)",
                style="dim",
            )
        return render

    def _tool_result_text(self) -> str:
        tr = self.item.tool_result
        if tr is None:
            return "[dim]rollout-derived result: running / unavailable[/]"
        header = (
            f"[bold]rollout-derived result[/] status={tr.status}"
            + (f" duration_ms={tr.duration_ms}" if tr.duration_ms is not None else "")
            + (f" exit={tr.exit_code}" if tr.exit_code is not None else "")
        )
        body = tr.output or tr.summary or ""
        if is_large_content(body):
            return header + f"\n[dim](large output — expand virtual / fullscreen)[/]\n" + body[:500]
        return header + "\n" + body

    def _raw_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for i, ev in enumerate(self.item.events, start=1):
            n = display_item_number(self.item.output_index)
            header = (
                f"{n}.{i}  {ev.event_type}  "
                f"ts={ev.timestamp:.3f} seq={ev.sequence_number} log_id={ev.log_id}"
            )
            pairs.append((header, ev.raw_body))
        return pairs

    def _raw_text(self, pairs: list[tuple[str, str]]) -> str:
        chunks: list[str] = []
        for header, body in pairs:
            chunks.append(header)
            chunks.append(body)
            chunks.append("")
        return "\n".join(chunks)

    def raw_source(self) -> VirtualContentSource:
        return VirtualContentSource.from_text(self._raw_text(self._raw_pairs()))


class ResponseCard(Vertical):
    """One Responses API response as a real bordered card widget."""

    can_focus = True
    collapsed: reactive[bool] = reactive(False)

    class ExpandAll(Message):
        def __init__(self, response_id: str) -> None:
            super().__init__()
            self.response_id = response_id

    class CollapseAll(Message):
        def __init__(self, response_id: str) -> None:
            super().__init__()
            self.response_id = response_id

    def __init__(self, state: ResponseState, *, agent_label: str = "unassigned", **kwargs) -> None:
        super().__init__(classes="response-card", **kwargs)
        self.state = state
        self.agent_label = agent_label
        self._item_widgets: dict[str, ResponseItemWidget] = {}
        self._user_collapsed: bool | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="card-header"):
            yield Label("▾", classes="card-collapse-icon")
            yield Label("", classes="card-title")
            yield Button("Expand All", classes="card-expand-all", compact=True)
            yield Button("Collapse All", classes="card-collapse-all", compact=True)
        yield Vertical(classes="card-body")

    def on_mount(self) -> None:
        self.refresh_from_state(self.state, agent_label=self.agent_label)

    def watch_collapsed(self, value: bool) -> None:
        try:
            body = self.query_one(".card-body")
            icon = self.query_one(".card-collapse-icon", Label)
        except Exception:
            return
        body.display = not value
        icon.update("▸" if value else "▾")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "card-expand-all" in event.button.classes:
            for w in self._item_widgets.values():
                w.set_expanded(True)
            event.stop()
        elif "card-collapse-all" in event.button.classes:
            for w in self._item_widgets.values():
                w.set_expanded(False)
            event.stop()

    def on_click(self, event) -> None:
        # Toggle collapse when header icon/title clicked — ignore buttons
        pass

    def toggle_collapsed(self) -> None:
        self.collapsed = not self.collapsed
        self._user_collapsed = self.collapsed

    def refresh_from_state(self, state: ResponseState, *, agent_label: str | None = None) -> None:
        self.state = state
        if agent_label is not None:
            self.agent_label = agent_label
        try:
            title = self.query_one(".card-title", Label)
            body = self.query_one(".card-body", Vertical)
        except Exception:
            return

        title.update(self._header_text())
        self._set_status_class(state.status)

        # Reconcile item widgets in protocol order
        desired_ids = [item.item_id for item in state.item_list]
        # Remove gone
        for iid in list(self._item_widgets):
            if iid not in desired_ids:
                w = self._item_widgets.pop(iid)
                w.remove()

        for item in state.item_list:
            w = self._item_widgets.get(item.item_id)
            if w is None:
                w = ResponseItemWidget(item, state.response_id, classes="response-item")
                self._item_widgets[item.item_id] = w
                body.mount(w)
            else:
                w.refresh_from_state(item)

        # Other events section
        other_id = "__other__"
        if state.other_events:
            # Represent as a synthetic summary static at end
            existing = body.query(f"#other-{state.response_id}")
            text = self._other_text(state)
            if existing:
                existing.first().update(text)
            else:
                body.mount(Static(text, id=f"other-{state.response_id}", classes="card-other"))

    def _header_text(self) -> str:
        s = self.state
        color = _model_color(s.model)
        status = s.status.value
        status_color = {
            ResponseStatus.ACTIVE: "yellow",
            ResponseStatus.COMPLETED: "green",
            ResponseStatus.FAILED: "red",
            ResponseStatus.INCOMPLETE: "red",
        }.get(s.status, "white")
        duration = ""
        if s.started_at:
            end = s.ended_at
            import time

            duration = f"{(end or time.time()) - s.started_at:.1f}s"
        usage = s.usage or {}
        token_text = f"in={usage.get('input_tokens', '?')} out={usage.get('output_tokens', '?')}"
        cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
        if cached:
            token_text += f" cached={cached}"
        badges = []
        if s.attribution.confidence == AttributionConfidence.INFERRED:
            badges.append("[yellow]inferred[/]")
        elif s.attribution.confidence == AttributionConfidence.UNASSIGNED:
            badges.append("[dim]unassigned[/]")
        if s.partial:
            badges.append("[dim]partial[/]")
        badge = " ".join(badges)
        short_id = s.response_id[:20]
        return (
            f"[bold {_model_color(s.model)}]{s.model}[/]  "
            f"[{status_color}]{status}[/]  "
            f"{self.agent_label}  "
            f"effort={s.reasoning_effort or '?'}  {duration}  {token_text}  "
            f"items={len(s.items)}  {badge}  [dim]{short_id}[/]"
        )

    def _set_status_class(self, status: ResponseStatus) -> None:
        self.remove_class("status-active", "status-completed", "status-failed", "status-incomplete")
        self.add_class(f"status-{status.value}")

    def _other_text(self, state: ResponseState) -> str:
        lines = ["[dim]Other (response-level)[/]"]
        for i, ev in enumerate(state.other_events, start=1):
            lines.append(f"  O.{i}  {ev.event_type}  log_id={ev.log_id}")
        return "\n".join(lines)
