import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reveal.sources.sse import SSESource


def _create_logs_db(path):
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            ts_nanos INTEGER NOT NULL,
            level TEXT NOT NULL,
            target TEXT NOT NULL,
            feedback_log_body TEXT
        )"""
    )
    db.commit()
    db.close()


def _insert_event(path, event_type):
    body = "SSE event: " + json.dumps({"type": event_type})
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body) "
        "VALUES (1, 0, 'INFO', 'codex_api::sse::responses', ?)",
        (body,),
    )
    db.commit()
    db.close()


class SSESourceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "logs.sqlite"
        _create_logs_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_poll_reads_backlog_in_bounded_batches(self):
        source = SSESource(str(self.db_path), batch_size=2)

        for event_type in ("response.created", "response.output_text.delta", "response.completed"):
            _insert_event(self.db_path, event_type)

        self.assertEqual(
            [event.event_type for event in source.poll()],
            ["response.created", "response.output_text.delta"],
        )
        self.assertEqual(
            [event.event_type for event in source.poll()],
            ["response.completed"],
        )
        self.assertEqual(source.poll(), [])
        self.assertEqual(source.events_seen, 3)
        self.assertEqual(source.cursor_id, 3)

    def test_reset_skips_existing_events_and_resets_counter(self):
        source = SSESource(str(self.db_path))
        _insert_event(self.db_path, "response.created")
        self.assertEqual(len(source.poll()), 1)

        source.reset()

        self.assertEqual(source.events_seen, 0)
        self.assertEqual(source.poll(), [])
