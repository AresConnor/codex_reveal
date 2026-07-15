"""Log view widget — main panel with tabs for Live SSE and History."""

from datetime import datetime, timezone, timedelta

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import (
    TabbedContent, TabPane, RichLog, Static, Label,
)
from textual.reactive import reactive

from ..sources.sse import SSESource, SSEEvent
from ..sources.rollout import read_rollout
from ..models import SessionMeta
from ..diagnostics import ResponseAnalyzer
CST = timezone(timedelta(hours=8))



def _safe_write(log: RichLog, text: str):
    """Write text to RichLog — unescape JSON sequences, protect from Rich markup parsing."""
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
                yield RichLog(
                    id="sse-log",
                    highlight=True,
                    markup=True,
                    wrap=True,
                    min_width=40,
                )
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

        sse_log = self.query_one("#sse-log", RichLog)
        sse_log.write("[bold green]Reveal[/] — waiting for SSE events...")
        sse_log.write(f"[dim]Database: {self._sse.db_path}[/]")
        sse_log.write(f"[dim]Existing events: {self._sse.count_total()}[/]")

        hist_log = self.query_one("#history-log", RichLog)
        hist_log.write("[bold green]Select a session from the sidebar[/] to view its history.")

        diag_log = self.query_one("#diag-log", RichLog)
        diag_log.write("[bold green]Diagnostics[/] — response integrity + speed analysis")
        diag_log.write("[dim]Detects: WS padding, token inflation, invisible chars, burst dumping, TPS anomalies[/]")

    def load_session(self, meta: SessionMeta):
        """Load a session's rollout history into the History tab."""
        self._active_session = meta
        hist_log = self.query_one("#history-log", RichLog)
        hist_log.clear()

        ts = meta.timestamp.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
        name = meta.agent_nickname or "root"
        role = meta.agent_role or meta.thread_source

        hist_log.write(f"[bold cyan]{'─' * 70}[/]")
        hist_log.write(f"[bold white]{name}[/] [dim]{role}[/]  [dim]{ts}[/]")
        hist_log.write(f"[dim]cwd: {meta.cwd}[/]")
        hist_log.write(f"[bold cyan]{'─' * 70}[/]")

        events = read_rollout(meta.file_path)
        for ev in events:
            etype = ev.get("type", "")
            payload = ev.get("payload", {})

            if etype == "event_msg":
                self._write_event_msg(hist_log, payload)
            elif etype == "response_item":
                self._write_response_item(hist_log, payload)
            elif etype == "turn_context":
                pass  # skip verbose context
            elif etype == "world_state":
                pass  # skip world state
            # session_meta already handled

        self._history_event_count = len(events)
        hist_log.write(f"\n[dim]{'─' * 40}[/]")
        hist_log.write(f"[dim]End of session ({self._history_event_count} events)[/]")

    def poll_sse(self):
        """Poll for new SSE events and update all views."""
        sse_log = self.query_one("#sse-log", RichLog)
        events = self._sse.poll()

        for ev in events:
            self._write_sse_event(sse_log, ev)
            self._analyzer.feed(ev)

        if events:
            self._refresh_diagnostics()

        # ── Live history re-read ──────────────────────────────────────
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

        # Append only new events
        hist_log = self.query_one("#history-log", RichLog)
        for ev in events[self._history_event_count:]:
            etype = ev.get("type", "")
            payload = ev.get("payload", {})
            if etype == "event_msg":
                self._write_event_msg(hist_log, payload)
            elif etype == "response_item":
                self._write_response_item(hist_log, payload)

        self._history_event_count = new_count

    def _refresh_diagnostics(self):
        """Update the Diagnostics tab with current analyzer state."""
        diag_log = self.query_one("#diag-log", RichLog)
        a = self._analyzer

        # Only redraw if there's something meaningful
        if a.active_count == 0 and not a.completed:
            return

        diag_log.clear()

        # ── Summary bar ──────────────────────────────────────────────
        ws_pct = a.overall_whitespace_ratio * 100
        ws_color = "red" if ws_pct > 25 else ("yellow" if ws_pct > 10 else "green")

        diag_log.write(
            f"[bold]Overall WS%:[/] [{ws_color}]{ws_pct:.1f}%[/]  "
            f"[bold]Suspicious:[/] [bold red]{a.total_suspicious}[/]  "
            f"[bold]Active:[/] {a.active_count}  "
            f"[bold]Completed:[/] {len(a.completed)}"
        )
        diag_log.write("")

        # ── Recent suspicious responses ──────────────────────────────
        suspicious = a.recent_suspicious(20)
        if suspicious:
            diag_log.write("[bold underline]Recent Suspicious Responses:[/]")
            diag_log.write(
                f"  {'Model':<12} {'Sc':>3} {'WS%':>5} {'Tok×':>5} "
                f"{'TPS':>6} {'δ/s':>6} {'Flags'}"
            )
            for rs in reversed(suspicious):
                score = rs.suspicion_score
                sc_color = "red" if score >= 50 else ("yellow" if score >= 25 else "dim")
                flags_str = " ".join(rs.flags[:4])
                diag_log.write(
                    f"  {rs.model:<12} [{sc_color}]{score:>2}[/]  "
                    f"{rs.whitespace_ratio:>4.0%}  "
                    f"{rs.token_inflation:>4.1f}  "
                    f"{rs.effective_tps:>5.0f}  "
                    f"{rs.deltas_per_second:>5.0f}  "
                    f"[dim]{flags_str}[/]"
                )
                if score >= 30 and rs.suspicious_samples:
                    for sample in rs.suspicious_samples[:2]:
                        diag_log.write(f"    [red]![/] [dim]{sample}[/]")
            diag_log.write("")

        # ── Active responses snapshot ────────────────────────────────
        if a.responses:
            diag_log.write("[bold underline]In-Flight:[/]")
            for rid, rs in a.responses.items():
                if rs.delta_count == 0:
                    continue
                ws_pct_r = rs.whitespace_ratio * 100
                ws_c = "red" if ws_pct_r > 25 else ("yellow" if ws_pct_r > 10 else "dim")
                diag_log.write(
                    f"  {rs.model:<12} "
                    f"δ={rs.delta_count:<4} chars={rs.total_chars:<6} "
                    f"ws=[{ws_c}]{ws_pct_r:.0f}%[/]"
                    + (f" [bold red]⚠ {rs.high_whitespace_deltas} dirty δ[/]" if rs.high_whitespace_deltas > 0 else "")
                )

        # ── Footer hint ──────────────────────────────────────────────
        diag_log.write("")
        diag_log.write(
            "[dim]Legend: WS%=whitespace  Tok×=token inflation  TPS=tokens/sec  δ/s=deltas/sec[/]"
        )
        diag_log.write(
            "[dim]  FAST=high TPS  BURST=delta bursts  CHUNK=large avg delta  BIGδ=oversized delta[/]"
        )

    def _write_event_msg(self, log: RichLog, payload: dict):
        ptype = payload.get("type", "")
        if ptype == "agent_message":
            msg = payload.get("message", "")
            phase = payload.get("phase", "")
            prefix = {
                "final_answer": "[bold green]✓[/]",
                "commentary": "[bold yellow]💬[/]",
            }.get(phase, "[dim]•[/]")
            log.write(f"{prefix} {msg}")
        elif ptype == "user_message":
            msg = payload.get("message", "")[:200]
            log.write(f"[bold cyan]👤[/] {msg}")
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

    def _write_sse_event(self, log: RichLog, ev: SSEEvent):
        etype = ev.event_type
        parsed = ev.parsed

        if etype == "response.created":
            resp = parsed.get("response", {})
            rid = resp.get("id", "")[:24]
            model = resp.get("model", "?").replace("gpt-", "")
            reasoning = resp.get("reasoning", {}).get("effort", "?")
            log.write(f"\n[bold cyan]{'━' * 60}[/]")
            log.write(f"[bold white]  {model}[/] reasoning=[bold]{reasoning}[/]  [dim]{rid}[/]")

            _safe_write(log, parsed.get("delta", ""))

        elif etype == "response.output_item.added":
            item = parsed.get("item", {})
            itype = item.get("type", "")
            if itype == "custom_tool_call":
                name = item.get("name", "?")
                log.write(f"  [yellow]⚙ {name}[/]")
            elif itype == "function_call":
                name = item.get("name", "?")
                log.write(f"  [yellow]→ {name}[/]")

        elif etype == "response.custom_tool_call_input.delta":
            _safe_write(log, parsed.get("delta", ""))

        elif etype == "response.completed":
            resp = parsed.get("response", {})
            usage = resp.get("usage", {})
            inp = usage.get("input_tokens", 0)
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
            out = usage.get("output_tokens", 0)
            parts = []
            if inp: parts.append(f"in={inp}")
            if cached: parts.append(f"cached={cached}")
            if out: parts.append(f"out={out}")
            tok_str = "  ".join(parts)
            log.write(f"[dim]{'─' * 60}[/]")
            log.write(f"[dim]  ✓ {tok_str}[/]")
