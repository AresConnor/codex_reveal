"""Rollout JSONL source — scans and reads Codex session rollout files."""

import json
import os
import glob as glob_mod
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta
from typing import Optional

from ..models import SessionMeta, SessionGroup

SESSIONS_DIR = os.path.expandvars(r"%USERPROFILE%\.codex\sessions")

# Timezone for session timestamps (Asia/Shanghai = UTC+8)
CST = timezone(timedelta(hours=8))


def scan_sessions(sessions_dir: str = SESSIONS_DIR) -> list[SessionGroup]:
    """Scan all rollout JSONL files and group by parent session."""
    if not os.path.isdir(sessions_dir):
        return []

    files = glob_mod.glob(
        os.path.join(sessions_dir, "**", "rollout-*.jsonl"),
        recursive=True,
    )

    # Sort by timestamp (newest first) — encoded in filename
    files.sort(reverse=True)

    groups: dict[str, SessionGroup] = {}

    for filepath in files:
        meta = _parse_session_meta(filepath)
        if meta is None:
            continue

        sid = meta.session_id
        if sid not in groups:
            groups[sid] = SessionGroup(
                session_id=sid,
                timestamp=meta.timestamp,
            )
        groups[sid].agents.append(meta)

    # Sort groups newest first
    result = sorted(groups.values(), key=lambda g: g.timestamp, reverse=True)

    # Within each group, sort agents by depth then time
    for g in result:
        g.agents.sort(key=lambda a: (a.depth, a.timestamp))

    return result


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

    # Parse timestamp
    ts_str = payload.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        ts = datetime.min.replace(tzinfo=timezone.utc)

    # Extract subagent info from source
    source = payload.get("source", {})
    subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
    spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}

    return SessionMeta(
        rollout_id=payload.get("id", ""),
        session_id=payload.get("session_id", ""),
        parent_thread_id=payload.get("parent_thread_id", ""),
        timestamp=ts,
        cwd=payload.get("cwd", ""),
        originator=payload.get("originator", ""),
        cli_version=payload.get("cli_version", ""),
        agent_nickname=payload.get("agent_nickname", "") or spawn.get("agent_nickname", ""),
        agent_role=payload.get("agent_role", "") or spawn.get("agent_role", ""),
        agent_path=payload.get("agent_path", "") or spawn.get("agent_path", ""),
        thread_source=payload.get("thread_source", "user"),
        depth=spawn.get("depth", 0),
        file_path=filepath,
    )


def read_rollout(filepath: str) -> list[dict]:
    """Read all events from a rollout JSONL file."""
    events = []
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
