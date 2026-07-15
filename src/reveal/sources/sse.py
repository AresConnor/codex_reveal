"""SSE event source — reads from Codex logs_2.sqlite in real time."""

import json
import os
import sqlite3
from collections.abc import Iterator

from ..models import SSEEvent

DEFAULT_DB = os.path.expandvars(r"%USERPROFILE%\.codex\logs_2.sqlite")


class SSESource:
    """Polling reader for Codex SSE log events."""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self._last_id = 0
        self._init_cursor()

    def _init_cursor(self):
        if not os.path.exists(self.db_path):
            self._last_id = 0
            return
        db = sqlite3.connect(self.db_path)
        row = db.execute(
            "SELECT MAX(id) FROM logs WHERE target = 'codex_api::sse::responses'"
        ).fetchone()
        self._last_id = row[0] if row and row[0] else 0
        db.close()

    def poll(self) -> list[SSEEvent]:
        """Return new SSE events since last poll. Non-blocking."""
        if not os.path.exists(self.db_path):
            return []

        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, ts, level, feedback_log_body FROM logs "
            "WHERE target = 'codex_api::sse::responses' AND id > ? "
            "ORDER BY id ASC",
            (self._last_id,)
        ).fetchall()
        db.close()

        events = []
        for row in rows:
            self._last_id = row["id"]
            body = row["feedback_log_body"] or ""
            if not body.startswith("SSE event:"):
                continue

            try:
                parsed = json.loads(body[11:])
            except json.JSONDecodeError:
                parsed = {}

            events.append(SSEEvent(
                id=row["id"],
                timestamp=row["ts"],
                level=row["level"],
                event_type=parsed.get("type", "?"),
                raw_body=body,
                parsed=parsed,
            ))

        return events

    def reset(self):
        """Rewind to the latest event (skip everything logged so far)."""
        self._init_cursor()

    def count_total(self) -> int:
        """Total SSE events in the database."""
        if not os.path.exists(self.db_path):
            return 0
        db = sqlite3.connect(self.db_path)
        row = db.execute(
            "SELECT COUNT(*) FROM logs WHERE target = 'codex_api::sse::responses'"
        ).fetchone()
        db.close()
        return row[0] if row else 0
