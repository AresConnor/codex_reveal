"""Data models for Codex log objects."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SessionMeta:
    """Metadata for a single rollout session."""
    rollout_id: str
    session_id: str
    parent_thread_id: str
    timestamp: datetime
    cwd: str
    originator: str
    cli_version: str
    agent_nickname: str = ""
    agent_role: str = ""
    agent_path: str = ""
    thread_source: str = "user"
    depth: int = 0
    file_path: str = ""


@dataclass
class SessionGroup:
    """A group of sessions sharing the same parent session_id."""
    session_id: str
    timestamp: datetime
    agents: list[SessionMeta] = field(default_factory=list)


@dataclass
class SSEEvent:
    """A single SSE event from the logs database."""
    id: int
    timestamp: int
    level: str
    event_type: str
    raw_body: str
    parsed: dict = field(default_factory=dict)
