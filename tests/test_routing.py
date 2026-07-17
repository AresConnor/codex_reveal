import unittest

from reveal.models import (
    AttributionConfidence,
    ItemKind,
    ResponseStatus,
    ToolResult,
)
from reveal.routing import ResponseRouter, classify_item, display_item_number, parse_log_body
from tests.fixtures.sse_samples import (
    fixture_ambiguous_delta_without_ids,
    fixture_function_and_unknown_items,
    fixture_malformed_and_unhandled,
    fixture_partial_missing_start,
    fixture_single_response_thinking_tool_answer,
    fixture_two_interleaved_responses,
    make_event,
)


class ClassifyTests(unittest.TestCase):
    def test_classification_table(self):
        self.assertEqual(classify_item("reasoning"), ItemKind.THINKING)
        self.assertEqual(classify_item("custom_tool_call"), ItemKind.TOOL_CALL)
        self.assertEqual(classify_item("function_call"), ItemKind.TOOL_CALL)
        self.assertEqual(classify_item("message"), ItemKind.ANSWER)
        self.assertEqual(classify_item("future_widget"), ItemKind.OTHER)

    def test_display_item_numbers_are_one_based(self):
        self.assertEqual(display_item_number(0), 1)
        self.assertEqual(display_item_number(1), 2)
        self.assertEqual(display_item_number(2), 3)


class ParseTests(unittest.TestCase):
    def test_raw_body_is_verbatim(self):
        body = 'SSE event: {"type":"response.created","response":{"id":"resp_1"}}'
        event = parse_log_body(
            log_id=1,
            timestamp=1.0,
            target="codex_api::sse::responses",
            body=body,
        )
        self.assertEqual(event.raw_body, body)
        self.assertEqual(event.response_id, "resp_1")


