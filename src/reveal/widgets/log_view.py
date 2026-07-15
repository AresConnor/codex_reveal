"""Log view widget - main panel with tabs for Live SSE and History."""

from datetime import datetime, timezone, timedelta

from textual.app import ComposeResult
from textual.containers import Container, Vertical, Horizontal
from textual.widgets import (
    TabbedContent, TabPane, RichLog, Static, Label, OptionList,
)
from textual.widgets.option_list import Option
from textual.reactive import reactive

from ..sources.sse import SSESource, SSEEvent
from ..sources.rollout import read_rollout
from ..models import SessionMeta
from ..diagnostics import ResponseAnalyzer

CST = timezone(timedelta(hours=8))

_MODEL_COLORS: dict[str, str] = {
    "sol": "cyan",
    "luna": "magenta",
    "pro": "green",
    "mini": "blue",
    "nano": "dim",
}


def _model_color(model: str) -> str:
    ml = model.lower()
    for key, color in _MODEL_COLORS.items():
        if key in ml:
            return color
    return "blue"


def _safe_write(log: RichLog, text: str):
    """Write text to RichLog - unescape JSON sequences, protect from Rich markup parsing."""
    text = text.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace("\\\"", "\"")
    # Escape [ to prevent Rich markup parsing of code brackets
    text = text.replace("[", "\\[")
    log.write(text, animate=False)


