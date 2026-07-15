"""Response diagnostics — detect token inflation, whitespace padding, and anomalies."""

import re
from dataclasses import dataclass, field
from collections import defaultdict

from .models import SSEEvent

# ── Patterns ────────────────────────────────────────────────────────────────

# Characters that contribute zero semantic value but inflate token count
REPEATED_CHAR_RE = re.compile(r"([^\s])\1{20,}")  # 20+ repeats of non-whitespace char
WHITESPACE_RE = re.compile(r"\s")
INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\u00a0\u00ad\u180e\ufeff]")

# Suspicious: delta is mostly whitespace
WHITESPACE_DELTA_THRESHOLD = 0.8  # 80%+ whitespace → flag

# Suspicious: reported tokens >> estimated tokens
# Rough heuristic: ~4 chars per token for English text
TOKEN_INFLATION_RATIO = 5.0  # reported/estimated > 5x → suspicious

# Suspicious: delta much larger than typical streaming chunk
LARGE_DELTA_THRESHOLD = 200  # chars — normal deltas are 1-20 chars


@dataclass
class ResponseStats:
    """Per-response metrics collected from SSE events."""
    response_id: str = ""
    model: str = ""
    reasoning_effort: str = ""

    # Content metrics
    total_chars: int = 0
    content_chars: int = 0         # non-whitespace chars
    whitespace_chars: int = 0
    invisible_chars: int = 0
    delta_count: int = 0
    text_delta_count: int = 0
    tool_input_delta_count: int = 0

    # Token metrics (from response.completed)
    reported_output_tokens: int = 0
    reported_input_tokens: int = 0
    reported_reasoning_tokens: int = 0

    # Anomaly flags
    high_whitespace_deltas: int = 0       # deltas with >80% whitespace
    all_whitespace_deltas: int = 0        # deltas that are 100% whitespace
    large_deltas: int = 0                 # deltas >200 chars
    repeated_char_deltas: int = 0         # deltas with 20+ repeated chars
    invisible_char_deltas: int = 0        # deltas with invisible unicode
    # Timing / speed metrics
    created_at: float = 0.0              # Unix timestamp (seconds with nanos)
    completed_at: float = 0.0
    delta_timestamps: list[float] = field(default_factory=list)  # per-delta arrival times
    burst_count: int = 0                 # groups of 3+ deltas within 50ms
    max_delta_gap_ms: float = 0.0        # largest inter-delta gap
    avg_delta_size: float = 0.0          # average chars per text delta
    # Samples of suspicious deltas (for inspection)
    suspicious_samples: list[str] = field(default_factory=list)
    max_delta_size: int = 0
    max_delta_preview: str = ""

    status: str = "in_progress"

    @property
    def whitespace_ratio(self) -> float:
        if self.total_chars == 0:
            return 0.0
        return self.whitespace_chars / self.total_chars

    @property
    def estimated_tokens(self) -> int:
        """Rough token estimate: ~4 chars per token."""
        return max(1, self.content_chars // 4)

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock time from created to completed."""
        if self.created_at and self.completed_at:
            return self.completed_at - self.created_at
        return 0.0

    @property
    def effective_tps(self) -> float:
        """Tokens per second based on wall time."""
        if self.elapsed_seconds > 0:
            return self.reported_output_tokens / self.elapsed_seconds
        return 0.0

    @property
    def deltas_per_second(self) -> float:
        """Text deltas per second."""
        if self.elapsed_seconds > 0 and self.text_delta_count > 0:
            return self.text_delta_count / self.elapsed_seconds
        return 0.0

    @property
    def token_inflation(self) -> float:
        """Ratio of reported tokens to estimated tokens."""
        if self.estimated_tokens == 0:
            return 0.0
        return self.reported_output_tokens / self.estimated_tokens

    @property
    def suspicion_score(self) -> int:
        """0-100 score: higher = more suspicious."""
        score = 0
        if self.whitespace_ratio > 0.3:
            score += int(self.whitespace_ratio * 50)
        if self.token_inflation > TOKEN_INFLATION_RATIO:
            score += min(30, int(self.token_inflation * 5))
        if self.high_whitespace_deltas > 0:
            score += min(20, self.high_whitespace_deltas * 5)
        if self.invisible_chars > 0:
            score += 15
        if self.repeated_char_deltas > 0:
            score += 15
        if self.large_deltas > 2:
            score += 10
        # Timing/speed heuristics
        if self.effective_tps > 150:
            score += min(25, int(self.effective_tps / 10))
        if self.deltas_per_second > 50:
            score += min(15, int(self.deltas_per_second / 5))
        if self.burst_count > 0:
            score += min(15, self.burst_count * 5)
        if self.text_delta_count > 0 and self.avg_delta_size > 100:
            score += min(15, int(self.avg_delta_size / 20))
        return min(100, score)

    @property
    def flags(self) -> list[str]:
        """Human-readable anomaly flags."""
        f = []
        if self.whitespace_ratio > 0.3:
            f.append(f"WS:{self.whitespace_ratio:.0%}")
        if self.token_inflation > TOKEN_INFLATION_RATIO:
            f.append(f"TOK:{self.token_inflation:.1f}x")
        if self.high_whitespace_deltas > 0:
            f.append(f"WSδ:{self.high_whitespace_deltas}")
        if self.all_whitespace_deltas > 0:
            f.append(f"EMPTYδ:{self.all_whitespace_deltas}")
        if self.invisible_chars > 0:
            f.append(f"INV:{self.invisible_chars}")
        if self.repeated_char_deltas > 0:
            f.append(f"REP:{self.repeated_char_deltas}")
        if self.large_deltas > 2:
            f.append(f"BIGδ:{self.large_deltas}")
        if self.effective_tps > 150:
            f.append(f"FAST:{self.effective_tps:.0f}tps")
        if self.deltas_per_second > 50:
            f.append(f"BURST:{self.deltas_per_second:.0f}δ/s")
        if self.burst_count > 0:
            f.append(f"BURSTS:{self.burst_count}")
        if self.text_delta_count > 0 and self.avg_delta_size > 100:
            f.append(f"CHUNK:{self.avg_delta_size:.0f}avg")
        return f


class ResponseAnalyzer:
    """Processes SSE events and builds per-response diagnostic stats."""

    def __init__(self):
        self.responses: dict[str, ResponseStats] = {}
        # Track which item belongs to which response
        self._item_response: dict[str, str] = {}
        # Completed responses moved here for history
        self.completed: list[ResponseStats] = []
        # Global counters
        self.total_suspicious = 0
        self.total_whitespace_chars = 0
        self.total_content_chars = 0

    def feed(self, event: SSEEvent) -> ResponseStats | None:
        """Feed an SSE event. Returns the affected ResponseStats for UI updates."""
        etype = event.event_type
        parsed = event.parsed
        ts = float(event.timestamp)  # seconds (integer) — sufficient for burst detection

        if etype == "response.created":
            return self._on_created(parsed, ts)

        elif etype == "response.output_text.delta":
            return self._on_delta(parsed, kind="text", ts=ts)

        elif etype == "response.custom_tool_call_input.delta":
            return self._on_delta(parsed, kind="tool_input", ts=ts)

        elif etype == "response.output_item.added":
            return self._on_item_added(parsed)

        elif etype == "response.completed":
            return self._on_completed(parsed, ts)

        return None

    def _on_created(self, parsed: dict, ts: float) -> ResponseStats:
        resp = parsed.get("response", {})
        rid = resp.get("id", "")
        rs = ResponseStats(
            response_id=rid,
            model=resp.get("model", "?"),
            reasoning_effort=resp.get("reasoning", {}).get("effort", "?"),
            created_at=ts,
        )
        self.responses[rid] = rs
        return rs

    def _on_item_added(self, parsed: dict) -> ResponseStats | None:
        item = parsed.get("item", {})
        iid = item.get("id", "")
        rid = parsed.get("response_id", "")
        if rid and iid:
            self._item_response[iid] = rid
    def _on_delta(self, parsed: dict, kind: str, ts: float = 0.0) -> ResponseStats | None:

        delta = parsed.get("delta", "")
        iid = parsed.get("item_id", "")
        rid = parsed.get("response_id", "") or self._item_response.get(iid, "")
        rs = self.responses.get(rid)
        if rs is None:
            return None

        # Unescape JSON sequences for accurate measurement
        raw = delta.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace("\\\"", "\"")

        n = len(raw)
        ws = len(WHITESPACE_RE.findall(raw))
        content = n - ws
        inv = len(INVISIBLE_RE.findall(raw))
        rpt = bool(REPEATED_CHAR_RE.search(raw))

        rs.total_chars += n
        rs.whitespace_chars += ws
        rs.content_chars += content
        rs.invisible_chars += inv
        rs.delta_count += 1

        if kind == "text":
            rs.text_delta_count += 1
        else:
            rs.tool_input_delta_count += 1

        # ── Timing tracking (per-delta arrival) ───────────────────────
        if ts > 0 and kind == "text":
            rs.delta_timestamps.append(ts)
            # Incremental avg delta size
            if rs.text_delta_count > 0:
                rs.avg_delta_size = (
                    (rs.avg_delta_size * (rs.text_delta_count - 1) + n)
                    / rs.text_delta_count
                )
        if kind == "text":
            self.total_whitespace_chars += ws
            self.total_content_chars += content

        # Anomaly detection (text deltas only — tool input whitespace is structural)
        if kind == "text":
            ratio = ws / n if n > 0 else 0
            if ratio >= WHITESPACE_DELTA_THRESHOLD:
                rs.high_whitespace_deltas += 1
            if ratio >= 1.0:
                rs.all_whitespace_deltas += 1
            if n > LARGE_DELTA_THRESHOLD:
                rs.large_deltas += 1
                if n > rs.max_delta_size:
                    rs.max_delta_size = n
                    rs.max_delta_preview = raw[:120]
            if rpt:
                rs.repeated_char_deltas += 1
            if inv > 0:
                rs.invisible_char_deltas += 1

        # Keep samples of suspicious deltas (up to 3)
        if ratio >= WHITESPACE_DELTA_THRESHOLD or rpt or inv > 0:
            if len(rs.suspicious_samples) < 3:
                preview = raw[:80].replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
                rs.suspicious_samples.append(f"δ={n} ws={ratio:.0%} [{preview}]")

    def _on_completed(self, parsed: dict, ts: float) -> ResponseStats | None:
        resp = parsed.get("response", {})
        rid = resp.get("id", "")
        rs = self.responses.get(rid)
        if rs is None:
            return None

        rs.completed_at = ts

        usage = resp.get("usage", {})
        details_out = usage.get("output_tokens_details") or {}

        rs.reported_output_tokens = usage.get("output_tokens", 0)
        rs.reported_input_tokens = usage.get("input_tokens", 0)
        rs.reported_reasoning_tokens = details_out.get("reasoning_tokens", 0)
        rs.status = resp.get("status", "?")

        # ── Burst detection ───────────────────────────────────────────
        # A "burst" = 3+ text deltas within 50ms window
        tss = rs.delta_timestamps
        if len(tss) >= 3:
            # Compute inter-delta gaps
            gaps = [tss[i] - tss[i-1] for i in range(1, len(tss))]
            if gaps:
                rs.max_delta_gap_ms = max(gaps) * 1000
            # Sliding window burst detection
            burst_count = 0
            window_start = 0
            for i in range(len(tss)):
                while tss[i] - tss[window_start] > 0.05:  # 50ms window
                    window_start += 1
                if i - window_start >= 2:  # at least 3 in window
                    burst_count += 1
                    window_start = i + 1  # reset window after burst
            rs.burst_count = burst_count

        if rs.suspicion_score >= 15:
            self.total_suspicious += 1

        self.completed.append(rs)
        del self.responses[rid]
        return rs

    @property
    def overall_whitespace_ratio(self) -> float:
        total = self.total_whitespace_chars + self.total_content_chars
        if total == 0:
            return 0.0
        return self.total_whitespace_chars / total

    @property
    def active_count(self) -> int:
        return len(self.responses)

    def recent_suspicious(self, n: int = 10) -> list[ResponseStats]:
        """Return the most recently completed suspicious responses."""
        suspicious = [r for r in self.completed if r.suspicion_score >= 10]
        return suspicious[-n:]
