"""Pure response/item routing and attribution state machine.

No Textual imports. All Live SSE ingestion funnels through ResponseRouter
before widgets are touched.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable

from .models import (
    AttributionConfidence,
    EvidenceKind,
    ItemKind,
    RawLogEvent,
    ResponseItemState,
    ResponseState,
    ResponseStatus,
    RouterSnapshot,
    RoutingMetrics,
    ToolResult,
    UnresolvedEvent,
)

# Observed but non-contractual: item/response ids often share a generation family.
# Real shapes: resp_<hex>, msg_<hex>, rs_<hex>, ctc_<hex>, item_<hex>
_ID_KIND_RE = re.compile(r"^(?P<kind>[A-Za-z]+)_+(?P<rest>.+)$")
_HEX_RUN_RE = re.compile(r"^[0-9a-fA-F]{16,}")
_FAMILY_LEN = 24

# Hard cap: events that never get a safe assignment must not grow without bound.
# Long-running Live sessions previously froze the UI because feed() re-scanned
# the entire unresolved list on every event (O(events × unresolved × responses)).
MAX_UNRESOLVED = 5000



def classify_item(protocol_type: str) -> ItemKind:
    t = (protocol_type or "").lower()
    if t == "reasoning":
        return ItemKind.THINKING
    if t in ("custom_tool_call", "function_call"):
        return ItemKind.TOOL_CALL
    if t == "message":
        return ItemKind.ANSWER
    return ItemKind.OTHER


def extract_response_id(parsed: dict[str, Any]) -> str | None:
    response = parsed.get("response")
    if isinstance(response, dict):
        rid = response.get("id")
        if isinstance(rid, str) and rid:
            return rid
    rid = parsed.get("response_id")
    if isinstance(rid, str) and rid:
        return rid
    return None


def extract_item_fields(parsed: dict[str, Any]) -> tuple[str | None, int | None, dict]:
    item = parsed.get("item")
    if not isinstance(item, dict):
        item = {}
    item_id = parsed.get("item_id") or item.get("id")
    if not isinstance(item_id, str) or not item_id:
        item_id = None
    output_index = parsed.get("output_index")
    if output_index is None:
        output_index = item.get("output_index")
    if output_index is not None:
        try:
            output_index = int(output_index)
        except (TypeError, ValueError):
            output_index = None
    return item_id, output_index, item


def parse_log_body(
    *,
    log_id: int,
    timestamp: float,
    target: str,
    body: str,
    level: str = "INFO",
    process_uuid: str | None = None,
    thread_id: str | None = None,
) -> RawLogEvent:
    """Parse a relevant log row into a RawLogEvent without mutating body text."""
    raw_body = body if body is not None else ""
    parsed: dict[str, Any] = {}
    event_type = "other"
    sequence_number = None
    response_id = None
    item_id = None
    output_index = None

    if raw_body.startswith("SSE event:"):
        payload = raw_body[len("SSE event:") :].lstrip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {}
            event_type = "malformed_sse"
        else:
            event_type = str(parsed.get("type") or "unknown")
            sequence_number = parsed.get("sequence_number")
            if sequence_number is not None:
                try:
                    sequence_number = int(sequence_number)
                except (TypeError, ValueError):
                    sequence_number = None
            response_id = extract_response_id(parsed)
            item_id, output_index, _item = extract_item_fields(parsed)
            if response_id is None and item_id is None and output_index is None:
                # Keep non-standard but relevant SSE-ish rows inspectable.
                pass
    elif raw_body.startswith("unhandled responses event:"):
        event_type = "unhandled_responses_event"
        # Keep body verbatim; try to pull a type token if present.
        rest = raw_body[len("unhandled responses event:") :].strip()
        try:
            parsed = json.loads(rest)
            event_type = str(parsed.get("type") or event_type)
            response_id = extract_response_id(parsed)
            item_id, output_index, _item = extract_item_fields(parsed)
        except json.JSONDecodeError:
            parsed = {"raw": rest}
    else:
        event_type = "non_sse"
        # Optional: thread-bound request/client rows may be plain text/json.
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                response_id = extract_response_id(parsed)
                item_id, output_index, _item = extract_item_fields(parsed)
                event_type = str(parsed.get("type") or event_type)
        except json.JSONDecodeError:
            parsed = {}

    return RawLogEvent(
        log_id=log_id,
        timestamp=timestamp,
        target=target,
        process_uuid=process_uuid,
        thread_id=thread_id,
        event_type=event_type,
        sequence_number=sequence_number,
        response_id=response_id,
        item_id=item_id,
        output_index=output_index,
        raw_body=raw_body,
        parsed=parsed if isinstance(parsed, dict) else {},
        level=level,
    )


def generation_key(value: str | None) -> str | None:
    """Extract shared generation family token from opaque OpenAI-style ids.

    Real Codex ids share a 24-hex family across kinds:
    ``resp_`` / ``msg_`` / ``rs_`` / ``ctc_`` / ``item_``.

    Short fixture ids like ``resp_aaa111`` / ``item_aaa111_r`` share ``aaa111``.
    """
    if not value or not isinstance(value, str):
        return None
    rest = value
    m = _ID_KIND_RE.match(value)
    if m:
        rest = m.group("rest")
    hm = _HEX_RUN_RE.match(rest)
    if hm:
        hexpart = hm.group(0).lower()
        if len(hexpart) >= _FAMILY_LEN:
            return hexpart[:_FAMILY_LEN]
        if len(hexpart) >= 8:
            return hexpart
    token = rest.split("_", 1)[0].lower()
    if len(token) >= 3 and re.fullmatch(r"[0-9a-z]+", token):
        return token
    return None


def _opaque_prefix(value: str | None) -> str | None:
    """Backward-compatible alias for generation_key."""
    return generation_key(value)


@dataclass
class ResponseRouter:
    """Routes RawLogEvents into ResponseState objects with attribution."""

    responses: OrderedDict[str, ResponseState] = field(default_factory=OrderedDict)
    item_index: dict[str, str] = field(default_factory=dict)  # item_id -> response_id
    family_index: dict[str, str] = field(default_factory=dict)  # generation family -> response_id
    unresolved: list[UnresolvedEvent] = field(default_factory=list)
    # process_uuid -> recent response_ids (supporting evidence only)
    process_responses: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    # response_id -> next expected sequence (supporting only)
    next_sequence: dict[str, int] = field(default_factory=dict)
    # call_id -> response_id for tool join
    call_index: dict[str, str] = field(default_factory=dict)
    # thread-bound request evidence: process_uuid -> thread_id candidates
    process_threads: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _order_counter: int = 0
    _synthetic_seq: int = 0

    def metrics(self) -> RoutingMetrics:
        confirmed = inferred = unassigned = partial = conflicts = 0
        for r in self.responses.values():
            if r.partial:
                partial += 1
            conf = r.attribution.confidence
            if conf == AttributionConfidence.CONFIRMED:
                confirmed += 1
            elif conf == AttributionConfidence.INFERRED:
                inferred += 1
            else:
                unassigned += 1
            conflicts += len(r.attribution.conflicts)
        return RoutingMetrics(
            confirmed=confirmed,
            inferred=inferred,
            unassigned=unassigned,
            unresolved_queue=len(self.unresolved),
            conflicts=conflicts,
            partial=partial,
        )

    def get(self, response_id: str) -> ResponseState | None:
        return self.responses.get(response_id)

    def all_responses(self) -> list[ResponseState]:
        return list(self.responses.values())

    def feed_many(self, events: Iterable[RawLogEvent]) -> RouterSnapshot:
        changed: list[str] = []
        reassigned: list[tuple[str, str | None, str | None]] = []
        for event in events:
            # Defer unresolved drain to once-per-batch (not once-per-event).
            snap = self.feed(event, retry_unresolved=False)
            changed.extend(snap.changed_response_ids)
            reassigned.extend(snap.reassigned)
        drained = self._retry_unresolved()
        changed.extend(drained)
        # de-dupe preserve order
        seen: set[str] = set()
        ordered_changed: list[str] = []
        for rid in changed:
            if rid not in seen:
                seen.add(rid)
                ordered_changed.append(rid)
        return RouterSnapshot(changed_response_ids=ordered_changed, reassigned=reassigned)

    def feed(self, event: RawLogEvent, *, retry_unresolved: bool = True) -> RouterSnapshot:
        # Lifecycle/request evidence may carry thread ids without being SSE items.
        if event.thread_id and event.process_uuid:
            self.process_threads[event.process_uuid].add(event.thread_id)

        response_id = self._resolve_response_id(event)
        if response_id is None:
            self._append_unresolved(event, reason="no_safe_response_assignment")
            drained = self._retry_unresolved() if retry_unresolved else []
            return RouterSnapshot(changed_response_ids=drained)

        created = response_id not in self.responses
        state = self._ensure_response(response_id, event)
        old_thread = state.agent_thread_id

        self._apply_event(state, event)
        self._maybe_attribute(state, event)

        reassigned: list[tuple[str, str | None, str | None]] = []
        if state.agent_thread_id != old_thread:
            reassigned.append((response_id, old_thread, state.agent_thread_id))

        if event.process_uuid:
            bucket = self.process_responses[event.process_uuid]
            if response_id not in bucket:
                bucket.append(response_id)

        drained = self._retry_unresolved() if retry_unresolved else []
        changed = [response_id] + drained
        # de-dupe
        out: list[str] = []
        seen: set[str] = set()
        for rid in changed:
            if rid not in seen:
                seen.add(rid)
                out.append(rid)
        if created:
            # created already in out via response_id
            pass
        return RouterSnapshot(changed_response_ids=out, reassigned=reassigned)


    def attach_tool_result(self, call_id: str, result: ToolResult) -> str | None:
        rid = self.call_index.get(call_id)
        if not rid:
            # Search items (rare path after late join registration)
            for response in self.responses.values():
                for item in response.items.values():
                    if item.call_id == call_id:
                        item.tool_result = result
                        self.call_index[call_id] = response.response_id
                        # Confirmed attribution via rollout call if known
                        return response.response_id
            return None
        state = self.responses.get(rid)
        if not state:
            return None
        for item in state.items.values():
            if item.call_id == call_id:
                item.tool_result = result
                break
        return rid

    def confirm_thread(self, response_id: str, thread_id: str, *, log_id: int | None = None, kind: EvidenceKind = EvidenceKind.REQUEST_LIFECYCLE) -> bool:
        state = self.responses.get(response_id)
        if not state:
            return False
        return self._upgrade_attribution(
            state,
            thread_id=thread_id,
            confidence=AttributionConfidence.CONFIRMED,
            evidence_kind=kind,
            log_id=log_id,
            detail="confirmed",
        )

    def mark_partial_missing_start(self, response_id: str) -> None:
        state = self.responses.get(response_id)
        if state:
            state.partial = True

    def _resolve_response_id(self, event: RawLogEvent) -> str | None:
        if event.response_id:
            return event.response_id

        if event.item_id and event.item_id in self.item_index:
            return self.item_index[event.item_id]

        # Do NOT assign based solely on single active response.
        # Generation-family match routes item streams to their response when unique.
        # O(1) via family_index only — never scan all responses (that froze Live UI).
        candidates: list[str] = []
        for token in (event.item_id, event.response_id):
            key = generation_key(token)
            if key and key in self.family_index:
                rid = self.family_index[key]
                if rid not in candidates:
                    candidates.append(rid)
        if len(candidates) == 1:
            return candidates[0]

        return None


    def _ensure_response(self, response_id: str, event: RawLogEvent) -> ResponseState:
        state = self.responses.get(response_id)
        if state is not None:
            key = generation_key(response_id)
            if key and key not in self.family_index:
                self.family_index[key] = response_id
            return state
        self._order_counter += 1
        state = ResponseState(
            response_id=response_id,
            started_at=event.timestamp,
            order_key=event.log_id or self._order_counter,
            partial=event.event_type != "response.created",
        )
        key = generation_key(response_id)
        if key:
            self.family_index.setdefault(key, response_id)
        if event.thread_id:
            # Direct thread on the event is strong enough for confirmed only if
            # it is a lifecycle/request path; for SSE rows treat as inferred until
            # stronger evidence arrives. SSE rows rarely include thread_id.
            self._upgrade_attribution(
                state,
                thread_id=event.thread_id,
                confidence=AttributionConfidence.CONFIRMED
                if event.target != "codex_api::sse::responses"
                else AttributionConfidence.INFERRED,
                evidence_kind=EvidenceKind.REQUEST_LIFECYCLE
                if event.target != "codex_api::sse::responses"
                else EvidenceKind.STREAM_CORRELATION,
                log_id=event.log_id,
                detail="event.thread_id",
            )
        self.responses[response_id] = state
        return state

    def _apply_event(self, state: ResponseState, event: RawLogEvent) -> None:
        typ = event.event_type
        parsed = event.parsed

        if typ == "response.created":
            response = parsed.get("response") or {}
            state.model = str(response.get("model", state.model) or state.model).replace(
                "gpt-", ""
            )
            reasoning = response.get("reasoning") or {}
            if isinstance(reasoning, dict):
                effort = reasoning.get("effort")
                if effort is not None:
                    state.reasoning_effort = str(effort)
            state.status = ResponseStatus.ACTIVE
            state.started_at = event.timestamp or state.started_at
            state.partial = False
            state.other_events.append(event)
            if event.sequence_number is not None:
                self.next_sequence[state.response_id] = event.sequence_number + 1
            return

        if typ in ("response.completed", "response.failed", "response.incomplete"):
            response = parsed.get("response") or {}
            if isinstance(response, dict):
                state.usage = response.get("usage") or state.usage
                if response.get("model"):
                    state.model = str(response.get("model")).replace("gpt-", "")
            state.ended_at = event.timestamp
            if typ == "response.completed":
                state.status = ResponseStatus.COMPLETED
            elif typ == "response.failed":
                state.status = ResponseStatus.FAILED
            else:
                state.status = ResponseStatus.INCOMPLETE
            state.other_events.append(event)
            return

        item_id, output_index, item = extract_item_fields(parsed)
        if item_id is None and output_index is None:
            state.other_events.append(event)
            return

        # Ensure item id for indexing
        if item_id is None:
            item_id = f"anon-{state.response_id}-{output_index if output_index is not None else 'x'}-{event.log_id}"

        if output_index is None:
            # Place unknown index after existing items
            output_index = max((idx for idx, _ in state.items.keys()), default=-1) + 1

        key = (output_index, item_id)
        item_state = state.items.get(key)
        if item_state is None:
            # Maybe same item_id different key — search by item_id
            for k, existing in list(state.items.items()):
                if existing.item_id == item_id:
                    item_state = existing
                    key = k
                    break

        if item_state is None:
            protocol_type = str(item.get("type") or self._infer_protocol_type(typ) or "other")
            item_state = ResponseItemState(
                item_id=item_id,
                output_index=output_index,
                kind=classify_item(protocol_type),
                protocol_type=protocol_type,
                tool_name=item.get("name") if isinstance(item.get("name"), str) else None,
                call_id=item.get("call_id") if isinstance(item.get("call_id"), str) else None,
            )
            state.items[key] = item_state
            # Keep items ordered by output_index
            state.items = OrderedDict(sorted(state.items.items(), key=lambda kv: kv[0][0]))

        self.item_index[item_id] = state.response_id
        if item_state.call_id is None and isinstance(item.get("call_id"), str):
            item_state.call_id = item.get("call_id")
        if item_state.tool_name is None and isinstance(item.get("name"), str):
            item_state.tool_name = item.get("name")
        if item_state.call_id:
            self.call_index[item_state.call_id] = state.response_id

        if typ in ("response.output_item.added", "response.output_item.done"):
            protocol_type = str(item.get("type") or item_state.protocol_type)
            item_state.protocol_type = protocol_type
            item_state.kind = classify_item(protocol_type)
            if isinstance(item.get("encrypted_content"), str) and item.get("encrypted_content"):
                item_state.has_encrypted_content = True
            if typ == "response.output_item.done":
                item_state.done = True
                # Prefer final assembled fields from done payload when present
                if item_state.kind == ItemKind.TOOL_CALL:
                    args = item.get("input") or item.get("arguments")
                    if isinstance(args, str) and args and not item_state.tool_input:
                        item_state.tool_input = args
                if item_state.kind == ItemKind.THINKING:
                    # Final reasoning item is authoritative: rebuild multi-part summary
                    # (and any rare plaintext content blocks) with stable separators.
                    text = self._extract_reasoning_text(item)
                    if text:
                        item_state.assembled_text = text
                        item_state.summary_parts = self._summary_parts_from_item(item)
                else:
                    text = self._extract_message_text(item)
                    if text and not item_state.assembled_text:
                        item_state.assembled_text = text

        # Reasoning summary part / text completion (no delta field)
        if item_state.kind == ItemKind.THINKING:
            self._apply_reasoning_summary_event(item_state, typ, parsed)

        # Delta assembly
        delta = parsed.get("delta")
        if isinstance(delta, str) and delta:
            if "tool_call_input" in typ or "function_call_arguments" in typ:
                item_state.tool_input += delta
            elif item_state.kind == ItemKind.THINKING or "reasoning" in typ:
                idx = self._summary_index(parsed)
                item_state.summary_parts[idx] = item_state.summary_parts.get(idx, "") + delta
                item_state.assembled_text = self._join_summary_parts(item_state.summary_parts)
            else:
                item_state.assembled_text += delta

        item_state.events.append(event)

        if event.sequence_number is not None:
            self.next_sequence[state.response_id] = event.sequence_number + 1

    def _infer_protocol_type(self, event_type: str) -> str | None:
        if "reasoning" in event_type:
            return "reasoning"
        if "function_call" in event_type or "tool_call" in event_type:
            return "custom_tool_call"
        if "output_text" in event_type or event_type.endswith("message"):
            return "message"
        return None

    @staticmethod
    def _summary_index(parsed: dict[str, Any]) -> int:
        idx = parsed.get("summary_index")
        try:
            return int(idx) if idx is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _join_summary_parts(parts: dict[int, str]) -> str:
        if not parts:
            return ""
        return "\n".join(parts[i] for i in sorted(parts) if parts[i])

    def _apply_reasoning_summary_event(
        self,
        item_state: ResponseItemState,
        typ: str,
        parsed: dict[str, Any],
    ) -> None:
        """Fold reasoning summary part/text.done events into summary_parts."""
        if typ == "response.reasoning_summary_text.done":
            text = parsed.get("text")
            if isinstance(text, str) and text:
                item_state.summary_parts[self._summary_index(parsed)] = text
                item_state.assembled_text = self._join_summary_parts(item_state.summary_parts)
            return
        if typ == "response.reasoning_summary_part.done":
            part = parsed.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    item_state.summary_parts[self._summary_index(parsed)] = text
                    item_state.assembled_text = self._join_summary_parts(item_state.summary_parts)

    def _summary_parts_from_item(self, item: dict[str, Any]) -> dict[int, str]:
        parts: dict[int, str] = {}
        summary = item.get("summary")
        if isinstance(summary, list):
            for i, block in enumerate(summary):
                if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]:
                    parts[i] = block["text"]
        return parts

    def _extract_reasoning_text(self, item: dict[str, Any]) -> str:
        """Prefer plaintext content blocks; fall back to multi-part summary."""
        content = item.get("content")
        content_parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text:
                    content_parts.append(text)
        if content_parts:
            return "\n".join(content_parts)
        if isinstance(content, str) and content:
            return content
        summary_parts = self._summary_parts_from_item(item)
        return self._join_summary_parts(summary_parts)

    def _extract_message_text(self, item: dict[str, Any]) -> str:
        content = item.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        if isinstance(content, str):
            return content
        summary = item.get("summary")
        if isinstance(summary, list):
            parts = []
            for block in summary:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            return "\n".join(parts)
        return ""

    def _maybe_attribute(self, state: ResponseState, event: RawLogEvent) -> None:
        if state.attribution.confidence == AttributionConfidence.CONFIRMED:
            # Record conflicts only; never downgrade or move.
            if event.thread_id and state.agent_thread_id and event.thread_id != state.agent_thread_id:
                msg = f"conflict thread {event.thread_id} vs {state.agent_thread_id} @log {event.log_id}"
                if msg not in state.attribution.conflicts:
                    state.attribution.conflicts.append(msg)
            return

        # Level 2-ish: unique process->thread mapping with request path
        if event.process_uuid:
            threads = self.process_threads.get(event.process_uuid) or set()
            if len(threads) == 1:
                thread_id = next(iter(threads))
                conf = (
                    AttributionConfidence.CONFIRMED
                    if event.target != "codex_api::sse::responses"
                    else AttributionConfidence.INFERRED
                )
                kind = (
                    EvidenceKind.REQUEST_LIFECYCLE
                    if conf == AttributionConfidence.CONFIRMED
                    else EvidenceKind.STREAM_CORRELATION
                )
                self._upgrade_attribution(
                    state,
                    thread_id=thread_id,
                    confidence=conf,
                    evidence_kind=kind,
                    log_id=event.log_id,
                    detail="unique process thread",
                )
                return

        # Level 4: opaque id prefix alone never confirms; only infer when unique
        # and we already have a candidate thread elsewhere — otherwise leave unassigned.
        return

    def _upgrade_attribution(
        self,
        state: ResponseState,
        *,
        thread_id: str,
        confidence: AttributionConfidence,
        evidence_kind: EvidenceKind,
        log_id: int | None,
        detail: str,
    ) -> bool:
        current = state.attribution.confidence
        if confidence < current:
            return False
        if (
            current == AttributionConfidence.CONFIRMED
            and state.agent_thread_id
            and state.agent_thread_id != thread_id
        ):
            msg = f"ignored move {state.agent_thread_id} -> {thread_id} ({detail})"
            if msg not in state.attribution.conflicts:
                state.attribution.conflicts.append(msg)
            return False

        # Monotonic: unassigned -> inferred -> confirmed; same confidence may refine detail.
        if confidence == current and state.agent_thread_id and state.agent_thread_id != thread_id:
            # Do not oscillate between agents at same confidence.
            msg = f"same-confidence conflict {state.agent_thread_id} vs {thread_id}"
            if msg not in state.attribution.conflicts:
                state.attribution.conflicts.append(msg)
            return False

        state.agent_thread_id = thread_id
        state.attribution.thread_id = thread_id
        state.attribution.confidence = confidence
        state.attribution.evidence_kind = evidence_kind
        state.attribution.detail = detail
        state.attribution.updated_at = state.ended_at or state.started_at
        if log_id is not None:
            state.attribution.source_log_ids.append(log_id)
        return True

    def _append_unresolved(self, event: RawLogEvent, *, reason: str) -> None:
        self.unresolved.append(UnresolvedEvent(event=event, reason=reason))
        if len(self.unresolved) > MAX_UNRESOLVED:
            # Drop oldest; keep the freshest window for late joins.
            overflow = len(self.unresolved) - MAX_UNRESOLVED
            del self.unresolved[:overflow]

    def _retry_unresolved(self) -> list[str]:
        if not self.unresolved:
            return []
        remaining: list[UnresolvedEvent] = []
        changed: list[str] = []
        for entry in self.unresolved:
            # Skip permanently unroutable rows (no id hooks) without re-scanning forever.
            ev = entry.event
            if not ev.response_id and not ev.item_id:
                remaining.append(entry)
                continue
            rid = self._resolve_response_id(ev)
            if rid is None:
                remaining.append(entry)
                continue
            state = self._ensure_response(rid, ev)
            self._apply_event(state, ev)
            self._maybe_attribute(state, ev)
            changed.append(rid)
        self.unresolved = remaining
        return changed



def display_item_number(output_index: int) -> int:
    """Protocol output_index is zero-based; UI is one-based."""
    return output_index + 1
