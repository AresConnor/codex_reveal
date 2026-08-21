"""Incremental response card feed with scope filtering and follow-bottom."""

from __future__ import annotations

from collections import OrderedDict

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Label, Static

from ..models import (
    AttributionConfidence,
    ResponseState,
    ResponseStatus,
    Scope,
    UnassignedScope,
    AgentScope,
    SessionScope,
    WorkspaceScope,
)
from .response_card import ResponseCard


class ResponseFeed(Vertical):
    """Owns ordered response cards, filtering, retention, follow state."""

    follow_bottom: reactive[bool] = reactive(True)

    class FollowPaused(Message):
        def __init__(self, new_events: int, updated: int) -> None:
            super().__init__()
            self.new_events = new_events
            self.updated = updated

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._order: list[str] = []
        self._states: OrderedDict[str, ResponseState] = OrderedDict()
        self._cards: dict[str, ResponseCard] = {}
        self._view_state: dict[str, dict] = {}  # response_id -> {collapsed, items: {id: expanded}}
        self._scope: Scope = None
        self._scope_threads: set[str] | None = None  # None = all
        self._scope_unassigned_only = False
        self._agent_labels: dict[str, str] = {}
        self._card_limits = (100, 200, 500, 1000)
        self._card_limit_index = 0
        self._paused_new = 0
        self._paused_updated = 0
        self._was_at_bottom = True

    def compose(self) -> ComposeResult:
        yield Label("", id="follow-indicator", classes="follow-indicator")
        yield VerticalScroll(id="feed-scroll")

    def on_mount(self) -> None:
        ind = self.query_one("#follow-indicator", Label)
        ind.display = False

    @property
    def card_limit(self) -> int:
        return self._card_limits[self._card_limit_index]

    def cycle_card_limit(self, direction: int) -> None:
        self._card_limit_index = max(
            0, min(len(self._card_limits) - 1, self._card_limit_index + direction)
        )
        self._trim()

    def set_agent_label(self, thread_id: str, label: str) -> None:
        self._agent_labels[thread_id] = label

    def set_scope(self, scope: Scope, thread_ids: set[str] | None) -> None:
        """Configure Live filter.

        - None / Workspace / Session → total feed (all cards + unassigned)
        - AgentScope → only that agent thread
        - UnassignedScope → only unassigned / no-thread cards
        """
        self._scope = scope
        if isinstance(scope, UnassignedScope):
            self._scope_unassigned_only = True
            self._scope_threads = set()
        elif isinstance(scope, AgentScope):
            self._scope_unassigned_only = False
            self._scope_threads = set(thread_ids or ())
        else:
            # Workspace/Session/None: aggregate Live view
            self._scope_unassigned_only = False
            self._scope_threads = None
        self._remount_filter()

    def upsert_response(self, state: ResponseState) -> None:
        rid = state.response_id
        is_new = rid not in self._states
        self._states[rid] = state
        if is_new:
            self._order.append(rid)
            # keep order by order_key
            self._order.sort(key=lambda x: self._states[x].order_key)
        self._trim()

        if not self._visible(state):
            self._hide_card(rid)
            if not self.follow_bottom:
                if is_new:
                    self._paused_new += 1
                else:
                    self._paused_updated += 1
                self._update_indicator()
            return

        scroll = self.query_one("#feed-scroll", VerticalScroll)
        at_bottom = self._is_at_bottom(scroll)
        if not at_bottom and self.follow_bottom:
            # user scrolled up
            self.follow_bottom = False

        card = self._cards.get(rid)
        label = self._label_for(state)
        if card is None:
            card = ResponseCard(state, agent_label=label)
            self._cards[rid] = card
            self._mount_card_ordered(scroll, card, rid)
            self._restore_view_state(card)
        elif card.parent is not scroll:
            # Re-attach the same widget instance after filter hide/show.
            if card.is_mounted:
                try:
                    card.remove()
                except Exception:
                    pass
            self._mount_card_ordered(scroll, card, rid)
            card.refresh_from_state(state, agent_label=label)
        else:
            card.refresh_from_state(state, agent_label=label)

        if self.follow_bottom or at_bottom:
            self.follow_bottom = True
            self._paused_new = 0
            self._paused_updated = 0
            self._update_indicator()
            scroll.scroll_end(animate=False)
        else:
            if is_new:
                self._paused_new += 1
            else:
                self._paused_updated += 1
            self._update_indicator()

    def reassign(self, response_id: str, state: ResponseState) -> None:
        """Attribution changed; update filter membership without losing view state."""
        self.upsert_response(state)

    def _hide_card(self, rid: str) -> None:
        """Remove card from feed dict and DOM if present."""
        card = self._cards.pop(rid, None)
        if card is not None:
            self._capture_view_state(card)
            if card.is_mounted:
                try:
                    card.remove()
                except Exception:
                    pass

    def _mount_card_ordered(self, scroll: VerticalScroll, card: ResponseCard, rid: str) -> None:
        # Find previous visible card to mount after
        idx = self._order.index(rid)
        prev_widget = None
        for prev_id in reversed(self._order[:idx]):
            prev = self._cards.get(prev_id)
            if prev is not None and prev.is_mounted and prev.parent is scroll:
                prev_widget = prev
                break
        if prev_widget is None:
            scroll.mount(card)
        else:
            scroll.mount(card, after=prev_widget)


    def _visible(self, state: ResponseState) -> bool:
        if self._scope_unassigned_only:
            return (
                state.attribution.confidence == AttributionConfidence.UNASSIGNED
                or not state.agent_thread_id
            )
        if self._scope_threads is None:
            # Total Live view: every card, including unassigned.
            return True
        if not state.agent_thread_id:
            # Agent-scoped filter excludes unassigned.
            return False
        return state.agent_thread_id in self._scope_threads

    def _label_for(self, state: ResponseState) -> str:
        if not state.agent_thread_id:
            return "unassigned"
        return self._agent_labels.get(state.agent_thread_id, state.agent_thread_id[:12])

    def _capture_view_state(self, card: ResponseCard) -> None:
        rid = card.state.response_id
        items = {iid: w.expanded for iid, w in card._item_widgets.items()}
        self._view_state[rid] = {"collapsed": card.collapsed, "items": items}

    def _restore_view_state(self, card: ResponseCard) -> None:
        rid = card.state.response_id
        vs = self._view_state.get(rid)
        if not vs:
            return
        card.collapsed = bool(vs.get("collapsed"))
        for iid, expanded in (vs.get("items") or {}).items():
            w = card._item_widgets.get(iid)
            if w is not None:
                w.expanded = bool(expanded)

    def _trim(self) -> None:
        limit = self.card_limit
        while len(self._order) > limit:
            removable = None
            for rid in self._order:
                st = self._states.get(rid)
                if st and st.status != ResponseStatus.ACTIVE:
                    removable = rid
                    break
            if removable is None:
                removable = self._order[0]
            self._order.remove(removable)
            self._states.pop(removable, None)
            self._view_state.pop(removable, None)
            self._hide_card(removable)

    def _remount_filter(self) -> None:
        """Show/hide existing cards for the current scope — never mass-recreate IDs."""
        if not self.is_mounted:
            return
        try:
            scroll = self.query_one("#feed-scroll", VerticalScroll)
        except Exception:
            return

        for rid in list(self._order):
            st = self._states.get(rid)
            want = st is not None and self._visible(st)
            card = self._cards.get(rid)

            if want:
                label = self._label_for(st)
                if card is None:
                    card = ResponseCard(st, agent_label=label)
                    self._cards[rid] = card
                    self._mount_card_ordered(scroll, card, rid)
                    self._restore_view_state(card)
                elif card.parent is not scroll:
                    if card.is_mounted:
                        try:
                            card.remove()
                        except Exception:
                            pass
                    self._mount_card_ordered(scroll, card, rid)
                    card.refresh_from_state(st, agent_label=label)
                else:
                    card.refresh_from_state(st, agent_label=label)
            elif card is not None:
                self._hide_card(rid)

        if self.follow_bottom:
            scroll.scroll_end(animate=False)


    def _is_at_bottom(self, scroll: VerticalScroll) -> bool:
        try:
            # max_scroll_y == scroll.scroll_y when at bottom
            return scroll.scroll_y >= max(0, scroll.max_scroll_y - 1)
        except Exception:
            return True

    def on_scroll(self, event) -> None:
        # Textual may not fire this on VerticalScroll children consistently;
        # also check on mouse wheel via message bubble.
        pass

    def check_scroll_follow(self) -> None:
        try:
            scroll = self.query_one("#feed-scroll", VerticalScroll)
        except Exception:
            return
        if self._is_at_bottom(scroll):
            if not self.follow_bottom:
                self.resume_follow()
        else:
            if self.follow_bottom:
                self.follow_bottom = False
                self._update_indicator()

    def resume_follow(self) -> None:
        self.follow_bottom = True
        self._paused_new = 0
        self._paused_updated = 0
        self._update_indicator()
        try:
            scroll = self.query_one("#feed-scroll", VerticalScroll)
            scroll.scroll_end(animate=False)
        except Exception:
            pass

    def _update_indicator(self) -> None:
        ind = self.query_one("#follow-indicator", Label)
        if self.follow_bottom or (self._paused_new == 0 and self._paused_updated == 0):
            ind.display = False
            ind.update("")
            return
        ind.display = True
        ind.update(
            f"[bold yellow]↓ {self._paused_new} new / {self._paused_updated} updated — End or click to follow[/]"
        )

    def on_click(self, event) -> None:
        try:
            ind = self.query_one("#follow-indicator", Label)
        except Exception:
            return
        if event.widget is ind or (hasattr(event.widget, "id") and event.widget.id == "follow-indicator"):
            self.resume_follow()
            event.stop()
