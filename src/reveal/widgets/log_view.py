"""Log view — tab coordinator for Live feed, History, Diagnostics."""

from __future__ import annotations

from datetime import timezone, timedelta
import os
import time

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import TabbedContent, TabPane, RichLog, Label
from textual.reactive import reactive
from textual.worker import Worker, get_current_worker

from ..diagnostics import ResponseAnalyzer
from ..models import (
    AgentScope,
    RawLogEvent,
    Scope,
    SessionMeta,
    SessionScope,
    UnassignedScope,
    WorkspaceScope,
)
from ..routing import ResponseRouter
from ..sources.log_stream import LogStream
from ..sources.rollout import SessionCatalog, read_rollout
from .response_feed import ResponseFeed
from .content_screen import ContentScreen
from .response_card import ResponseItemWidget
from .virtual_content import VirtualContentSource

CST = timezone(timedelta(hours=8))


def format_history_token_line(last_token_usage: dict) -> str:
    """Format History token_count line with optional cache hit rate."""
    last = last_token_usage or {}
    in_tok = last.get("input_tokens", "?")
    cached = last.get("cached_input_tokens", "?")
    out_tok = last.get("output_tokens", "?")
    parts = [
        f"tokens: in={in_tok}",
        f"cached={cached}",
        f"out={out_tok}",
    ]
    try:
        in_n = int(last.get("input_tokens"))
        cached_n = int(last.get("cached_input_tokens") or 0)
    except (TypeError, ValueError):
        in_n = 0
        cached_n = 0
    if in_n > 0:
        rate_pct = (cached_n / in_n) * 100.0
        parts.append(f"cached_rate={rate_pct:.1f}%")
    return "[dim]" + " ".join(parts) + "[/]"



