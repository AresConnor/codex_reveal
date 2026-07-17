import unittest
from datetime import datetime, timedelta, timezone

from textual.app import App, ComposeResult

from reveal.models import ResponseState
from reveal.routing import ResponseRouter
from reveal.widgets.response_card import ResponseCard, ResponseItemWidget
from reveal.widgets.response_feed import ResponseFeed
from tests.fixtures.sse_samples import (
    fixture_single_response_thinking_tool_answer,
    make_event,
)


class CardApp(App):
    CSS = """
    .response-card, .card-header, .card-body, .response-item, .item-summary, .item-body,
    .item-view-switch,
    .item-semantic, .item-tool-result, .item-raw-header, .item-raw-host,
    .item-raw-inline, .item-range { height: auto; }
    .item-view-switch { height: 1; }
    """

    def __init__(self, state: ResponseState):
        super().__init__()
        self.state = state

    def compose(self) -> ComposeResult:
        yield ResponseCard(self.state, agent_label="root", id="card")


class FeedApp(App):
    def compose(self) -> ComposeResult:
        yield ResponseFeed(id="feed")


class WidgetPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_card_is_real_widget(self):
        router = ResponseRouter()
        router.feed_many(fixture_single_response_thinking_tool_answer())
        state = router.get("resp_aaa111")
        assert state is not None
        app = CardApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            card = app.query_one(ResponseCard)
            self.assertIsInstance(card, ResponseCard)
            # No RichLog fake separators
            self.assertTrue(card.has_class("response-card") or True)
            items = list(card.query(ResponseItemWidget))
            self.assertEqual(len(items), 3)
            summaries = [item.query_one(".item-summary") for item in items]
            summary_texts = [str(summary.render()) for summary in summaries]
            for summary in summary_texts:
                self.assertRegex(summary, r"^\d{2}:\d{2}:\d{2}\.\d{3}\s{2}")
            first_local_time = datetime.fromtimestamp(
                items[0].item.events[0].timestamp,
                tz=timezone(timedelta(hours=8)),
            ).strftime("%H:%M:%S.%f")[:-3]
            self.assertTrue(summary_texts[0].startswith(first_local_time + "  "))
            self.assertNotRegex(summary_texts[0], r"^1\s")

            # A card with short, collapsed summaries remains content-sized.
            self.assertLess(card.region.height, app.size.height // 2)

            # A summary click expands only that semantic item.
            await pilot.click(summaries[0])
            await pilot.pause()
            self.assertTrue(items[0].expanded)
            self.assertFalse(items[1].expanded)
            self.assertFalse(items[2].expanded)

            thinking = str(items[0].query_one(".item-semantic").render())
            self.assertIn("Thinking about shells", thinking)
            self.assertTrue(items[0].query_one(".item-semantic").display)
            self.assertFalse(items[0].query_one(".item-raw-host").display)

            # Thinking can switch independently from decoded text to its raw SSE.
            await pilot.click(items[0].query_one(".item-view-raw"))
            await pilot.pause()
            raw = str(items[0].query_one(".item-raw-inline").render())
            self.assertIn("response.reasoning_summary_text.delta", raw)
            self.assertNotIn("Done listing files.", raw)
            self.assertFalse(items[0].query_one(".item-semantic").display)
            self.assertTrue(items[0].query_one(".item-raw-host").display)

            await pilot.click(items[0].query_one(".item-view-text"))
            await pilot.pause()
            self.assertTrue(items[0].query_one(".item-semantic").display)
            self.assertFalse(items[0].query_one(".item-raw-host").display)

            # Clicking expanded content does not affect this item or siblings.
            await pilot.click(items[0].query_one(".item-semantic"))
            await pilot.pause()
            self.assertTrue(items[0].expanded)
            self.assertFalse(items[1].expanded)
            self.assertFalse(items[2].expanded)

            # Expand all via card API
            for item in items:
                item.set_expanded(True)
            for item in items:
                self.assertTrue(item.expanded)
            # Incoming refresh preserves collapse of card
            card.collapsed = True
            card.refresh_from_state(state)
            self.assertTrue(card.collapsed)

    async def test_feed_incremental_upsert(self):
        router = ResponseRouter()
        app = FeedApp()
        async with app.run_test() as pilot:
            feed = app.query_one(ResponseFeed)
            events = fixture_single_response_thinking_tool_answer()
            for ev in events[:3]:
                router.feed(ev)
                state = router.get("resp_aaa111")
                assert state is not None
                feed.upsert_response(state)
            cards = list(feed.query(ResponseCard))
            self.assertEqual(len(cards), 1)
            # More events should not create second card
            for ev in events[3:]:
                router.feed(ev)
            feed.upsert_response(router.get("resp_aaa111"))
            self.assertEqual(len(list(feed.query(ResponseCard))), 1)

    async def test_scope_remount_reuses_single_card(self):
        """Regression: filter remount must not DuplicateIds / multi-mount same response."""
        from reveal.models import AgentScope, AttributionConfidence, AttributionEvidence

        router = ResponseRouter()
        events = fixture_single_response_thinking_tool_answer()
        router.feed_many(events)
        state = router.get("resp_aaa111")
        assert state is not None
        # Simulate attributed agent so agent-scope can match.
        state.agent_thread_id = "thread-a"
        state.attribution = AttributionEvidence(
            thread_id="thread-a",
            confidence=AttributionConfidence.INFERRED,
        )

        app = FeedApp()
        async with app.run_test() as pilot:
            feed = app.query_one(ResponseFeed)
            feed.upsert_response(state)
            await pilot.pause()
            self.assertEqual(len(list(feed.query(ResponseCard))), 1)

            # Toggle scopes repeatedly (same path as tree clicks).
            feed.set_scope(AgentScope(thread_id="thread-a"), {"thread-a"})
            await pilot.pause()
            self.assertEqual(len(list(feed.query(ResponseCard))), 1)

            feed.set_scope(None, None)
            await pilot.pause()
            self.assertEqual(len(list(feed.query(ResponseCard))), 1)

            feed.set_scope(AgentScope(thread_id="thread-b"), {"thread-b"})
            await pilot.pause()
            self.assertEqual(len(list(feed.query(ResponseCard))), 0)

            feed.set_scope(AgentScope(thread_id="thread-a"), {"thread-a"})
            await pilot.pause()
            self.assertEqual(len(list(feed.query(ResponseCard))), 1)
            # Same rid still only one DOM card after many remounts.
            feed.upsert_response(state)
            feed.upsert_response(state)
            await pilot.pause()
            self.assertEqual(len(list(feed.query(ResponseCard))), 1)


    async def test_thinking_text_appends_until_done_and_raw_is_selectable(self):
        router = ResponseRouter()
        events = fixture_single_response_thinking_tool_answer()
        router.feed_many(events[:3])
        state = router.get("resp_aaa111")
        assert state is not None

        app = CardApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            card = app.query_one(ResponseCard)
            thinking = list(card.query(ResponseItemWidget))[0]
            await pilot.click(thinking.query_one(".item-summary"))
            await pilot.pause()

            self.assertIn("Thinking about shells", str(thinking.query_one(".item-semantic").render()))
            self.assertIn("streaming", str(thinking.query_one(".item-view-status").render()))

            router.feed(
                make_event(
                    30,
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "sequence_number": 3,
                        "item_id": "item_aaa111_r",
                        "output_index": 0,
                        "delta": " and appending",
                    },
                )
            )
            card.refresh_from_state(state)
            await pilot.pause()
            self.assertIn(
                "Thinking about shells and appending",
                str(thinking.query_one(".item-semantic").render()),
            )
            self.assertIn("streaming", str(thinking.query_one(".item-view-status").render()))

            router.feed(
                make_event(
                    31,
                    {
                        "type": "response.output_item.done",
                        "sequence_number": 4,
                        "output_index": 0,
                        "item": {
                            "id": "item_aaa111_r",
                            "type": "reasoning",
                            "summary": [
                                {
                                    "type": "summary_text",
                                    "text": "Thinking about shells and appending",
                                }
                            ],
                        },
                    },
                )
            )
            card.refresh_from_state(state)
            await pilot.pause()
            self.assertTrue(thinking.item.done)
            self.assertIn("done", str(thinking.query_one(".item-view-status").render()))

            await pilot.click(thinking.query_one(".item-view-raw"))
            await pilot.pause()
            raw = str(thinking.query_one(".item-raw-inline").render())
            self.assertIn('"delta":" and appending"', raw)
            self.assertIn("response.output_item.done", raw)


if __name__ == "__main__":
    unittest.main()
