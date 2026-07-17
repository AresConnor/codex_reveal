import unittest

from reveal.models import (
    AgentScope,
    AttributionConfidence,
    AttributionEvidence,
    ResponseState,
    SessionScope,
    UnassignedScope,
    WorkspaceScope,
)
from reveal.widgets.response_feed import ResponseFeed


class ResponseFeedScopeTests(unittest.TestCase):
    def _state(self, rid: str, thread: str | None = None) -> ResponseState:
        st = ResponseState(response_id=rid, agent_thread_id=thread)
        if thread is None:
            st.attribution = AttributionEvidence(confidence=AttributionConfidence.UNASSIGNED)
        else:
            st.attribution = AttributionEvidence(
                thread_id=thread,
                confidence=AttributionConfidence.INFERRED,
            )
        return st

    def test_workspace_and_session_are_total_views(self):
        feed = ResponseFeed()
        unassigned = self._state("r0", None)
        a = self._state("r1", "thread-a")
        b = self._state("r2", "thread-b")

        feed.set_scope(None, None)
        self.assertTrue(feed._visible(unassigned))
        self.assertTrue(feed._visible(a))
        self.assertTrue(feed._visible(b))

        feed.set_scope(WorkspaceScope(workspace_key="ws"), {"thread-a"})
        self.assertTrue(feed._visible(unassigned))
        self.assertTrue(feed._visible(a))
        self.assertTrue(feed._visible(b))

        feed.set_scope(SessionScope(session_id="sess"), {"thread-a", "thread-b"})
        self.assertTrue(feed._visible(unassigned))
        self.assertTrue(feed._visible(a))
        self.assertTrue(feed._visible(b))

    def test_agent_scope_filters_to_thread(self):
        feed = ResponseFeed()
        unassigned = self._state("r0", None)
        a = self._state("r1", "thread-a")
        b = self._state("r2", "thread-b")

        feed.set_scope(AgentScope(thread_id="thread-a"), {"thread-a"})
        self.assertFalse(feed._visible(unassigned))
        self.assertTrue(feed._visible(a))
        self.assertFalse(feed._visible(b))

    def test_unassigned_scope_only_unassigned(self):
        feed = ResponseFeed()
        unassigned = self._state("r0", None)
        a = self._state("r1", "thread-a")

        feed.set_scope(UnassignedScope(), set())
        self.assertTrue(feed._visible(unassigned))
        self.assertFalse(feed._visible(a))


if __name__ == "__main__":
    unittest.main()
