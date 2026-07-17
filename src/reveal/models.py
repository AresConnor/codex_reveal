"""Data models for Codex log objects and Live response routing."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any



class AttributionConfidence(IntEnum):
    UNASSIGNED = 0
    INFERRED = 1
    CONFIRMED = 2


class ResponseStatus(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class ItemKind(Enum):
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    ANSWER = "answer"
    OTHER = "other"


class EvidenceKind(Enum):
    NONE = "none"
    OPAQUE_PREFIX = "opaque_prefix"
    STREAM_CORRELATION = "stream_correlation"
    REQUEST_LIFECYCLE = "request_lifecycle"
    ROLLOUT_CALL = "rollout_call"


@dataclass(frozen=True)
class AgentKey:
    thread_id: str


@dataclass(frozen=True)
class WorkspaceScope:
    workspace_key: str


@dataclass(frozen=True)
class SessionScope:
    session_id: str


@dataclass(frozen=True)
class AgentScope:
    thread_id: str


@dataclass(frozen=True)
class LoadOlderScope:
    workspace_key: str


@dataclass(frozen=True)
class UnassignedScope:
    """Synthetic scope for responses without safe agent assignment."""

    pass


Scope = WorkspaceScope | SessionScope | AgentScope | LoadOlderScope | UnassignedScope | None


@dataclass
class SessionMeta:
    """Metadata for a single rollout session/thread."""

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
    thread_id: str = ""
    closed: bool = False

    def __post_init__(self) -> None:
        if not self.thread_id:
            self.thread_id = self.rollout_id or self.session_id

    @property
    def is_root(self) -> bool:
        if self.thread_source == "subagent" and self.parent_thread_id:
            return False
        if self.parent_thread_id and self.parent_thread_id != self.thread_id:
            # Nested thread with parent.
            if self.thread_source == "subagent":
                return False
        return self.thread_id == self.session_id or not self.parent_thread_id


@dataclass
class SessionGroup:
    """A group of sessions sharing the same parent session_id."""

    session_id: str
    timestamp: datetime
    agents: list[SessionMeta] = field(default_factory=list)
    cwd: str = ""
    workspace_key: str = ""
    active: bool = False


@dataclass
class SSEEvent:
    """Compatibility SSE event used by diagnostics and legacy tests."""

    id: int
    timestamp: float  # seconds with nanosecond precision
    level: str
    event_type: str
    raw_body: str
    parsed: dict = field(default_factory=dict)
    target: str = "codex_api::sse::responses"
    process_uuid: str | None = None
    thread_id: str | None = None


@dataclass
class RawLogEvent:
    """One relevant log row after parsing (or failed parse with raw body)."""

    log_id: int
    timestamp: float
    target: str
    process_uuid: str | None
    thread_id: str | None
    event_type: str
    sequence_number: int | None
    response_id: str | None
    item_id: str | None
    output_index: int | None
    raw_body: str
    parsed: dict = field(default_factory=dict)
    level: str = "INFO"

    def to_sse_event(self) -> SSEEvent:
        return SSEEvent(
            id=self.log_id,
            timestamp=self.timestamp,
            level=self.level,
            event_type=self.event_type,
            raw_body=self.raw_body,
            parsed=self.parsed,
            target=self.target,
            process_uuid=self.process_uuid,
            thread_id=self.thread_id,
        )


@dataclass
class AttributionEvidence:
    thread_id: str | None = None
    confidence: AttributionConfidence = AttributionConfidence.UNASSIGNED
    evidence_kind: EvidenceKind = EvidenceKind.NONE
    source_log_ids: list[int] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    detail: str = ""


@dataclass
class ToolResult:
    call_id: str
    status: str  # running | completed | failed | unavailable
    tool_name: str | None = None
    duration_ms: float | None = None
    exit_code: int | None = None
    summary: str = ""
    output: str = ""
    source: str = "rollout"


@dataclass
class ResponseItemState:
    item_id: str
    output_index: int
    kind: ItemKind
    protocol_type: str
    tool_name: str | None = None
    call_id: str | None = None
    events: list[RawLogEvent] = field(default_factory=list)
    assembled_text: str = ""
    # Reasoning summary parts keyed by summary_index (provider plaintext).
    summary_parts: dict[int, str] = field(default_factory=dict)
    has_encrypted_content: bool = False
    tool_input: str = ""
    tool_result: ToolResult | None = None
    done: bool = False


@dataclass
class ResponseState:
    response_id: str
    agent_thread_id: str | None = None
    process_uuid: str | None = None
    attribution: AttributionEvidence = field(default_factory=AttributionEvidence)
    status: ResponseStatus = ResponseStatus.ACTIVE
    model: str = "?"
    reasoning_effort: str | None = None
    started_at: float = 0.0
    ended_at: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    items: OrderedDict[tuple[int, str], ResponseItemState] = field(
        default_factory=OrderedDict
    )
    other_events: list[RawLogEvent] = field(default_factory=list)
    partial: bool = False
    order_key: int = 0  # global creation order (log id of first event)

    @property
    def item_list(self) -> list[ResponseItemState]:
        return list(self.items.values())


@dataclass
class UnresolvedEvent:
    event: RawLogEvent
    reason: str
    candidates: list[str] = field(default_factory=list)


@dataclass
class RoutingMetrics:
    confirmed: int = 0
    inferred: int = 0
    unassigned: int = 0
    unresolved_queue: int = 0
    conflicts: int = 0
    partial: int = 0
    sqlite_lag: int | None = None


@dataclass
class RouterSnapshot:
    """Immutable-ish view of router changes for UI consumption."""

    changed_response_ids: list[str] = field(default_factory=list)
    reassigned: list[tuple[str, str | None, str | None]] = field(
        default_factory=list
    )  # (response_id, old_thread, new_thread)
