"""Rollout JSONL source — catalog, pagination, incremental tailing."""

from __future__ import annotations

import glob as glob_mod
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Iterator, Optional

from ..models import SessionGroup, SessionMeta, ToolResult

SESSIONS_DIR = os.path.expandvars(r"%USERPROFILE%\.codex\sessions")
CST = timezone(timedelta(hours=8))
DEFAULT_SESSION_PAGE = 20


def canonicalize_workspace(path: str) -> str:
    """Canonicalize Windows path separators and case for identity."""
    if not path:
        return ""
    expanded = os.path.expandvars(path)
    norm = os.path.normpath(expanded)
    # On Windows, case-fold drive/path for identity only.
    return os.path.normcase(norm)


def workspace_label(path: str, *, collide: bool = False) -> str:
    """Primary label is final path component; disambiguate with parent when needed."""
    if not path:
        return "(unknown)"
    base = os.path.basename(path.rstrip("\\/")) or path
    if not collide:
        return base
    parent = os.path.basename(os.path.dirname(path.rstrip("\\/")))
    if parent:
        return f"{base}  [dim]{parent}[/]"
    return base


def scan_sessions(sessions_dir: str = SESSIONS_DIR) -> list[SessionGroup]:
    """Scan rollout JSONL files and group by root session_id."""
    catalog = SessionCatalog(sessions_dir=sessions_dir)
    catalog.refresh()
    return catalog.all_sessions(limit_per_workspace=None)


