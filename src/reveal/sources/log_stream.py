"""Multi-target SQLite high-water polling for Live SSE reconstruction."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass, field

from ..models import RawLogEvent, SSEEvent
from ..routing import parse_log_body
from ..safe import as_finite_float, as_finite_int, as_str, as_text

DEFAULT_DB = os.path.expandvars(r"%USERPROFILE%\.codex\logs_2.sqlite")

DEFAULT_TARGETS = (
    "codex_api::sse::responses",
    "codex_core::thread",
    "codex_core::client",
    "codex_api::endpoint::responses",
    # Thread-bearing breadcrumbs used to attribute concurrent subagents.
    "codex_core::session::turn",
    "codex_core::stream_events_utils",
)

BACKFILL_COMPLETE_RESPONSES = 20
BACKFILL_ROW_BUDGET = 50_000
DEFAULT_BATCH = 500


@dataclass
class LogStreamStats:
    high_water_id: int = 0
    cursor_id: int = 0
    events_seen: int = 0
    backfill_partial: int = 0
    backfill_rows_scanned: int = 0
    last_error: str = ""
    db_generation: int = 0


@dataclass
class LogStream:
    """Row-id based multi-target log reader with startup backfill."""

    db_path: str = DEFAULT_DB
    batch_size: int = DEFAULT_BATCH
    targets: tuple[str, ...] = DEFAULT_TARGETS
    backfill_complete: int = BACKFILL_COMPLETE_RESPONSES
    row_budget: int = BACKFILL_ROW_BUDGET
    stats: LogStreamStats = field(default_factory=LogStreamStats)
    _last_id: int = 0
    _initialized: bool = False
    _db_mtime: float | None = None
    _db_size: int | None = None
    _has_thread_cols: bool | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def cursor_id(self) -> int:
        return self._last_id

    @property
    def events_seen(self) -> int:
        return self.stats.events_seen

    def _connect(self) -> sqlite3.Connection | None:
        if not os.path.exists(self.db_path):
            return None
        try:
            uri = f"file:{os.path.abspath(self.db_path)}?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=0.2)
        except sqlite3.Error:
            try:
                db = sqlite3.connect(self.db_path, timeout=0.2)
            except sqlite3.Error as exc:
                self.stats.last_error = str(exc)
                return None
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=200")
        except sqlite3.Error:
            pass
        if self._has_thread_cols is None:
            self._has_thread_cols = self._detect_thread_cols(db)
        return db

    def _detect_thread_cols(self, db: sqlite3.Connection) -> bool:
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(logs)").fetchall()}
            return "thread_id" in cols and "process_uuid" in cols
        except sqlite3.Error:
            return False

    def _select_cols(self) -> str:
        if self._has_thread_cols:
            return (
                "id, ts, ts_nanos, level, target, feedback_log_body, "
                "thread_id, process_uuid"
            )
        return "id, ts, ts_nanos, level, target, feedback_log_body"

    def _signature(self) -> tuple[float | None, int | None]:
        try:
            st = os.stat(self.db_path)
            return st.st_mtime, st.st_size
        except OSError:
            return None, None

    def detect_reset(self) -> bool:
        mtime, size = self._signature()
        if self._db_mtime is None:
            self._db_mtime, self._db_size = mtime, size
            return False
        replaced = False
        if size is not None and self._db_size is not None and size + 1024 < self._db_size * 0.5:
            replaced = True
        if replaced:
            self.stats.db_generation += 1
            self._db_mtime, self._db_size = mtime, size
            self._last_id = 0
            self._initialized = False
            self.stats.events_seen = 0
            self._has_thread_cols = None
            return True
        self._db_mtime, self._db_size = mtime, size
        return False

    def ensure_started(self) -> list[RawLogEvent]:
        with self._lock:
            if self._initialized:
                return []
            events = self.backfill()
            self._initialized = True
            return events

    def reset_to_live(self) -> None:
        db = self._connect()
        if db is None:
            self._last_id = 0
            self._initialized = True
            return
        try:
            row = db.execute("SELECT MAX(id) AS m FROM logs").fetchone()
            self._last_id = (as_finite_int(row["m"]) or 0) if row else 0
            self.stats.high_water_id = self._last_id
            self.stats.cursor_id = self._last_id
            self.stats.events_seen = 0
            self._initialized = True
        except sqlite3.Error as exc:
            self.stats.last_error = str(exc)
        finally:
            db.close()

    def backfill(self) -> list[RawLogEvent]:
        db = self._connect()
        if db is None:
            self._initialized = True
            return []

        try:
            row = db.execute("SELECT MAX(id) AS m FROM logs").fetchone()
            high_water = (as_finite_int(row["m"]) or 0) if row else 0
            self.stats.high_water_id = high_water
            if high_water <= 0:
                self._last_id = 0
                return []

            targets = list(self.targets)
            placeholders = ",".join("?" for _ in targets)
            complete_starts: set[int] = set()
            inflight_starts: set[int] = set()
            scanned = 0
            page = min(self.batch_size * 4, 2000)
            cursor = high_water
            seen_response_starts: dict[str, int] = {}
            completed_responses: set[str] = set()

            while cursor > 0 and scanned < self.row_budget:
                lower = max(0, cursor - page)
                rows = db.execute(
                    f"SELECT id, feedback_log_body, target FROM logs "
                    f"WHERE id > ? AND id <= ? AND target IN ({placeholders}) "
                    f"ORDER BY id DESC",
                    (lower, cursor, *targets),
                ).fetchall()
                if not rows:
                    scanned += cursor - lower
                    cursor = lower
                    continue

                for r in rows:
                    scanned += 1
                    body = as_text(r["feedback_log_body"])
                    rid = None
                    etype = ""
                    if body.startswith("SSE event:"):
                        if '"type":"response.created"' in body or '"type": "response.created"' in body:
                            etype = "response.created"
                        elif '"type":"response.completed"' in body or '"type": "response.completed"' in body:
                            etype = "response.completed"
                        elif '"type":"response.failed"' in body or '"type": "response.failed"' in body:
                            etype = "response.failed"
                        elif '"type":"response.incomplete"' in body or '"type": "response.incomplete"' in body:
                            etype = "response.incomplete"
                        for key in ('"id":"resp_', '"id": "resp_'):
                            idx = body.find(key)
                            if idx >= 0:
                                start = idx + len(key) - len("resp_")
                                end = body.find('"', start)
                                if end > start:
                                    rid = body[start:end]
                                    break
                        if rid is None:
                            for key in ('"response_id":"', '"response_id": "'):
                                idx = body.find(key)
                                if idx >= 0:
                                    start = idx + len(key)
                                    end = body.find('"', start)
                                    if end > start:
                                        rid = body[start:end]
                                        break

                    if etype == "response.created" and rid:
                        seen_response_starts[rid] = r["id"]
                        if rid in completed_responses:
                            complete_starts.add(r["id"])
                        else:
                            inflight_starts.add(r["id"])
                    elif etype in ("response.completed", "response.failed", "response.incomplete") and rid:
                        completed_responses.add(rid)
                        if rid in seen_response_starts:
                            complete_starts.add(seen_response_starts[rid])
                            inflight_starts.discard(seen_response_starts[rid])

                    if len(complete_starts) >= self.backfill_complete:
                        break

                cursor = lower
                if len(complete_starts) >= self.backfill_complete:
                    break

            self.stats.backfill_rows_scanned = scanned
            start_ids = complete_starts | inflight_starts
            if not start_ids:
                min_id = max(1, high_water - min(2000, self.row_budget))
            else:
                min_id = min(start_ids)

            cols = self._select_cols()
            rows = db.execute(
                f"SELECT {cols} FROM logs "
                f"WHERE id >= ? AND id <= ? AND target IN ({placeholders}) "
                f"ORDER BY id ASC",
                (min_id, high_water, *targets),
            ).fetchall()

            events = []
            for r in rows:
                try:
                    events.append(self._row_to_event(r))
                except Exception:
                    continue
            self._last_id = high_water
            self.stats.cursor_id = high_water
            self.stats.events_seen += len(events)
            return events
        except sqlite3.Error as exc:
            self.stats.last_error = str(exc)
            return []
        finally:
            db.close()

    def poll(self) -> list[RawLogEvent]:
        if self.detect_reset():
            return self.ensure_started()
        if not self._initialized:
            return self.ensure_started()

        db = self._connect()
        if db is None:
            return []

        targets = list(self.targets)
        placeholders = ",".join("?" for _ in targets)
        cols = self._select_cols()
        try:
            rows = db.execute(
                f"SELECT {cols} FROM logs "
                f"WHERE id > ? AND target IN ({placeholders}) "
                f"ORDER BY id ASC LIMIT ?",
                (self._last_id, *targets, self.batch_size),
            ).fetchall()
        except sqlite3.Error as exc:
            self.stats.last_error = str(exc)
            return []
        finally:
            db.close()

        events: list[RawLogEvent] = []
        for r in rows:
            row_id = as_finite_int(r["id"])
            if row_id is None:
                continue
            self._last_id = row_id
            try:
                events.append(self._row_to_event(r))
            except Exception:
                continue
        self.stats.cursor_id = self._last_id
        self.stats.events_seen += len(events)
        return events

    def sqlite_lag(self) -> int | None:
        db = self._connect()
        if db is None:
            return None
        try:
            row = db.execute("SELECT MAX(id) AS m FROM logs").fetchone()
            latest = (as_finite_int(row["m"]) or 0) if row else 0
            return max(0, latest - self._last_id)
        except sqlite3.Error:
            return None
        finally:
            db.close()

    def _row_to_event(self, row: sqlite3.Row) -> RawLogEvent:
        keys = set(row.keys())
        ts = as_finite_float(row["ts"] if "ts" in keys else 0.0)
        ts_nanos = as_finite_float(row["ts_nanos"] if "ts_nanos" in keys else 0.0)
        timestamp = ts + ts_nanos / 1e9
        thread_id = row["thread_id"] if "thread_id" in keys else None
        process_uuid = row["process_uuid"] if "process_uuid" in keys else None
        if thread_id is not None and not isinstance(thread_id, str):
            thread_id = as_str(thread_id) or None
        if process_uuid is not None and not isinstance(process_uuid, str):
            process_uuid = as_str(process_uuid) or None
        target = as_str(row["target"] if "target" in keys else None) or "codex_api::sse::responses"
        level = as_str(row["level"] if "level" in keys else None) or "INFO"
        body = as_text(row["feedback_log_body"] if "feedback_log_body" in keys else "")
        log_id = as_finite_int(row["id"], 0) or 0
        return parse_log_body(
            log_id=log_id,
            timestamp=timestamp,
            target=target,
            body=body,
            level=level,
            process_uuid=process_uuid,
            thread_id=thread_id,
        )


class SSESource:
    """Compatibility polling reader for Codex SSE log events."""

    def __init__(self, db_path: str = DEFAULT_DB, batch_size: int = DEFAULT_BATCH):
        self._stream = LogStream(
            db_path=db_path,
            batch_size=batch_size,
            targets=("codex_api::sse::responses",),
        )
        self._stream.reset_to_live()

    @property
    def db_path(self) -> str:
        return self._stream.db_path

    def poll(self) -> list[SSEEvent]:
        return [e.to_sse_event() for e in self._stream.poll()]

    def reset(self) -> None:
        self._stream.reset_to_live()

    @property
    def events_seen(self) -> int:
        return self._stream.events_seen

    @property
    def cursor_id(self) -> int:
        return self._stream.cursor_id