class LogView(Container):
    """Main log display with Live SSE and History tabs."""

    active_tab = reactive("live")

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="live"):
            with TabPane("Live SSE", id="live"):
                with Horizontal():
                    yield RichLog(
                        id="sse-log",
                        highlight=True,
                        markup=True,
                        wrap=True,
                        min_width=40,
                    )
                    with Vertical(id="agent-sidebar"):
                        yield Label("Active Agents", id="agent-title")
                        yield OptionList(id="agent-filter")
            with TabPane("History", id="history"):
                yield RichLog(
                    id="history-log",
                    highlight=True,
                    markup=True,
                    min_width=40,
                )
            with TabPane("Diagnostics", id="diagnostics"):
                yield RichLog(
                    id="diag-log",
                    highlight=True,
                    markup=True,
                    wrap=True,
                    min_width=40,
                )

    def on_mount(self):
        self._sse = SSESource()
        self._analyzer = ResponseAnalyzer()
        self._active_session: SessionMeta | None = None
        self._history_event_count: int = 0
        # Streaming state for append-mode text/tool display
        self._stream_text: dict[str, str] = {}   # response_id -> text
        self._stream_tool: dict[str, str] = {}   # response_id -> tool input
        self._stream_last_rid: str = ""
        # Buffer SSE events while Live SSE tab is not visible
        self._sse_buffer: list[SSEEvent] = []
        self._sse_active: bool = True
        # Agent tracking: rid -> {model, effort, status, start_ts}
        self._active_agents: dict[str, dict] = {}
        # Ring buffer of displayed lines: (rid, text, shrink)
        self._display_lines: list[tuple[str, str, bool]] = []
        # Current filter: "" = all, otherwise specific rid
        self._filter_rid: str = ""
        # Last agent state for change detection
        self._last_agent_state: dict[str, str] = {}

        sse_log = self.query_one("#sse-log", RichLog)
        sse_log.write("[bold green]Reveal[/] - waiting for SSE events...")
        sse_log.write(f"[dim]Database: {self._sse.db_path}[/]")
        sse_log.write(f"[dim]Existing events: {self._sse.count_total()}[/]")

        hist_log = self.query_one("#history-log", RichLog)
        hist_log.write("[bold green]Select a session from the sidebar[/] to view its history.")

        # Initialize agent filter with "All"
        ol = self.query_one("#agent-filter", OptionList)
        ol.add_option(Option("All Agents", id="all"))

    # ── Tab switching ────────────────────────────────────────────────

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated):
        """Re-render content on tab switch to fix width issues."""
        try:
            tabs = self.query_one(TabbedContent)
        except Exception:
            return
        active = tabs.active
        self._sse_active = (active == "live")
        if active == "diagnostics":
            self.call_after_refresh(self._refresh_diagnostics)
            self.set_timer(0.1, self._refresh_diagnostics)
        elif active == "live":
            def _flush_sse():
                sse_log = self.query_one("#sse-log", RichLog)
                if self._sse_buffer and sse_log.size.width > 1:
                    for ev in self._sse_buffer:
                        self._write_sse_event(sse_log, ev)
                    self._sse_buffer.clear()
                    self._flush_stream(sse_log, final=False)
                    self._update_agent_list()
            self.call_after_refresh(_flush_sse)
            self.set_timer(0.1, _flush_sse)
        else:
            try:
                pane = event.pane
                if pane is not None:
                    log = pane.query_one(RichLog)
                    self.call_after_refresh(lambda: log.refresh(layout=True))
                    self.set_timer(0.1, lambda: log.refresh(layout=True))
            except Exception:
                pass

    # ── Agent filter ─────────────────────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        """Handle agent filter selection."""
        if event.option.id:
            self._filter_rid = "" if event.option.id == "all" else event.option.id
            self._replay_filtered()

    def _update_agent_list(self):
        """Refresh the agent sidebar OptionList."""
        try:
            ol = self.query_one("#agent-filter", OptionList)
        except Exception:
            return

        # Check if agent state changed
        current_state = {rid: info["status"] for rid, info in self._active_agents.items()}
        if current_state == self._last_agent_state and ol.option_count > 0:
            return
        self._last_agent_state = current_state

        # Build options
        options = [Option("All Agents", id="all")]
        # Show last 30 agents (most recent at bottom)
        agents = list(self._active_agents.items())[-30:]
        for rid, info in agents:
            status_icon = "\u25cf" if info["status"] == "active" else "\u25cb"
            model = info.get("model", "?")[:15]
            rid_short = rid[:16]
            options.append(Option(f"{status_icon} {model:<15} {rid_short}", id=rid))

        # Remember current selection
        current_id = self._filter_rid or "all"

        # Rebuild
        ol.clear_options()
        for opt in options:
            ol.add_option(opt)

        # Restore selection
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id == current_id:
                ol.highlighted = i
                break

    def _replay_filtered(self):
        """Clear RichLog and replay filtered entries from ring buffer."""
        try:
            sse_log = self.query_one("#sse-log", RichLog)
        except Exception:
            return
        sse_log.clear()
        for rid, text, shrink in self._display_lines:
            if self._filter_rid and rid != self._filter_rid:
                continue
            sse_log.write(text, shrink=shrink)

    # ── Session loading ──────────────────────────────────────────────

    def load_session(self, meta: SessionMeta):
        """Load a session's rollout history into the History tab."""
        self._active_session = meta
        hist_log = self.query_one("#history-log", RichLog)
        hist_log.clear()

        name = meta.agent_nickname or "root"
        role = meta.agent_role or ""
        ts_str = meta.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        hist_log.write(f"[bold cyan]{'━' * 50}[/]")
        hist_log.write(f"[bold]  {name}[/]" + (f" [dim]({role})[/]" if role else ""))
        hist_log.write(f"[dim]  {ts_str}  session={meta.session_id[:16]}...[/]")
        hist_log.write(f"[dim]  {meta.cwd}[/]")
        hist_log.write(f"[dim]{'━' * 50}[/]\n")

        try:
            events = read_rollout(meta.file_path)
        except OSError:
            hist_log.write("[red]Failed to read session file.[/]")
            return

        for ev in events:
            etype = ev.get("type", "")
            payload = ev.get("payload", {})

            if etype == "event_msg":
                self._write_event_msg(hist_log, payload)
            elif etype == "response_item":
                self._write_response_item(hist_log, payload)
            elif etype == "turn_context":
                pass
            elif etype == "world_state":
                pass

        self._history_event_count = len(events)
        hist_log.write(f"\n[dim]{'─' * 40}[/]")

    # ── SSE polling ──────────────────────────────────────────────────

    def poll_sse(self):
        """Poll for new SSE events and update all views."""
        sse_log = self.query_one("#sse-log", RichLog)
        events = self._sse.poll()

        # Always feed analyzer (diagnostics must track even when tab hidden)
        for ev in events:
            self._analyzer.feed(ev)

        # Buffer events if Live SSE tab is not visible or has no width
        if not self._sse_active or sse_log.size.width <= 1:
            self._sse_buffer.extend(events)
        else:
            # Flush any buffered events first
            if self._sse_buffer:
                for ev in self._sse_buffer:
                    self._write_sse_event(sse_log, ev)
                self._sse_buffer.clear()
            for ev in events:
                self._write_sse_event(sse_log, ev)

        if events:
            self._flush_stream(sse_log, final=False)
            self._refresh_diagnostics()
            self._update_agent_list()

        # Live history re-read
        self._poll_history()

    def _poll_history(self):
        """Re-read the active session's JSONL file and append new events."""
        if self._active_session is None:
            return
        try:
            events = read_rollout(self._active_session.file_path)
        except OSError:
            return

        new_count = len(events)
        if new_count <= self._history_event_count:
            return

        hist_log = self.query_one("#history-log", RichLog)
        for ev in events[self._history_event_count:]:
            etype = ev.get("type", "")
            payload = ev.get("payload", {})
            if etype == "event_msg":
                self._write_event_msg(hist_log, payload)
            elif etype == "response_item":
                self._write_response_item(hist_log, payload)

        self._history_event_count = new_count

    # ── Diagnostics rendering ────────────────────────────────────────

    def _refresh_diagnostics(self):
        """Update the Diagnostics tab with current analyzer state."""
        diag_log = self.query_one("#diag-log", RichLog)
        a = self._analyzer

        if a.active_count == 0 and not a.completed:
            return
        if diag_log.size.width <= 1:
            return

        diag_log.clear()

        # Summary bar
        ws_pct = a.overall_whitespace_ratio * 100
        ws_color = "red" if ws_pct > 25 else ("yellow" if ws_pct > 10 else "green")
        diag_log.write(
            f"[bold]Overall WS%:[/] [{ws_color}]{ws_pct:.1f}%[/]  "
            f"[bold]Suspicious:[/] [bold red]{a.total_suspicious}[/]  "
            f"[bold]Active:[/] {a.active_count}  "
            f"[bold]Completed:[/] {len(a.completed)}"
        )
        diag_log.write("")

        # Recent suspicious responses
        suspicious = a.recent_suspicious(20)
        if suspicious:
            diag_log.write("[bold underline]Recent Suspicious Responses:[/]")
            diag_log.write(
                f"  {'Model':<15} {'Effort':<7} {'Sc':>3}  "
                f"{'WS%':>5}  {'TWS%':>5}  {'Tokx':>5}  "
                f"{'TPS':>6}  {'d/s':>6}  {'Flags'}"
            )
            for rs in reversed(suspicious):
                score = rs.suspicion_score
                flags_str = " ".join(rs.flags[:4])
                effort = (rs.reasoning_effort or "-")[:7]
                diag_log.write(
                    f"  {rs.model:<15} {effort:<7} {score:>3}  "
                    f"{rs.whitespace_ratio:>5.0%}  "
                    f"{rs.tool_ws_ratio:>5.0%}  "
                    f"{rs.token_inflation:>5.1f}  "
                    f"{rs.effective_tps:>6.0f}  "
                    f"{rs.deltas_per_second:>6.0f}  "
                    f"{flags_str}"
                )
                if score >= 30 and rs.suspicious_samples:
                    for sample in rs.suspicious_samples[:2]:
                        diag_log.write(f"    [red]![/] {sample}")
            diag_log.write("")

        # Active responses snapshot
        if a.responses:
            diag_log.write("[bold underline]In-Flight:[/]")
            for rid, rs in a.responses.items():
                if rs.delta_count == 0:
                    continue
                model = rs.model[:12]
                ws_pct = rs.whitespace_ratio * 100
                diag_log.write(
                    f"  {model:<12} {rs.delta_count:>4}d  "
                    f"WS={ws_pct:.0f}%  "
                    f"avg={rs.avg_delta_size:.0f}  "
                    f"rid={rid[:16]}"
                )
            diag_log.write("")

    # ── History rendering helpers ────────────────────────────────────

    def _write_event_msg(self, log: RichLog, payload: dict):
        ptype = payload.get("type", "")
        if ptype == "agent_message":
            msg = payload.get("message", "")
            phase = payload.get("phase", "")
            prefix = {
                "final_answer": "[bold green]\u2713[/]",
                "commentary": "[bold yellow]\U0001f4ac[/]",
            }.get(phase, "[dim]\u2022[/]")
            log.write(f"{prefix} {msg}")
        elif ptype == "user_message":
            msg = payload.get("message", "")[:200]
            log.write(f"[bold cyan]\U0001f464[/] {msg}")
        elif ptype == "token_count":
            info = payload.get("info", {})
            last = info.get("last_token_usage", {})
            log.write(
                f"[dim]  tokens: in={last.get('input_tokens','?')} "
                f"cached={last.get('cached_input_tokens','?')} "
                f"out={last.get('output_tokens','?')}[/]"
            )
        elif ptype == "task_complete":
            dur = payload.get("duration_ms", 0)
            log.write(f"[dim]  task completed in {dur}ms[/]")

    def _write_response_item(self, log: RichLog, payload: dict):
        role = payload.get("role", "")
        content = payload.get("content", [])
        if role == "assistant":
            for c in content:
                if c.get("type") == "output_text":
                    text = c.get("text", "")
                    if text:
                        _safe_write(log, text)

    # ── SSE event rendering ──────────────────────────────────────────

    def _sse_write(self, log: RichLog, rid: str, text: str, shrink: bool = True):
        """Write to RichLog with ring buffer and agent filter support."""
        self._display_lines.append((rid, text, shrink))
        # Trim ring buffer to last 500 lines
        if len(self._display_lines) > 500:
            self._display_lines = self._display_lines[-500:]
        # Skip display if filtered out
        if self._filter_rid and rid != self._filter_rid:
            return
        log.write(text, shrink=shrink)

    def _write_sse_event(self, log: RichLog, ev: SSEEvent):
        etype = ev.event_type
        parsed = ev.parsed
        resp_data = parsed.get("response", {})
        rid = resp_data.get("id", "") or parsed.get("response_id", "")

        if etype == "response.created":
            self._flush_stream(log, final=True)
            rid_short = rid[:20]
            model = resp_data.get("model", "?").replace("gpt-", "")
            reasoning = resp_data.get("reasoning", {}).get("effort", "?")
            self._stream_last_rid = rid
            self._stream_text.pop(rid, None)
            self._stream_tool.pop(rid, None)
            # Track agent
            self._active_agents[rid] = {
                "model": model,
                "effort": reasoning,
                "status": "active",
                "start_ts": ev.timestamp,
            }
            # Color-coded header with border
            c = _model_color(model)
            bar = "\u2501" * 60
            self._sse_write(log, rid, f"[{c}]{bar}[/]")
            self._sse_write(log, rid,
                f"[bold white]  {model}[/] [{c}]\u00b7[/] "
                f"[yellow]effort={reasoning}[/]  [dim]{rid_short}[/]")

        elif etype == "response.output_text.delta":
            delta = parsed.get("delta", "")
            buf = self._stream_text.get(rid, "")
            self._stream_text[rid] = buf + delta

        elif etype == "response.output_text.done":
            pass

        elif etype == "response.output_item.added":
            item = parsed.get("item", {})
            itype = item.get("type", "")
            if itype in ("custom_tool_call", "function_call"):
                self._flush_stream(log, final=True)
                name = item.get("name", "?")
                icon = "\u2699" if itype == "custom_tool_call" else "->"
                self._sse_write(log, rid, f"  [bold yellow]{icon} {name}[/]")

        elif etype == "response.custom_tool_call_input.delta":
            delta = parsed.get("delta", "")
            buf = self._stream_tool.get(rid, "")
            self._stream_tool[rid] = buf + delta

        elif etype == "response.custom_tool_call_input.done":
            pass

        elif etype == "response.completed":
            self._flush_stream(log, final=True)
            usage = resp_data.get("usage", {})
            inp = usage.get("input_tokens", 0)
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
            out = usage.get("output_tokens", 0)
            parts = [f"in={inp}"] if inp else []
            if cached:
                parts.append(f"cached={cached}")
            if out:
                parts.append(f"out={out}")
            tok_str = "  ".join(parts)
            # Duration
            agent_info = self._active_agents.get(rid, {})
            dur = ""
            if "start_ts" in agent_info:
                dur_s = ev.timestamp - agent_info["start_ts"]
                dur = f"  [dim]{dur_s:.1f}s[/]"
            # Mark agent done
            if rid in self._active_agents:
                self._active_agents[rid]["status"] = "done"
            c = _model_color(agent_info.get("model", ""))
            bar = "\u2500" * 60
            self._sse_write(log, rid, f"[{c} dim]{bar}[/]")
            self._sse_write(log, rid, f"[green]  \u2713 {tok_str}[/]{dur}")

    def _flush_stream(self, log: RichLog, final: bool = False):
        """Write accumulated streaming text and tool input to RichLog."""
        # Flush text streams
        for rid, text in list(self._stream_text.items()):
            if text:
                display = text.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace("\\\"", "\"")
                display = display.replace("[", "\\[")
                self._sse_write(log, rid, display, shrink=not final)
            if final:
                self._stream_text.pop(rid, None)
            else:
                self._stream_text[rid] = ""
        # Flush tool input streams
        for rid, text in list(self._stream_tool.items()):
            if text:
                display = text.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace("\\\"", "\"")
                display = display.replace("[", "\\[")
                self._sse_write(log, rid, f"  [dim]{display}[/]", shrink=not final)
            if final:
                self._stream_tool.pop(rid, None)
            else:
                self._stream_tool[rid] = ""
