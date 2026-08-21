"""Regression tests for docs/security-audit-crash.md (F1–F11)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rich.markup import render
from textual.app import App, ComposeResult

from reveal.diagnostics import ResponseAnalyzer
from reveal.models import (
    ItemKind,
    RawLogEvent,
    ResponseItemState,
    ResponseState,
    ResponseStatus,
    SessionMeta,
    SSEEvent,
    ToolResult,
)
from reveal.routing import ResponseRouter, parse_log_body
from reveal.sources.log_stream import LogStream
from reveal.sources.rollout import (
    RolloutTailer,
    SessionCatalog,
    _parse_session_meta,
    read_rollout,
)
from reveal.widgets.log_view import LogView, format_history_token_line
from reveal.widgets.response_card import (
    ResponseCard,
    ResponseItemWidget,
    _local_item_time,
)
from reveal.widgets.response_feed import ResponseFeed
from reveal.widgets.session_tree import _agent_label
from reveal.widgets.virtual_content import VirtualContentSource


def _meta_line(**payload):
    base = {
        "id": "t1",
        "session_id": "t1",
        "parent_thread_id": "",
        "timestamp": "2026-01-01T00:00:00Z",
        "cwd": "c:/x",
        "originator": "o",
        "cli_version": "0",
        "thread_source": "user",
    }
    base.update(payload)
    return {"type": "session_meta", "payload": base}


def _write_jsonl(path: Path, *rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            if isinstance(row, (bytes, bytearray)):
                raise TypeError("use binary write for bytes")
            f.write(json.dumps(row) if not isinstance(row, str) else row)
            f.write("\n")


class _Sink:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text):
        self.lines.append(text)


class ParseSessionMetaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name="rollout-a.jsonl"):
        return self.root / name

    def test_skips_non_object_payload_and_top_level(self):
        for raw in (
            {"type": "session_meta", "payload": []},
            {"type": "session_meta", "payload": None},
            [1, 2],
        ):
            path = self._path()
            _write_jsonl(path, raw)
            self.assertIsNone(_parse_session_meta(str(path)), msg=raw)

    def test_skips_bad_field_types(self):
        cases = (
            {"timestamp": 123},
            {"cwd": 123},
            {"id": [1], "session_id": [1]},
            {"source": {"subagent": {"thread_spawn": {"depth": "deep"}}}},
            {"source": {"subagent": {"thread_spawn": {"depth": 1e999}}}},
        )
        for payload in cases:
            path = self._path()
            _write_jsonl(path, _meta_line(**payload))
            meta = _parse_session_meta(str(path))
            if "id" in payload:
                self.assertIsNone(meta, msg=payload)
            else:
                self.assertIsNotNone(meta, msg=payload)
                self.assertIsInstance(meta.cwd, str)
                self.assertIsInstance(meta.depth, int)
                self.assertTrue(meta.timestamp.tzinfo)

    def test_catalog_refresh_survives_poison_file_and_mixed_timezones(self):
        folder = self.root / "2026" / "01" / "01"
        _write_jsonl(folder / "rollout-good.jsonl", _meta_line(id="good", session_id="good"))
        _write_jsonl(folder / "rollout-bad.jsonl", {"type": "session_meta", "payload": []})
        _write_jsonl(
            folder / "rollout-naive.jsonl",
            _meta_line(id="naive", session_id="naive", timestamp="2026-01-02T00:00:00"),
        )
        catalog = SessionCatalog(sessions_dir=str(self.root))
        catalog.refresh()
        self.assertIn("good", catalog.sessions)
        self.assertIn("naive", catalog.sessions)
        self.assertEqual(len(catalog.workspace_keys()), 1)


class ReadRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_skips_null_and_non_object_lines(self):
        path = self.root / "rollout.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_meta_line()) + "\n")
            f.write("null\n")
            f.write("[1]\n")
            f.write('"x"\n')
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}}) + "\n")
        events = read_rollout(str(path))
        self.assertEqual(len(events), 2)
        self.assertTrue(all(isinstance(ev, dict) for ev in events))

    def test_replace_invalid_utf8_instead_of_raising(self):
        path = self.root / "rollout.jsonl"
        path.write_bytes(b'{"type":"event_msg"}\n{"payload":"\xff\xfe broken"}\n')
        events = read_rollout(str(path))
        self.assertIsInstance(events, list)

    def test_torn_multibyte_at_eof_does_not_raise(self):
        path = self.root / "rollout.jsonl"
        path.write_bytes(b'{"message":"ok "}\n' + "你".encode("utf-8")[:-1])
        self.assertIsInstance(read_rollout(str(path)), list)
        self.assertIsInstance(RolloutTailer(str(path)).poll_tools(), list)

    def test_tailer_infinity_exit_code_is_ignored(self):
        path = self.root / "rollout.jsonl"
        _write_jsonl(
            path,
            {
                "type": "event_msg",
                "payload": {"type": "tool_result", "call_id": "c1", "exit_code": 1e309},
            },
        )
        results = RolloutTailer(str(path)).poll_tools()
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].exit_code)


class HistoryWriteTests(unittest.TestCase):
    def test_event_msg_payload_type_confusion(self):
        sink = _Sink()
        LogView._write_event_msg(None, sink, [])
        LogView._write_event_msg(None, sink, {"type": "token_count", "info": [1]})
        LogView._write_event_msg(
            None,
            sink,
            {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 1e309}},
            },
        )
        LogView._write_event_msg(None, sink, {"type": "agent_message", "message": "[/]"})
        render(sink.lines[-1])

    def test_response_item_payload_type_confusion(self):
        sink = _Sink()
        LogView._write_response_item(None, sink, {"role": "assistant", "content": "x"})
        LogView._write_response_item(None, sink, {"role": "assistant", "content": [1]})

    def test_token_line_overflow_is_safe(self):
        line = format_history_token_line(
            {"input_tokens": float("inf"), "cached_input_tokens": 1, "output_tokens": 1}
        )
        self.assertIn("tokens:", line)
        self.assertNotIn("cached_rate=", line)

    def test_token_values_with_markup_are_escaped(self):
        line = format_history_token_line(
            {"input_tokens": "[/]", "cached_input_tokens": "[/]", "output_tokens": "[/]"}
        )
        render(line)

    def test_task_complete_duration_markup_is_escaped(self):
        sink = _Sink()
        LogView._write_event_msg(None, sink, {"type": "task_complete", "duration_ms": "[/]"})
        render(sink.lines[-1])


class ParseLogBodyTests(unittest.TestCase):
    def _parse(self, body: str):
        return parse_log_body(
            log_id=1,
            timestamp=1.0,
            target="codex_api::sse::responses",
            body=body,
        )

    def test_non_object_sse_payloads(self):
        for body in (
            "SSE event: []",
            'SSE event: "x"',
            "SSE event: null",
            "unhandled responses event: []",
        ):
            ev = self._parse(body)
            self.assertIsInstance(ev.parsed, dict)

    def test_sequence_infinity_does_not_raise(self):
        ev = self._parse('SSE event: {"type":"response.created","sequence_number":1e309}')
        self.assertIsNone(ev.sequence_number)


class RouterApplyTests(unittest.TestCase):
    def test_response_created_non_dict_response(self):
        router = ResponseRouter()
        ev = parse_log_body(
            log_id=1,
            timestamp=1.0,
            target="codex_api::sse::responses",
            body='SSE event: {"type":"response.created","response_id":"resp_x","response":"x"}',
        )
        snap = router.feed(ev)
        self.assertTrue(snap.changed_response_ids)

    def test_summary_index_infinity(self):
        router = ResponseRouter()
        created = parse_log_body(
            log_id=1,
            timestamp=1.0,
            target="codex_api::sse::responses",
            body='SSE event: {"type":"response.created","response":{"id":"resp_x"}}',
        )
        delta = parse_log_body(
            log_id=2,
            timestamp=1.1,
            target="codex_api::sse::responses",
            body=(
                'SSE event: {"type":"response.reasoning_summary_text.delta",'
                '"response_id":"resp_x","item_id":"rs_1","output_index":0,'
                '"summary_index":1e309,"delta":"hi"}'
            ),
        )
        router.feed(created)
        router.feed(delta)


class DiagnosticsTests(unittest.TestCase):
    def test_created_model_list_does_not_poison_stats(self):
        a = ResponseAnalyzer()
        a.feed(
            SSEEvent(
                id=1,
                timestamp=1.0,
                level="INFO",
                event_type="response.created",
                raw_body="",
                parsed={"type": "response.created", "response": {"id": "r1", "model": ["x"]}},
            )
        )
        rs = a.responses["r1"]
        self.assertIsInstance(rs.model, str)
        f"{rs.model[:12]:<12}"

    def test_tool_input_delta_does_not_raise_unbound_ratio(self):
        a = ResponseAnalyzer()
        a.feed(
            SSEEvent(
                id=1,
                timestamp=1.0,
                level="INFO",
                event_type="response.created",
                raw_body="",
                parsed={"response": {"id": "r1", "model": "sol"}},
            )
        )
        a.feed(
            SSEEvent(
                id=2,
                timestamp=1.1,
                level="INFO",
                event_type="response.custom_tool_call_input.delta",
                raw_body="",
                parsed={"response_id": "r1", "item_id": "i1", "delta": "abc"},
            )
        )
        self.assertEqual(a.responses["r1"].tool_input_delta_count, 1)

    def test_completed_usage_list_and_string_tokens(self):
        a = ResponseAnalyzer()
        a.feed(
            SSEEvent(
                id=1,
                timestamp=1.0,
                level="INFO",
                event_type="response.created",
                raw_body="",
                parsed={"response": {"id": "r1", "model": "sol"}},
            )
        )
        a.feed(
            SSEEvent(
                id=2,
                timestamp=2.0,
                level="INFO",
                event_type="response.completed",
                raw_body="",
                parsed={
                    "response": {
                        "id": "r1",
                        "usage": {"output_tokens": "nope", "input_tokens": [1]},
                    }
                },
            )
        )
        self.assertEqual(len(a.completed), 1)
        self.assertIsInstance(a.completed[0].suspicion_score, int)

    def test_delta_non_string_is_ignored(self):
        a = ResponseAnalyzer()
        a.feed(
            SSEEvent(
                id=1,
                timestamp=1.0,
                level="INFO",
                event_type="response.created",
                raw_body="",
                parsed={"response": {"id": "r1"}},
            )
        )
        a.feed(
            SSEEvent(
                id=2,
                timestamp=1.1,
                level="INFO",
                event_type="response.output_text.delta",
                raw_body="",
                parsed={"response_id": "r1", "item_id": "i1", "delta": None},
            )
        )


class ResponseCardTests(unittest.TestCase):
    def test_header_usage_type_confusion_and_markup(self):
        state = ResponseState(response_id="resp_a", model="[/]", usage=[1])
        text = ResponseCard(state)._header_text()
        render(text)
        state.usage = {"input_tokens_details": [1]}
        render(ResponseCard(state)._header_text())

    def test_item_summary_escapes_markup(self):
        answer = ResponseItemState(
            item_id="i1",
            output_index=0,
            kind=ItemKind.ANSWER,
            protocol_type="message",
            assembled_text="[/]",
        )
        render(ResponseItemWidget(answer, "resp_a")._summary_text())
        tool = ResponseItemState(
            item_id="i2",
            output_index=1,
            kind=ItemKind.TOOL_CALL,
            protocol_type="custom_tool_call",
            tool_name="[/]",
            call_id="[/]",
            tool_input="[/]",
        )
        render(ResponseItemWidget(tool, "resp_a")._summary_text())
        render(ResponseItemWidget(tool, "resp_a")._semantic_text())
        tool.tool_result = ToolResult(call_id="[/]", status="[/]", output="[/]")
        render(ResponseItemWidget(tool, "resp_a")._tool_result_text())

    def test_local_item_time_out_of_range(self):
        from reveal.models import ItemKind, ResponseItemState

        item = ResponseItemState(
            item_id="i",
            output_index=0,
            kind=ItemKind.ANSWER,
            protocol_type="message",
            events=[
                RawLogEvent(
                    log_id=1,
                    timestamp=float("inf"),
                    target="t",
                    process_uuid=None,
                    thread_id=None,
                    event_type="x",
                    sequence_number=None,
                    response_id=None,
                    item_id=None,
                    output_index=None,
                    raw_body="",
                )
            ],
        )
        self.assertEqual(_local_item_time(item), "--:--:--.---")

    def test_other_widget_does_not_use_response_id_as_css_id(self):
        card = ResponseCard(ResponseState(response_id="resp[bad]"))
        self.assertFalse(hasattr(card, "_other_widget") and card._other_widget is not None)
        # Widget ids must not be interpolated from untrusted response_id.
        self.assertNotIn("resp[bad]", (card.id or ""))


class MarkupEscapeTests(unittest.TestCase):
    def test_agent_label_escapes_unmatched_close(self):
        meta = SessionMeta(
            rollout_id="t1",
            session_id="t1",
            parent_thread_id="",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            cwd="c:/x",
            originator="o",
            cli_version="0",
            agent_nickname="[/]",
            agent_role="[/dim]",
        )
        render(_agent_label(meta))


class LogStreamRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "logs.sqlite"
        db = sqlite3.connect(self.db_path)
        db.execute(
            """CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER,
                ts_nanos INTEGER,
                level TEXT,
                target TEXT,
                feedback_log_body
            )"""
        )
        db.commit()
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, *, ts=1, body="SSE event: {}", blob=False):
        db = sqlite3.connect(self.db_path)
        if blob:
            db.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body) VALUES (?, 0, 'INFO', ?, ?)",
                (ts, "codex_api::sse::responses", body),
            )
        else:
            db.execute(
                "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body) VALUES (?, 0, 'INFO', ?, ?)",
                (ts, "codex_api::sse::responses", body),
            )
        db.commit()
        db.close()

    def test_backfill_bad_rows_still_initializes(self):
        self._insert(ts=None, body="SSE event: []")
        self._insert(ts="abc", body="SSE event: null")
        self._insert(body=b"not-text", blob=True)
        self._insert(body=42)
        stream = LogStream(db_path=str(self.db_path))
        events = stream.ensure_started()
        self.assertTrue(stream._initialized)
        self.assertIsInstance(events, list)

    def test_non_finite_max_id_does_not_raise(self):
        db = sqlite3.connect(self.db_path)
        db.execute("DROP TABLE logs")
        db.execute(
            "CREATE TABLE logs (id, ts, ts_nanos, level, target, feedback_log_body)"
        )
        db.execute(
            "INSERT INTO logs VALUES (1e999, 1.0, 0, 'INFO', 'codex_api::sse::responses', 'plain')"
        )
        db.commit()
        db.close()
        stream = LogStream(db_path=str(self.db_path))
        self.assertEqual(stream.backfill(), [])
        self.assertEqual(stream.poll(), [])
        self.assertIsNone(stream.reset_to_live())


class _QuietLogView(LogView):
    """LogView without the mount worker, so tests don't touch the real log DB."""

    def on_mount(self) -> None:
        pass