@dataclass
class SessionCatalog:
    """Indexed workspace/session/agent catalog with per-workspace pagination."""

    sessions_dir: str = SESSIONS_DIR
    page_size: int = DEFAULT_SESSION_PAGE
    # workspace_key -> ordered root sessions (newest first)
    workspaces: dict[str, list[SessionGroup]] = field(default_factory=dict)
    # display path per workspace key
    workspace_paths: dict[str, str] = field(default_factory=dict)
    # thread_id -> SessionMeta
    threads: dict[str, SessionMeta] = field(default_factory=dict)
    # session_id -> SessionGroup
    sessions: dict[str, SessionGroup] = field(default_factory=dict)
    # loaded page count per workspace (1 = first 20)
    loaded_pages: dict[str, int] = field(default_factory=dict)
    # call_id -> ToolResult
    tool_results: dict[str, ToolResult] = field(default_factory=dict)
    # thread_id -> RolloutTailer
    _tailers: dict[str, "RolloutTailer"] = field(default_factory=dict)

    def refresh(self) -> None:
        """Rescan filesystem metadata. Preserves loaded_pages."""
        if not os.path.isdir(self.sessions_dir):
            self.workspaces.clear()
            self.workspace_paths.clear()
            self.threads.clear()
            self.sessions.clear()
            return

        files = glob_mod.glob(
            os.path.join(self.sessions_dir, "**", "rollout-*.jsonl"),
            recursive=True,
        )
        files.sort(reverse=True)

        groups: dict[str, SessionGroup] = {}
        threads: dict[str, SessionMeta] = {}

        for filepath in files:
            meta = _parse_session_meta(filepath)
            if meta is None:
                continue
            threads[meta.thread_id] = meta
            sid = meta.session_id or meta.thread_id
            if sid not in groups:
                groups[sid] = SessionGroup(
                    session_id=sid,
                    timestamp=meta.timestamp,
                    cwd=meta.cwd,
                    workspace_key=canonicalize_workspace(meta.cwd),
                )
            groups[sid].agents.append(meta)
            if meta.cwd and not groups[sid].cwd:
                groups[sid].cwd = meta.cwd
                groups[sid].workspace_key = canonicalize_workspace(meta.cwd)
            # Prefer earliest (root) timestamp for group ordering if older
            if meta.timestamp and meta.timestamp < groups[sid].timestamp:
                groups[sid].timestamp = meta.timestamp

        for g in groups.values():
            # Stable spawn/first-seen order: depth then timestamp then thread_id
            g.agents.sort(key=lambda a: (a.depth, a.timestamp, a.thread_id))
            if g.agents:
                # Mark closed when no agent has been updated recently is not available;
                # closed is left for live status updates. Root first if present.
                pass

        # workspace index
        by_ws: dict[str, list[SessionGroup]] = defaultdict(list)
        paths: dict[str, str] = {}
        for g in groups.values():
            key = g.workspace_key or canonicalize_workspace(g.cwd) or "(unknown)"
            g.workspace_key = key
            by_ws[key].append(g)
            if key not in paths and g.cwd:
                paths[key] = g.cwd

        for key, items in by_ws.items():
            items.sort(key=lambda g: g.timestamp, reverse=True)
            if key not in self.loaded_pages:
                self.loaded_pages[key] = 1

        self.workspaces = dict(by_ws)
        self.workspace_paths = paths
        self.threads = threads
        self.sessions = groups

    def workspace_keys(self) -> list[str]:
        # Sort by newest session timestamp
        def newest(key: str) -> datetime:
            items = self.workspaces.get(key) or []
            if not items:
                return datetime.min.replace(tzinfo=timezone.utc)
            return items[0].timestamp

        return sorted(self.workspaces.keys(), key=newest, reverse=True)

    def basename_collisions(self) -> set[str]:
        counts: dict[str, int] = defaultdict(int)
        for key, path in self.workspace_paths.items():
            base = os.path.basename((path or key).rstrip("\\/")) or key
            counts[base.lower()] += 1
        return {b for b, n in counts.items() if n > 1}

    def sessions_for_workspace(self, workspace_key: str) -> list[SessionGroup]:
        all_items = self.workspaces.get(workspace_key) or []
        pages = self.loaded_pages.get(workspace_key, 1)
        limit = pages * self.page_size
        # Active sessions always shown even if outside page window
        page = all_items[:limit]
        active_extra = [g for g in all_items[limit:] if g.active]
        # Preserve newest-first order
        seen = {g.session_id for g in page}
        for g in active_extra:
            if g.session_id not in seen:
                page.append(g)
        return page

    def has_older(self, workspace_key: str) -> bool:
        all_items = self.workspaces.get(workspace_key) or []
        pages = self.loaded_pages.get(workspace_key, 1)
        return len(all_items) > pages * self.page_size

    def load_older(self, workspace_key: str) -> bool:
        if not self.has_older(workspace_key):
            return False
        self.loaded_pages[workspace_key] = self.loaded_pages.get(workspace_key, 1) + 1
        return True

    def all_sessions(self, limit_per_workspace: int | None = DEFAULT_SESSION_PAGE) -> list[SessionGroup]:
        result: list[SessionGroup] = []
        for key in self.workspace_keys():
            items = self.workspaces.get(key) or []
            if limit_per_workspace is None:
                result.extend(items)
            else:
                pages = self.loaded_pages.get(key, 1)
                result.extend(items[: pages * (limit_per_workspace // self.page_size or 1) * self.page_size])
        result.sort(key=lambda g: g.timestamp, reverse=True)
        return result

    def thread_ids_for_scope(self, scope) -> set[str] | None:
        """Return thread ids for a scope. None means all threads."""
        from ..models import (
            AgentScope,
            SessionScope,
            UnassignedScope,
            WorkspaceScope,
        )

        if scope is None:
            return None
        if isinstance(scope, UnassignedScope):
            return set()  # special: unassigned only
        if isinstance(scope, AgentScope):
            return {scope.thread_id}
        if isinstance(scope, SessionScope):
            g = self.sessions.get(scope.session_id)
            if not g:
                return set()
            return {a.thread_id for a in g.agents}
        if isinstance(scope, WorkspaceScope):
            ids: set[str] = set()
            for g in self.workspaces.get(scope.workspace_key) or []:
                ids.update(a.thread_id for a in g.agents)
            return ids
        return None

    def get_thread(self, thread_id: str) -> SessionMeta | None:
        return self.threads.get(thread_id)

    def tailer_for(self, thread_id: str) -> "RolloutTailer | None":
        meta = self.threads.get(thread_id)
        if not meta or not meta.file_path:
            return None
        tailer = self._tailers.get(thread_id)
        if tailer is None or tailer.path != meta.file_path:
            tailer = RolloutTailer(meta.file_path)
            self._tailers[thread_id] = tailer
        return tailer

    def poll_tool_results(self, thread_ids: Iterable[str] | None = None) -> list[ToolResult]:
        """Incrementally tail rollouts and update call_id index."""
        updated: list[ToolResult] = []
        targets = (
            list(thread_ids)
            if thread_ids is not None
            else list(self.threads.keys())
        )
        for tid in targets:
            tailer = self.tailer_for(tid)
            if not tailer:
                continue
            for result in tailer.poll_tools():
                self.tool_results[result.call_id] = result
                updated.append(result)
        return updated


# Fix missing import used above
from collections.abc import Iterable  # noqa: E402


def _parse_session_meta(filepath: str) -> Optional[SessionMeta]:
    """Parse the first line of a rollout JSONL file to extract session metadata."""
    try:
        with open(filepath, encoding="utf-8") as f:
            first_line = f.readline().strip()
        if not first_line:
            return None
        data = json.loads(first_line)
    except (OSError, json.JSONDecodeError):
        return None

    if data.get("type") != "session_meta":
        return None

    payload = data.get("payload", {})

    ts_str = payload.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        ts = datetime.min.replace(tzinfo=timezone.utc)

    source = payload.get("source", {})
    subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
    spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}

    rollout_id = payload.get("id", "") or ""
    session_id = payload.get("session_id", "") or rollout_id
    parent_thread_id = payload.get("parent_thread_id", "") or ""
    thread_source = payload.get("thread_source", "user") or "user"

    return SessionMeta(
        rollout_id=rollout_id,
        session_id=session_id,
        parent_thread_id=parent_thread_id,
        timestamp=ts,
        cwd=payload.get("cwd", "") or "",
        originator=payload.get("originator", "") or "",
        cli_version=payload.get("cli_version", "") or "",
        agent_nickname=payload.get("agent_nickname", "") or spawn.get("agent_nickname", "") or "",
        agent_role=payload.get("agent_role", "") or spawn.get("agent_role", "") or "",
        agent_path=payload.get("agent_path", "") or spawn.get("agent_path", "") or "",
        thread_source=thread_source,
        depth=int(spawn.get("depth", 0) or 0),
        file_path=filepath,
        thread_id=rollout_id or session_id,
    )


def read_rollout(filepath: str) -> list[dict]:
    """Read all events from a rollout JSONL file."""
    events: list[dict] = []
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


@dataclass
class RolloutTailer:
    """Incremental JSONL tailer by byte offset."""

    path: str
    offset: int = 0
    _carry: str = ""
    _invocations: dict[str, dict[str, Any]] = field(default_factory=dict)
    _outputs: dict[str, ToolResult] = field(default_factory=dict)
    size_signature: int = 0

    def poll_tools(self) -> list[ToolResult]:
        results: list[ToolResult] = []
        try:
            st = os.stat(self.path)
        except OSError:
            return results

        # Truncation / rotation
        if st.st_size < self.offset:
            self.offset = 0
            self._carry = ""

        if st.st_size == self.offset and not self._carry:
            return results

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return results

        data = self._carry + chunk
        if not data:
            return results

        lines = data.split("\n")
        # If file doesn't end with newline, last line may be partial
        if not data.endswith("\n"):
            self._carry = lines[-1]
            lines = lines[:-1]
        else:
            self._carry = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Partial/invalid trailing JSON — retry next pass by restoring carry
                # Only re-carry if this was the final logical line situation; for
                # mid-file invalid lines, skip (corrupt history).
                continue
            for result in self._extract_tool_events(obj):
                results.append(result)
        return results

    def _extract_tool_events(self, obj: dict[str, Any]) -> list[ToolResult]:
        out: list[ToolResult] = []
        typ = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj

        # Invocation forms
        if typ == "response_item":
            ptype = payload.get("type")
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and ptype in (
                "custom_tool_call",
                "function_call",
                "tool_call",
            ):
                self._invocations[call_id] = payload
                if call_id not in self._outputs:
                    result = ToolResult(
                        call_id=call_id,
                        status="running",
                        tool_name=payload.get("name") if isinstance(payload.get("name"), str) else None,
                        summary="running",
                    )
                    self._outputs[call_id] = result
                    out.append(result)
            return out

        if typ == "event_msg":
            ptype = payload.get("type")
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                return out
            if ptype in ("tool_result", "exec_command_end", "tool_output", "function_call_output"):
                status = str(payload.get("status") or "completed")
                if payload.get("success") is False:
                    status = "failed"
                output = payload.get("output") or payload.get("content") or ""
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                duration = payload.get("duration_ms")
                try:
                    duration_ms = float(duration) if duration is not None else None
                except (TypeError, ValueError):
                    duration_ms = None
                exit_code = payload.get("exit_code")
                try:
                    exit_code_i = int(exit_code) if exit_code is not None else None
                except (TypeError, ValueError):
                    exit_code_i = None
                inv = self._invocations.get(call_id) or {}
                result = ToolResult(
                    call_id=call_id,
                    status=status,
                    tool_name=inv.get("name") if isinstance(inv.get("name"), str) else payload.get("name"),
                    duration_ms=duration_ms,
                    exit_code=exit_code_i,
                    summary=status,
                    output=output,
                    source="rollout",
                )
                self._outputs[call_id] = result
                out.append(result)
        return out
