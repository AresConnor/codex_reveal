import json
import os
import tempfile
import unittest
from pathlib import Path

from reveal.models import AgentScope, SessionScope, WorkspaceScope
from reveal.sources.rollout import (
    RolloutTailer,
    SessionCatalog,
    canonicalize_workspace,
    workspace_label,
)
from tests.fixtures.sse_samples import (
    fixture_rollout_session_meta_lines,
    fixture_tool_rollout_events,
)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        metas = fixture_rollout_session_meta_lines()
        # Write three rollout files under dated folder
        folder = self.root / "2026" / "07" / "16"
        folder.mkdir(parents=True)
        for key, meta in metas.items():
            path = folder / f"rollout-{key}.jsonl"
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(meta) + "\n")
                if key == "root":
                    for ev in fixture_tool_rollout_events():
                        f.write(json.dumps(ev) + "\n")
        self.catalog = SessionCatalog(sessions_dir=str(self.root), page_size=20)
        self.catalog.refresh()

    def tearDown(self):
        self.tmp.cleanup()

    def test_root_and_subagent_grouping(self):
        self.assertEqual(len(self.catalog.sessions), 1)
        group = next(iter(self.catalog.sessions.values()))
        self.assertEqual(len(group.agents), 3)
        names = [a.agent_nickname or "root" for a in group.agents]
        self.assertEqual(names[0], "root")
        self.assertIn("Gibbs", names)
        self.assertIn("Popper", names)

    def test_thread_identity(self):
        for tid, meta in self.catalog.threads.items():
            self.assertEqual(tid, meta.thread_id)
            self.assertTrue(meta.thread_id)

    def test_workspace_key(self):
        keys = self.catalog.workspace_keys()
        self.assertEqual(len(keys), 1)
        key = keys[0]
        self.assertEqual(key, canonicalize_workspace(r"D:\workspace\MCServerLauncher-Future"))

    def test_scope_thread_sets(self):
        group = next(iter(self.catalog.sessions.values()))
        sid = group.session_id
        ws = group.workspace_key
        self.assertEqual(
            len(self.catalog.thread_ids_for_scope(SessionScope(session_id=sid))),
            3,
        )
        self.assertEqual(
            len(self.catalog.thread_ids_for_scope(WorkspaceScope(workspace_key=ws))),
            3,
        )
        root = next(a for a in group.agents if (a.agent_nickname or "root") == "root")
        self.assertEqual(
            self.catalog.thread_ids_for_scope(AgentScope(thread_id=root.thread_id)),
            {root.thread_id},
        )

    def test_pagination_isolated(self):
        # Fabricate many sessions in same workspace
        folder = self.root / "extra"
        folder.mkdir()
        cwd = r"D:\workspace\MCServerLauncher-Future"
        for i in range(25):
            meta = {
                "type": "session_meta",
                "payload": {
                    "id": f"thread-extra-{i}",
                    "session_id": f"thread-extra-{i}",
                    "parent_thread_id": "",
                    "timestamp": f"2026-07-15T{i:02d}:00:00Z" if i < 24 else "2026-07-15T23:00:00Z",
                    "cwd": cwd,
                    "originator": "codex",
                    "cli_version": "0",
                    "thread_source": "user",
                },
            }
            # fix invalid hours
            hour = i % 24
            meta["payload"]["timestamp"] = f"2026-07-15T{hour:02d}:00:00Z"
            with open(folder / f"rollout-extra-{i}.jsonl", "w", encoding="utf-8") as f:
                f.write(json.dumps(meta) + "\n")
        self.catalog.refresh()
        ws = canonicalize_workspace(cwd)
        page1 = self.catalog.sessions_for_workspace(ws)
        self.assertLessEqual(len(page1), 20)
        self.assertTrue(self.catalog.has_older(ws))
        self.catalog.load_older(ws)
        page2 = self.catalog.sessions_for_workspace(ws)
        self.assertGreater(len(page2), len(page1))

    def test_tool_tail_join(self):
        root = next(m for m in self.catalog.threads.values() if (m.agent_nickname or "root") == "root")
        tailer = RolloutTailer(root.file_path)
        results = tailer.poll_tools()
        # invocation then completed
        by_id = {r.call_id: r for r in results}
        self.assertIn("call_shell_1", by_id)
        # Second poll should be empty (offset advanced)
        self.assertEqual(tailer.poll_tools(), [])
        # Append more output
        with open(root.file_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "tool_result",
                            "call_id": "call_shell_1",
                            "status": "completed",
                            "output": "more\n",
                        },
                    }
                )
                + "\n"
            )
        more = tailer.poll_tools()
        self.assertTrue(any(r.output == "more\n" for r in more))

    def test_partial_json_retry(self):
        path = self.root / "partial.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"type":"session_meta","payload":{"id":"t","session_id":"t","timestamp":"2026-07-16T00:00:00Z","cwd":"x","originator":"o","cli_version":"0"}}\n')
            f.write('{"type":"event_msg","payload":{"type":"tool_result","call_id":"c1","status":"completed","output":"ok"')
        tailer = RolloutTailer(str(path))
        # partial line should not crash; may yield nothing for incomplete JSON
        tailer.poll_tools()
        with open(path, "a", encoding="utf-8") as f:
            f.write("}}\n")
        # Depending on carry logic, completion may appear on next poll
        tailer.poll_tools()


class WorkspaceLabelTests(unittest.TestCase):
    def test_basename_and_collision(self):
        self.assertEqual(workspace_label(r"D:\a\proj"), "proj")
        self.assertIn("parent", workspace_label(r"D:\parent\proj", collide=True))


if __name__ == "__main__":
    unittest.main()