class _QuietLogViewApp(App):
    def compose(self) -> ComposeResult:
        yield _QuietLogView(id="lv")


class LogViewConstructionTests(unittest.TestCase):
    def test_default_construction_does_not_raise(self):
        view = LogView()
        self.assertIsNotNone(view.catalog)


class LogViewLoadSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_session_survives_malformed_and_markup_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            _write_jsonl(
                path,
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "[/]"}},
                None,
                [1],
            )
            meta = SessionMeta(
                rollout_id="t1",
                session_id="t1",
                parent_thread_id="",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                cwd="c:/x",
                originator="o",
                cli_version="0",
                file_path=str(path),
                thread_id="t1",
            )
            app = _QuietLogViewApp()
            async with app.run_test(size=(100, 30)) as pilot:
                view = app.query_one("#lv", _QuietLogView)
                view.load_session(meta)
                await pilot.pause()
                self.assertGreater(view._history_event_count, 0)


class BoundednessTests(unittest.TestCase):
    def test_virtual_content_total_is_stable(self):
        src = VirtualContentSource(rows=["x" * 10] * 50)
        src.set_width(4)
        n = src.total_display_lines()
        self.assertEqual(n, src.total_display_lines())
        self.assertEqual(n, sum(len(src.wrapped_lines_for_row(i)) for i in range(len(src.rows))))

    def test_trim_evicts_oldest_active_when_over_limit(self):
        feed = ResponseFeed()
        for i in range(feed.card_limit + 5):
            rid = f"resp_{i}"
            feed._states[rid] = ResponseState(response_id=rid, status=ResponseStatus.ACTIVE)
            feed._order.append(rid)
        feed._trim()
        self.assertLessEqual(len(feed._order), feed.card_limit)

    def test_router_drops_old_completed_responses(self):
        import reveal.routing as routing

        cap = getattr(routing, "MAX_RESPONSES", 8)
        router = ResponseRouter()
        n = max(int(cap) + 4, 12)
        for i in range(n):
            rid = f"resp_{i:03d}"
            router.feed(
                parse_log_body(
                    log_id=i * 2 + 1,
                    timestamp=float(i),
                    target="codex_api::sse::responses",
                    body=f'SSE event: {{"type":"response.created","response":{{"id":"{rid}"}}}}',
                )
            )
            router.feed(
                parse_log_body(
                    log_id=i * 2 + 2,
                    timestamp=float(i) + 0.5,
                    target="codex_api::sse::responses",
                    body=f'SSE event: {{"type":"response.completed","response":{{"id":"{rid}"}}}}',
                )
            )
        self.assertLessEqual(len(router.responses), cap)


if __name__ == "__main__":
    unittest.main()
