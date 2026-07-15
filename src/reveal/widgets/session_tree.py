"""Session browser tree widget — left sidebar."""

from datetime import datetime, timezone, timedelta
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Tree, Static
from textual.message import Message

from ..sources.rollout import scan_sessions, SessionGroup, SessionMeta

CST = timezone(timedelta(hours=8))


class SessionTree(Vertical):
    """Tree widget showing Codex sessions grouped by parent session and date."""

    class SessionSelected(Message):
        """Posted when the user selects a session."""
        def __init__(self, meta: SessionMeta):
            super().__init__()
            self.meta = meta

    def compose(self) -> ComposeResult:
        yield Static("Sessions", classes="panel-title")
        yield Tree("Loading...", id="session-tree")

    def on_mount(self):
        self.refresh_sessions()

    def refresh_sessions(self):
        """Re-scan sessions directory and rebuild the tree."""
        tree = self.query_one("#session-tree", Tree)
        tree.clear()

        groups = scan_sessions()

        if not groups:
            tree.root.label = "No sessions found"
            tree.root.expand()
            return

        tree.root.label = f"Sessions ({sum(len(g.agents) for g in groups)})"
        tree.root.expand()

        now = datetime.now(CST)
        today = now.date()

        for group in groups:
            ts = group.timestamp.astimezone(CST)
            date_label = _date_label(ts, today)
            date_node = tree.root.add(date_label, expand=True)

            for agent in group.agents:
                label = _agent_label(agent)
                leaf = date_node.add_leaf(label, data=agent)

        # Expand the first group
        if tree.root.children:
            tree.root.children[0].expand()

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        """When user clicks a leaf node, post SessionSelected."""
        if event.node.data is None:
            return
        meta = event.node.data
        if isinstance(meta, SessionMeta):
            self.post_message(self.SessionSelected(meta))


def _date_label(ts: datetime, today) -> str:
    """Human-friendly date label."""
    d = ts.date()
    if d == today:
        return "Today"
    elif d == today.replace(day=today.day - 1):
        return "Yesterday"
    else:
        return ts.strftime("%Y-%m-%d")


def _agent_label(agent: SessionMeta) -> str:
    """Formatted agent label for tree node."""
    name = agent.agent_nickname or "root"
    role = agent.agent_role or agent.thread_source
    ts = agent.timestamp.astimezone(CST).strftime("%H:%M")
    parts = [name]
    if role:
        parts.append(f"[{role}]")
    parts.append(ts)
    return " ".join(parts)
