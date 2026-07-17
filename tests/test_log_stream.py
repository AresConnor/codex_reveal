import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reveal.sources.log_stream import LogStream, SSESource
from reveal.routing import ResponseRouter


def _create_logs_db(path, *, with_thread_cols=False):
    db = sqlite3.connect(path)
    if with_thread_cols:
        db.execute(
            """CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL,
                level TEXT NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                thread_id TEXT,
                process_uuid TEXT
            )"""
        )
    else:
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


def _insert(path, event_type, *, response_id="resp_1", target="codex_api::sse::responses", extra=None):
    payload = {"type": event_type}
    if event_type in ("response.created", "response.completed", "response.failed", "response.incomplete"):
        payload["response"] = {"id": response_id, "model": "gpt-sol"}
        if event_type == "response.completed":
            payload["response"]["usage"] = {"input_tokens": 1, "output_tokens": 2}
    if extra:
        payload.update(extra)
    body = "SSE event: " + json.dumps(payload, separators=(",", ":"))
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body) VALUES (1, 0, 'INFO', ?, ?)",
        (target, body),
    )
    db.commit()
    db.close()


class SSESourceCompatTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "logs.sqlite"
        _create_logs_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_poll_reads_backlog_in_bounded_batches(self):
        source = SSESource(str(self.db_path), batch_size=2)
        for event_type in ("response.created", "response.output_text.delta", "response.completed"):
            _insert(self.db_path, event_type)
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
        _insert(self.db_path, "response.created")
        self.assertEqual(len(source.poll()), 1)
        source.reset()
        self.assertEqual(source.events_seen, 0)
        self.assertEqual(source.poll(), [])


class LogStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "logs.sqlite"
        _create_logs_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cursor_advances_over_malformed(self):
        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body) VALUES (1,0,'INFO','codex_api::sse::responses',?)",
            ("SSE event: {bad",),
        )
        db.execute(
            "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body) VALUES (1,0,'INFO','codex_api::sse::responses',?)",
            ('SSE event: {"type":"response.created","response":{"id":"resp_ok"}}',),
        )
        db.commit()
        db.close()
        stream = LogStream(str(self.db_path), targets=("codex_api::sse::responses",))
        stream.reset_to_live()
        # reset skips existing; insert new after
        _insert(self.db_path, "response.completed", response_id="resp_new")
        events = stream.poll()
        self.assertTrue(any(e.response_id == "resp_new" for e in events))
        self.assertEqual(stream.cursor_id, 3)

    def test_high_water_handoff_no_duplicates(self):
        for i in range(5):
            _insert(self.db_path, "response.created", response_id=f"resp_{i}")
            _insert(self.db_path, "response.completed", response_id=f"resp_{i}")
        stream = LogStream(
            str(self.db_path),
            targets=("codex_api::sse::responses",),
            backfill_complete=3,
            row_budget=1000,
        )
        backfilled = stream.ensure_started()
        ids = [e.log_id for e in backfilled]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        # live poll should not re-read backfilled ids
        live = stream.poll()
        live_ids = {e.log_id for e in live}
        self.assertTrue(live_ids.isdisjoint(set(ids)))

    def test_rows_during_backfill_consumed_once(self):
        _insert(self.db_path, "response.created", response_id="resp_a")
        _insert(self.db_path, "response.completed", response_id="resp_a")
        stream = LogStream(str(self.db_path), targets=("codex_api::sse::responses",))
        # Simulate: capture high water conceptually via ensure_started
        events = stream.ensure_started()
        # Insert after backfill cursor installed
        _insert(self.db_path, "response.created", response_id="resp_b")
        live = stream.poll()
        all_ids = [e.log_id for e in events] + [e.log_id for e in live]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertTrue(any(e.response_id == "resp_b" for e in live))

    def test_no_full_table_count_in_source(self):
        import inspect
        from reveal.sources import log_stream as mod

        src = inspect.getsource(mod)
        self.assertNotIn("COUNT(*)", src)
        self.assertNotIn("count(*)", src.lower().replace(" ", ""))

    def test_backfill_feeds_router(self):
        _insert(self.db_path, "response.created", response_id="resp_z")
        _insert(
            self.db_path,
            "response.output_item.added",
            response_id="resp_z",
            extra={
                "output_index": 0,
                "item": {"id": "item_z", "type": "message"},
                "response_id": "resp_z",
            },
        )
        _insert(self.db_path, "response.completed", response_id="resp_z")
        stream = LogStream(str(self.db_path), targets=("codex_api::sse::responses",))
        events = stream.ensure_started()
        router = ResponseRouter()
        router.feed_many(events)
        self.assertIn("resp_z", router.responses)


if __name__ == "__main__":
    unittest.main()