class LogView(Container):
    """Main log display with Live cards, History, and Diagnostics tabs."""

    active_tab = reactive("live")

    def __init__(self, catalog: SessionCatalog | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.catalog = catalog or SessionCatalog()
        self._stream = LogStream()
        self._router = ResponseRouter()
        self._analyzer = ResponseAnalyzer()
        self._active_session: SessionMeta | None = None
        self._history_event_count = 0
        self._history_file_sig: tuple[int, int] | None = None  # (mtime_ns, size)
        self._scope: Scope = None
        self._raw_mode = False  # retained for status; raw is per-item now
        self._card_limits = (100, 200, 500, 1000)
        self._worker_busy = False
        self._pending_backlog = False

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="live"):
            with TabPane("Live SSE", id="live"):
                yield ResponseFeed(id="response-feed")
            with TabPane("History", id="history"):
                yield RichLog(id="history-log", highlight=True, markup=True, min_width=40)
            with TabPane("Diagnostics", id="diagnostics"):
                yield RichLog(id="diag-log", highlight=True, markup=True, wrap=True, min_width=40)

    def on_mount(self) -> None:
        hist_log = self.query_one("#history-log", RichLog)
        hist_log.write("[bold green]Select an agent from the sidebar[/] to view its history.")
        # Kick off backfill in a worker
        self._worker_busy = True
        self.run_worker(self._worker_poll, exclusive=True, thread=True, name="log-poll")

    def set_catalog(self, catalog: SessionCatalog) -> None:
        self.catalog = catalog

    def apply_scope(self, scope: Scope) -> None:
        """Filter Live feed + load History for agent nodes.

        Live: workspace/session/none = total; agent = that thread; Unassigned = unassigned.
        History: only when an agent node is selected.
        """
        self._scope = scope
        feed = self.query_one("#response-feed", ResponseFeed)
        threads = self.catalog.thread_ids_for_scope(scope)
        # Update agent labels
        for tid, meta in self.catalog.threads.items():
            label = meta.agent_nickname or ("root" if meta.is_root else tid[:8])
            feed.set_agent_label(tid, label)
        feed.set_scope(scope, threads)

        # History: only load when agent selected
        if isinstance(scope, AgentScope):
            meta = self.catalog.get_thread(scope.thread_id)
            if meta:
                self.load_session(meta)
        elif isinstance(scope, (WorkspaceScope, SessionScope, UnassignedScope)):
            hist = self.query_one("#history-log", RichLog)
            hist.clear()
            hist.write("[bold]Select an agent[/] to load History (Live: workspace/session = all, agent/Unassigned = filter).")

    def load_session(self, meta: SessionMeta) -> None:
        self._active_session = meta
        self._history_file_sig = None
        hist_log = self.query_one("#history-log", RichLog)
        hist_log.clear()
        name = meta.agent_nickname or "root"
        role = meta.agent_role or ""
        hist_log.write("[bold cyan]" + "=" * 50 + "[/]")
        hist_log.write(f"[bold]  {name}[/]" + (f" [dim]({role})[/]" if role else ""))
        hist_log.write(
            f"[dim]  {meta.timestamp.strftime('%Y-%m-%d %H:%M:%S')}  thread={meta.thread_id[:16]}...[/]"
        )
        hist_log.write(f"[dim]  {meta.cwd}[/]")
        hist_log.write("[dim]" + "=" * 50 + "[/]\n")
        try:
            events = read_rollout(meta.file_path)
            st = os.stat(meta.file_path)
            self._history_file_sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            hist_log.write("[red]Failed to read session file.[/]")
            self._history_event_count = 0
            return
        for ev in events:
            if ev.get("type") == "event_msg":
                self._write_event_msg(hist_log, ev.get("payload", {}))
            elif ev.get("type") == "response_item":
                self._write_response_item(hist_log, ev.get("payload", {}))
        self._history_event_count = len(events)

    def poll_sse(self) -> None:
        """Called on UI interval — schedule worker if idle."""
        if self._worker_busy:
            self._pending_backlog = True
            return
        self._worker_busy = True
        self.run_worker(self._worker_poll, exclusive=True, thread=True, name="log-poll")

    def _tool_thread_targets(self) -> list[str]:
        """Bound rollout tailing to threads that currently own responses + active session."""
        ids: set[str] = set()
        for state in self._router.responses.values():
            if state.agent_thread_id:
                ids.add(state.agent_thread_id)
            if state.status.value == "active" and state.agent_thread_id:
                ids.add(state.agent_thread_id)
        if self._active_session is not None and self._active_session.thread_id:
            ids.add(self._active_session.thread_id)
        # Cap to avoid pathological catalogs
        return list(ids)[:32]

    def _worker_poll(self) -> None:
        try:
            events = self._stream.poll()
            tool_updates = []
            try:
                targets = self._tool_thread_targets()
                if targets:
                    tool_updates = self.catalog.poll_tool_results(targets)
            except Exception:
                tool_updates = []
            lag = self._stream.sqlite_lag()
            # Deliver to UI thread reliably (StateChanged can miss under tests/bubble=False)
            self.app.call_from_thread(self._apply_poll_result, events, tool_updates, lag)
        except Exception as exc:
            self.app.call_from_thread(self._on_poll_error, str(exc))
        finally:
            # busy cleared on UI after apply; also clear here if call_from_thread fails
            pass

    def _on_poll_error(self, message: str) -> None:
        self._worker_busy = False
        self._pending_backlog = False

    def _apply_poll_result(
        self,
        events: list[RawLogEvent],
        tools: list,
        lag: int | None,
    ) -> None:
        self._worker_busy = False
        if events:
            snap = self._router.feed_many(events)
            for ev in events:
                if ev.target.endswith("sse::responses") or ev.event_type.startswith("response."):
                    try:
                        self._analyzer.feed(ev.to_sse_event())
                    except Exception:
                        pass
            feed = self.query_one("#response-feed", ResponseFeed)
            for rid in snap.changed_response_ids:
                state = self._router.get(rid)
                if state:
                    feed.upsert_response(state)
            for rid, _old, _new in snap.reassigned:
                state = self._router.get(rid)
                if state:
                    feed.reassign(rid, state)

        for tr in tools:
            rid = self._router.attach_tool_result(tr.call_id, tr)
            if rid:
                state = self._router.get(rid)
                if state:
                    self.query_one("#response-feed", ResponseFeed).upsert_response(state)

        if events:
            self._refresh_diagnostics(lag=lag)
            if len(events) >= self._stream.batch_size:
                self.call_later(self.poll_sse)

        self._poll_history()
        if self._pending_backlog:
            self._pending_backlog = False
            self.call_later(self.poll_sse)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        # Fallback if a worker exits without call_from_thread delivery.
        from textual.worker import WorkerState

        if event.worker.name != "log-poll":
            return
        if event.state in (WorkerState.ERROR, WorkerState.CANCELLED):
            self._worker_busy = False

    def refresh_attribution(self) -> None:
        """Ctrl+R path: refresh catalog-driven labels and re-apply scope without clearing cards."""
        feed = self.query_one("#response-feed", ResponseFeed)
        for tid, meta in self.catalog.threads.items():
            label = meta.agent_nickname or ("root" if meta.is_root else tid[:8])
            feed.set_agent_label(tid, label)
        feed.set_scope(self._scope, self.catalog.thread_ids_for_scope(self._scope))

    def cycle_card_limit(self, direction: int) -> None:
        self.query_one("#response-feed", ResponseFeed).cycle_card_limit(direction)

    def toggle_raw_mode(self) -> None:
        # Per-item raw expansion replaces global raw mode; keep no-op toggle for binding.
        self._raw_mode = not self._raw_mode

    @property
    def card_limit(self) -> int:
        return self.query_one("#response-feed", ResponseFeed).card_limit

    @property
    def raw_mode(self) -> bool:
        return self._raw_mode

    def resume_follow(self) -> None:
        self.query_one("#response-feed", ResponseFeed).resume_follow()

    def on_response_item_widget_open_full(self, event: ResponseItemWidget.OpenFull) -> None:
        self.app.push_screen(ContentScreen(event.source, title=event.title))

    def _poll_history(self) -> None:
        if self._active_session is None:
            return
        path = self._active_session.file_path
        try:
            st = os.stat(path)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return
        # Cheap skip: unchanged rollout must not re-parse multi-MB JSONL on UI thread.
        if self._history_file_sig == sig:
            return
        try:
            events = read_rollout(path)
        except OSError:
            return
        self._history_file_sig = sig
        if len(events) <= self._history_event_count:
            return
        hist_log = self.query_one("#history-log", RichLog)
        for ev in events[self._history_event_count :]:
            if ev.get("type") == "event_msg":
                self._write_event_msg(hist_log, ev.get("payload", {}))
            elif ev.get("type") == "response_item":
                self._write_response_item(hist_log, ev.get("payload", {}))
        self._history_event_count = len(events)

    def _refresh_diagnostics(self, lag: int | None = None) -> None:
        try:
            diag_log = self.query_one("#diag-log", RichLog)
        except Exception:
            return
        a = self._analyzer
        m = self._router.metrics()
        if lag is not None:
            m.sqlite_lag = lag
        diag_log.clear()
        ws_pct = a.overall_whitespace_ratio * 100
        ws_color = "red" if ws_pct > 25 else ("yellow" if ws_pct > 10 else "green")
        diag_log.write(
            f"[bold]Overall WS%:[/] [{ws_color}]{ws_pct:.1f}%[/]  "
            f"[bold]Suspicious:[/] [bold red]{a.total_suspicious}[/]  "
            f"[bold]Active:[/] {a.active_count}  [bold]Completed:[/] {len(a.completed)}"
        )
        diag_log.write(
            f"[bold]Routing:[/] confirmed={m.confirmed} inferred={m.inferred} "
            f"unassigned={m.unassigned} unresolved={m.unresolved_queue} "
            f"conflicts={m.conflicts} partial={m.partial} "
            f"lag={m.sqlite_lag if m.sqlite_lag is not None else '?'}"
        )
        suspicious = a.recent_suspicious(20)
        if suspicious:
            diag_log.write("\n[bold underline]Recent Suspicious Responses:[/]")
            for rs in reversed(suspicious):
                diag_log.write(
                    f"  {rs.model:<15} {rs.reasoning_effort or '-':<7} "
                    f"Sc={rs.suspicion_score:>3} WS={rs.whitespace_ratio:.0%} "
                    f"Tokx={rs.token_inflation:.1f} TPS={rs.effective_tps:.0f} "
                    f"{' '.join(rs.flags[:4])}"
                )
        if a.responses:
            diag_log.write("\n[bold underline]In-Flight:[/]")
            for rid, rs in a.responses.items():
                if rs.delta_count:
                    diag_log.write(
                        f"  {rs.model[:12]:<12} {rs.delta_count:>4}d "
                        f"WS={rs.whitespace_ratio:.0%} avg={rs.avg_delta_size:.0f} rid={rid[:16]}"
                    )

    def _write_event_msg(self, log: RichLog, payload: dict) -> None:
        ptype = payload.get("type", "")
        if ptype == "agent_message":
            log.write(f"[bold green]agent:[/] {payload.get('message', '')}")
        elif ptype == "user_message":
            log.write(f"[bold cyan]user:[/] {payload.get('message', '')[:200]}")
        elif ptype == "token_count":
            last = (payload.get("info") or {}).get("last_token_usage") or {}
            log.write(format_history_token_line(last))
        elif ptype == "task_complete":
            log.write(f"[dim]task completed in {payload.get('duration_ms', 0)}ms[/]")

    def _write_response_item(self, log: RichLog, payload: dict) -> None:
        if payload.get("role") == "assistant":
            for content in payload.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    text = content["text"].replace("[", "\\[")
                    log.write(text)
