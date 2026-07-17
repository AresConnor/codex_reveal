"""Session browser tree — workspace / root session / agent scopes."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static, Tree
from textual.widgets.tree import TreeNode

from ..models import (
    AgentScope,
    LoadOlderScope,
    Scope,
    SessionMeta,
    SessionScope,
    UnassignedScope,
    WorkspaceScope,
)
from ..sources.rollout import (
    SessionCatalog,
    canonicalize_workspace,
    workspace_label,
)

CST = timezone(timedelta(hours=8))


class SessionTree(Vertical):
    """Tree widget showing workspaces, sessions, and agents."""

    class ScopeSelected(Message):
        def __init__(self, scope: Scope):
            super().__init__()
            self.scope = scope

    # Backward-compat alias used by older app handler during transition
    class SessionSelected(Message):
        def __init__(self, meta: SessionMeta):
            super().__init__()
            self.meta = meta

    def __init__(self, catalog: SessionCatalog | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.catalog = catalog or SessionCatalog()
        self._selected_scope: Scope = None
        self._expanded_workspaces: set[str] = set()
        self._expanded_sessions: set[str] = set()
        self._closed_threads: set[str] = set()
        self._active_threads: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Static("Workspace", classes="panel-title")
        yield Tree("Loading...", id="session-tree")

    def on_mount(self) -> None:
        self.refresh_sessions()

    def set_thread_status(self, thread_id: str, *, active: bool) -> None:
        if active:
            self._active_threads.add(thread_id)
            self._closed_threads.discard(thread_id)
        else:
            self._active_threads.discard(thread_id)
            self._closed_threads.add(thread_id)

    def refresh_sessions(self) -> None:
        """Rescan catalog and rebuild tree while preserving expansion/selection."""
        # Capture expansion from live tree when possible
        self._capture_expansion()
        self.catalog.refresh()
        tree = self.query_one("#session-tree", Tree)
        tree.clear()

        keys = self.catalog.workspace_keys()
        if not keys:
            tree.root.label = "No sessions found"
            tree.root.expand()
            return

        collisions = self.catalog.basename_collisions()
        total_agents = sum(len(g.agents) for gs in self.catalog.workspaces.values() for g in gs)
        tree.root.label = f"Workspaces ({len(keys)} / agents {total_agents})"
        tree.root.expand()

        # Unassigned synthetic node
        un_node = tree.root.add("Unassigned", data=UnassignedScope())
        if isinstance(self._selected_scope, UnassignedScope):
            tree.select_node(un_node)

        now = datetime.now(CST)
        today = now.date()

        for ws_key in keys:
            path = self.catalog.workspace_paths.get(ws_key, ws_key)
            base = (path.rstrip("\\/").split("\\")[-1].split("/")[-1]) if path else ws_key
            collide = base.lower() in collisions
            label = workspace_label(path or ws_key, collide=collide)
            # Strip markup for tree label safety — Tree uses plain/rich mixed; keep simple
            plain_label = base if not collide else f"{base} ({path})"
            ws_node = tree.root.add(plain_label, data=WorkspaceScope(workspace_key=ws_key))
            if ws_key in self._expanded_workspaces:
                ws_node.expand()

            sessions = self.catalog.sessions_for_workspace(ws_key)
            for group in sessions:
                ts = group.timestamp.astimezone(CST) if group.timestamp.tzinfo else group.timestamp.replace(tzinfo=timezone.utc).astimezone(CST)
                status = "active" if group.active or any(a.thread_id in self._active_threads for a in group.agents) else "closed"
                time_label = _session_time_label(ts, today)
                sess_label = f"{time_label} {status}"
                sess_node = ws_node.add(sess_label, data=SessionScope(session_id=group.session_id))
                if group.session_id in self._expanded_sessions or status == "active":
                    sess_node.expand()

                for agent in group.agents:
                    leaf_label = _agent_label(agent, closed=agent.thread_id in self._closed_threads and agent.thread_id not in self._active_threads)
                    leaf = sess_node.add_leaf(leaf_label, data=AgentScope(thread_id=agent.thread_id))
                    # Also stash meta for history loading via catalog
                    leaf.data = AgentScope(thread_id=agent.thread_id)

            if self.catalog.has_older(ws_key):
                ws_node.add_leaf("Load older...", data=LoadOlderScope(workspace_key=ws_key))

            if isinstance(self._selected_scope, WorkspaceScope) and self._selected_scope.workspace_key == ws_key:
                tree.select_node(ws_node)

        # Expand first workspace by default
        if tree.root.children and not self._expanded_workspaces:
            # children[0] may be Unassigned
            for child in tree.root.children:
                if isinstance(child.data, WorkspaceScope):
                    child.expand()
                    break

    def _capture_expansion(self) -> None:
        try:
            tree = self.query_one("#session-tree", Tree)
        except Exception:
            return
        for node in tree.root.children:
            data = node.data
            if isinstance(data, WorkspaceScope) and node.is_expanded:
                self._expanded_workspaces.add(data.workspace_key)
            for child in node.children:
                cdata = child.data
                if isinstance(cdata, SessionScope) and child.is_expanded:
                    self._expanded_sessions.add(cdata.session_id)
                if isinstance(cdata, WorkspaceScope) and child.is_expanded:
                    self._expanded_workspaces.add(cdata.workspace_key)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if data is None:
            return
        if isinstance(data, LoadOlderScope):
            self.catalog.load_older(data.workspace_key)
            self._expanded_workspaces.add(data.workspace_key)
            self.refresh_sessions()
            return
        if isinstance(data, (WorkspaceScope, SessionScope, AgentScope, UnassignedScope)):
            self._selected_scope = data
            self.post_message(self.ScopeSelected(data))
            # History compat: if agent selected, also emit SessionSelected with meta
            if isinstance(data, AgentScope):
                meta = self.catalog.get_thread(data.thread_id)
                if meta is not None:
                    self.post_message(self.SessionSelected(meta))


def _session_time_label(ts: datetime, today) -> str:
    d = ts.date()
    if d == today:
        return ts.strftime("%H:%M")
    yday = today.fromordinal(today.toordinal() - 1)
    if d == yday:
        return f"Yesterday {ts.strftime('%H:%M')}"
    return ts.strftime("%Y-%m-%d %H:%M")


def _agent_label(agent: SessionMeta, *, closed: bool = False) -> str:
    name = agent.agent_nickname or "root"
    role = agent.agent_role or ("" if name == "root" else agent.thread_source)
    if role and name != "root":
        label = f"{name}    {role}"
    else:
        label = name
    return f"· {label}" if closed else label