class RouterTests(unittest.TestCase):
    def test_one_response_one_state(self):
        router = ResponseRouter()
        router.feed_many(fixture_single_response_thinking_tool_answer())
        self.assertEqual(len(router.responses), 1)
        state = router.get("resp_aaa111")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, ResponseStatus.COMPLETED)
        self.assertEqual(state.model, "sol")
        self.assertEqual(state.reasoning_effort, "high")
        items = state.item_list
        self.assertEqual(len(items), 3)
        self.assertEqual([i.kind for i in items], [ItemKind.THINKING, ItemKind.TOOL_CALL, ItemKind.ANSWER])
        self.assertEqual([display_item_number(i.output_index) for i in items], [1, 2, 3])
        self.assertEqual(items[0].assembled_text, "Thinking about shells")
        self.assertEqual(items[1].tool_name, "shell_command")
        self.assertEqual(items[1].call_id, "call_shell_1")
        self.assertEqual(items[1].tool_input, '{"cmd":"ls"}')
        self.assertEqual(items[2].assembled_text, "Done listing files.")
        # Every event accounted for: 11 events across items + other
        total = len(state.other_events) + sum(len(i.events) for i in items)
        self.assertEqual(total, 11)

    def test_interleaved_responses_isolated(self):
        router = ResponseRouter()
        router.feed_many(fixture_two_interleaved_responses())
        self.assertEqual(set(router.responses), {"resp_alpha", "resp_beta"})
        a = router.get("resp_alpha")
        b = router.get("resp_beta")
        assert a and b
        self.assertEqual(a.item_list[0].assembled_text, "A")
        self.assertEqual(b.item_list[0].assembled_text, "B")
        self.assertNotEqual(a.item_list[0].item_id, b.item_list[0].item_id)

    def test_ambiguous_event_stays_unresolved(self):
        router = ResponseRouter()
        router.feed_many(fixture_ambiguous_delta_without_ids())
        self.assertEqual(len(router.unresolved), 1)
        self.assertEqual(router.unresolved[0].event.parsed.get("delta"), "orphan")
        # Must not invent assignment onto either active response
        for state in router.responses.values():
            texts = [i.assembled_text for i in state.item_list]
            self.assertTrue(all("orphan" not in t for t in texts))

    def test_malformed_and_unhandled_retained(self):
        router = ResponseRouter()
        router.feed_many(fixture_malformed_and_unhandled())
        state = router.get("resp_m")
        assert state is not None
        # malformed + completed + created in other or items; unhandled may attach via response_id
        bodies = [e.raw_body for e in state.other_events]
        self.assertTrue(any("not-json" in b for b in bodies) or any("not-json" in e.event.raw_body for e in router.unresolved))
        self.assertEqual(state.status, ResponseStatus.COMPLETED)

    def test_partial_missing_start(self):
        router = ResponseRouter()
        router.feed_many(fixture_partial_missing_start())
        state = router.get("resp_late")
        assert state is not None
        self.assertTrue(state.partial)
        self.assertEqual(state.status, ResponseStatus.COMPLETED)

    def test_function_and_unknown_classification(self):
        router = ResponseRouter()
        router.feed_many(fixture_function_and_unknown_items())
        state = router.get("resp_misc")
        assert state is not None
        kinds = [i.kind for i in state.item_list]
        self.assertEqual(kinds, [ItemKind.TOOL_CALL, ItemKind.OTHER])

    def test_attribution_monotonic_upgrade(self):
        router = ResponseRouter()
        router.feed(
            make_event(1, {"type": "response.created", "response": {"id": "resp_z", "model": "sol"}})
        )
        state = router.get("resp_z")
        assert state is not None
        self.assertEqual(state.attribution.confidence, AttributionConfidence.UNASSIGNED)

        # Inferred via unique process thread while still SSE target
        router.process_threads["p1"].add("thread-1")
        router.feed(
            make_event(
                2,
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "item_z", "type": "message"},
                    "response_id": "resp_z",
                },
                process_uuid="p1",
            )
        )
        self.assertEqual(state.attribution.confidence, AttributionConfidence.INFERRED)
        self.assertEqual(state.agent_thread_id, "thread-1")

        ok = router.confirm_thread("resp_z", "thread-1", log_id=3)
        self.assertTrue(ok)
        self.assertEqual(state.attribution.confidence, AttributionConfidence.CONFIRMED)

        # Conflicting confirmed move is rejected
        ok = router.confirm_thread("resp_z", "thread-other", log_id=4)
        self.assertFalse(ok)
        self.assertEqual(state.agent_thread_id, "thread-1")
        self.assertTrue(state.attribution.conflicts)

    def test_tool_result_join_by_call_id(self):
        router = ResponseRouter()
        router.feed_many(fixture_single_response_thinking_tool_answer())
        rid = router.attach_tool_result(
            "call_shell_1",
            ToolResult(
                call_id="call_shell_1",
                status="completed",
                duration_ms=42,
                exit_code=0,
                output="a.txt\n",
            ),
        )
        self.assertEqual(rid, "resp_aaa111")
        item = router.get("resp_aaa111").item_list[1]
        self.assertIsNotNone(item.tool_result)
        self.assertEqual(item.tool_result.status, "completed")
        self.assertEqual(item.tool_result.output, "a.txt\n")

    def test_failed_incomplete_lifecycle(self):
        router = ResponseRouter()
        router.feed(make_event(1, {"type": "response.created", "response": {"id": "resp_f"}}))
        router.feed(make_event(2, {"type": "response.failed", "response": {"id": "resp_f"}}))
        self.assertEqual(router.get("resp_f").status, ResponseStatus.FAILED)

        router.feed(make_event(3, {"type": "response.created", "response": {"id": "resp_i"}}))
        router.feed(make_event(4, {"type": "response.incomplete", "response": {"id": "resp_i"}}))
        self.assertEqual(router.get("resp_i").status, ResponseStatus.INCOMPLETE)

    def test_item_id_routes_delta_without_response_id(self):
        router = ResponseRouter()
        events = [
            make_event(1, {"type": "response.created", "response": {"id": "resp_a"}}),
            make_event(
                2,
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "item_a1", "type": "message"},
                    "response_id": "resp_a",
                },
            ),
            make_event(
                3,
                {
                    "type": "response.output_text.delta",
                    "item_id": "item_a1",
                    "output_index": 0,
                    "delta": "hello",
                },
            ),
        ]
        router.feed_many(events)
        self.assertEqual(router.get("resp_a").item_list[0].assembled_text, "hello")
        self.assertEqual(len(router.unresolved), 0)

    def test_generation_family_routes_msg_rs_ctc_ids(self):
        """Real Codex ids share a 24-hex family across resp_/msg_/rs_/ctc_."""
        family = "0af8c1b72a17fe43016a57c9"
        rid = f"resp_{family}cb880c8190baad9b0be2799cc1"
        rs = f"rs_{family}cf1c2481908fcfc8e0610ec1b2"
        msg = f"msg_{family}dd1b388196b1b3537b7810c950"
        ctc = f"ctc_{family}aabbccdd1122334455667788"
        router = ResponseRouter()
        events = [
            make_event(1, {"type": "response.created", "response": {"id": rid, "model": "sol"}}),
            make_event(
                2,
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": rs, "type": "reasoning"},
                },
            ),
            make_event(
                3,
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": rs,
                    "output_index": 0,
                    "delta": "plan",
                },
            ),
            make_event(
                4,
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {
                        "id": ctc,
                        "type": "custom_tool_call",
                        "name": "shell_command",
                        "call_id": "call_x",
                    },
                },
            ),
            make_event(
                5,
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": ctc,
                    "output_index": 1,
                    "delta": "{}",
                },
            ),
            make_event(
                6,
                {
                    "type": "response.output_item.added",
                    "output_index": 2,
                    "item": {"id": msg, "type": "message"},
                },
            ),
            make_event(
                7,
                {
                    "type": "response.output_text.delta",
                    "item_id": msg,
                    "output_index": 2,
                    "delta": "hi",
                },
            ),
            make_event(8, {"type": "response.completed", "response": {"id": rid}}),
        ]
        router.feed_many(events)
        state = router.get(rid)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(len(state.item_list), 3)
        self.assertEqual(state.item_list[0].assembled_text, "plan")
        self.assertEqual(state.item_list[1].tool_input, "{}")
        self.assertEqual(state.item_list[2].assembled_text, "hi")
        # Ambiguous concurrent family must not steal — second response different family
        self.assertEqual(len(router.unresolved), 0)

    def test_reasoning_multi_summary_parts_join_with_newlines(self):
        """Codex streams multiple summary_index parts; Text view must keep them all."""
        family = "0fd273cb5288bc0c016a5996"
        rid = f"resp_{family}b9d1808197"
        rs = f"rs_{family}b9d1808197a6c4b8fad6d3e371"
        router = ResponseRouter()
        events = [
            make_event(1, {"type": "response.created", "response": {"id": rid, "model": "sol"}}),
            make_event(
                2,
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": rs, "type": "reasoning", "content": [], "summary": []},
                },
            ),
            make_event(
                3,
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": rs,
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": "**Diagnosing bone transform node LOD impact**",
                },
            ),
            make_event(
                4,
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": rs,
                    "output_index": 0,
                    "summary_index": 0,
                    "text": "**Diagnosing bone transform node LOD impact**",
                },
            ),
            make_event(
                5,
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": rs,
                    "output_index": 0,
                    "summary_index": 1,
                    "delta": "**Planning runtime AnimGraph inspection using UE skill**",
                },
            ),
            make_event(
                6,
                {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": rs,
                    "output_index": 0,
                    "summary_index": 1,
                    "part": {
                        "type": "summary_text",
                        "text": "**Planning runtime AnimGraph inspection using UE skill**",
                    },
                },
            ),
            make_event(
                7,
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": rs,
                        "type": "reasoning",
                        "content": [],
                        "encrypted_content": "gAAAAA-encrypted",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": "**Diagnosing bone transform node LOD impact**",
                            },
                            {
                                "type": "summary_text",
                                "text": "**Planning runtime AnimGraph inspection using UE skill**",
                            },
                            {
                                "type": "summary_text",
                                "text": "**Evaluating UE Python scripting for asset inspection**",
                            },
                        ],
                    },
                },
            ),
        ]
        router.feed_many(events)
        state = router.get(rid)
        self.assertIsNotNone(state)
        assert state is not None
        thinking = state.item_list[0]
        self.assertEqual(
            thinking.assembled_text,
            "**Diagnosing bone transform node LOD impact**\n"
            "**Planning runtime AnimGraph inspection using UE skill**\n"
            "**Evaluating UE Python scripting for asset inspection**",
        )
        self.assertEqual(len(thinking.summary_parts), 3)
        self.assertTrue(thinking.has_encrypted_content)
        self.assertTrue(thinking.done)
        self.assertEqual(len(router.unresolved), 0)


if __name__ == "__main__":
    unittest.main()
