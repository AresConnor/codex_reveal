"""Sanitized SSE / rollout fixtures preserving real field shapes."""

from __future__ import annotations

import json
from typing import Any

from reveal.models import RawLogEvent
from reveal.routing import parse_log_body


def sse_body(payload: dict[str, Any]) -> str:
    return "SSE event: " + json.dumps(payload, separators=(",", ":"))


def make_event(
    log_id: int,
    payload: dict[str, Any] | str,
    *,
    ts: float = 1.0,
    target: str = "codex_api::sse::responses",
    process_uuid: str | None = None,
    thread_id: str | None = None,
    level: str = "INFO",
) -> RawLogEvent:
    if isinstance(payload, str):
        body = payload
    else:
        body = sse_body(payload)
    return parse_log_body(
        log_id=log_id,
        timestamp=ts + log_id * 0.001,
        target=target,
        body=body,
        level=level,
        process_uuid=process_uuid,
        thread_id=thread_id,
    )


def fixture_single_response_thinking_tool_answer() -> list[RawLogEvent]:
    rid = "resp_aaa111"
    return [
        make_event(
            1,
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": {
                    "id": rid,
                    "model": "gpt-sol",
                    "reasoning": {"effort": "high"},
                },
            },
        ),
        make_event(
            2,
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": {"id": "item_aaa111_r", "type": "reasoning"},
            },
        ),
        make_event(
            3,
            {
                "type": "response.reasoning_summary_text.delta",
                "sequence_number": 2,
                "item_id": "item_aaa111_r",
                "output_index": 0,
                "delta": "Thinking about shells",
            },
        ),
        make_event(
            4,
            {
                "type": "response.output_item.done",
                "sequence_number": 3,
                "output_index": 0,
                "item": {
                    "id": "item_aaa111_r",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Thinking about shells"}],
                },
            },
        ),
        make_event(
            5,
            {
                "type": "response.output_item.added",
                "sequence_number": 4,
                "output_index": 1,
                "item": {
                    "id": "item_aaa111_t",
                    "type": "custom_tool_call",
                    "name": "shell_command",
                    "call_id": "call_shell_1",
                },
            },
        ),
        make_event(
            6,
            {
                "type": "response.custom_tool_call_input.delta",
                "sequence_number": 5,
                "item_id": "item_aaa111_t",
                "output_index": 1,
                "delta": '{"cmd":"ls"}',
            },
        ),
        make_event(
            7,
            {
                "type": "response.output_item.done",
                "sequence_number": 6,
                "output_index": 1,
                "item": {
                    "id": "item_aaa111_t",
                    "type": "custom_tool_call",
                    "name": "shell_command",
                    "call_id": "call_shell_1",
                    "input": '{"cmd":"ls"}',
                },
            },
        ),
        make_event(
            8,
            {
                "type": "response.output_item.added",
                "sequence_number": 7,
                "output_index": 2,
                "item": {"id": "item_aaa111_m", "type": "message"},
            },
        ),
        make_event(
            9,
            {
                "type": "response.output_text.delta",
                "sequence_number": 8,
                "item_id": "item_aaa111_m",
                "output_index": 2,
                "delta": "Done listing files.",
            },
        ),
        make_event(
            10,
            {
                "type": "response.output_item.done",
                "sequence_number": 9,
                "output_index": 2,
                "item": {
                    "id": "item_aaa111_m",
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Done listing files."}],
                },
            },
        ),
        make_event(
            11,
            {
                "type": "response.completed",
                "sequence_number": 10,
                "response": {
                    "id": rid,
                    "model": "gpt-sol",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            },
        ),
    ]


def fixture_two_interleaved_responses() -> list[RawLogEvent]:
    a = "resp_alpha"
    b = "resp_beta"
    return [
        make_event(1, {"type": "response.created", "sequence_number": 0, "response": {"id": a, "model": "gpt-sol"}}),
        make_event(2, {"type": "response.created", "sequence_number": 0, "response": {"id": b, "model": "gpt-luna"}}),
        make_event(
            3,
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": {"id": "item_alpha_1", "type": "message"},
            },
        ),
        make_event(
            4,
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": {"id": "item_beta_1", "type": "message"},
            },
        ),
        make_event(
            5,
            {
                "type": "response.output_text.delta",
                "item_id": "item_alpha_1",
                "output_index": 0,
                "delta": "A",
            },
        ),
        make_event(
            6,
            {
                "type": "response.output_text.delta",
                "item_id": "item_beta_1",
                "output_index": 0,
                "delta": "B",
            },
        ),
        make_event(7, {"type": "response.completed", "response": {"id": a, "usage": {}}}),
        make_event(8, {"type": "response.completed", "response": {"id": b, "usage": {}}}),
    ]


def fixture_ambiguous_delta_without_ids() -> list[RawLogEvent]:
    """Delta with no response_id/item_id while two responses are active."""
    events = [
        make_event(1, {"type": "response.created", "response": {"id": "resp_x", "model": "sol"}}),
        make_event(2, {"type": "response.created", "response": {"id": "resp_y", "model": "luna"}}),
        make_event(
            3,
            {
                "type": "response.output_text.delta",
                "delta": "orphan",
            },
        ),
    ]
    return events


def fixture_malformed_and_unhandled() -> list[RawLogEvent]:
    return [
        make_event(1, {"type": "response.created", "response": {"id": "resp_m", "model": "sol"}}),
        make_event(2, "SSE event: {not-json"),
        make_event(3, "unhandled responses event: {\"type\":\"custom.future\",\"response_id\":\"resp_m\"}"),
        make_event(4, {"type": "response.completed", "response": {"id": "resp_m"}}),
    ]


def fixture_partial_missing_start() -> list[RawLogEvent]:
    """Events for a response whose created event is outside the backfill window."""
    return [
        make_event(
            50,
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "item_late_1", "type": "message"},
                "response_id": "resp_late",
            },
        ),
        make_event(
            51,
            {
                "type": "response.output_text.delta",
                "item_id": "item_late_1",
                "output_index": 0,
                "delta": "late",
            },
        ),
        make_event(52, {"type": "response.completed", "response": {"id": "resp_late"}}),
    ]


def fixture_function_and_unknown_items() -> list[RawLogEvent]:
    rid = "resp_misc"
    return [
        make_event(1, {"type": "response.created", "response": {"id": rid, "model": "sol"}}),
        make_event(
            2,
            {
                "type": "response.output_item.added",
                "response_id": rid,
                "output_index": 0,
                "item": {
                    "id": "item_fn",
                    "type": "function_call",
                    "name": "lookup",
                    "call_id": "call_fn_1",
                },
            },
        ),
        make_event(
            3,
            {
                "type": "response.output_item.added",
                "response_id": rid,
                "output_index": 1,
                "item": {"id": "item_unk", "type": "future_widget"},
            },
        ),
        make_event(4, {"type": "response.completed", "response": {"id": rid}}),
    ]



ROOT_THREAD = "thread-root-1"
SUB_A = "thread-sub-a"
SUB_B = "thread-sub-b"
SESSION_ID = "thread-root-1"


def fixture_rollout_session_meta_lines() -> dict[str, dict[str, Any]]:
    """First-line session_meta payloads for root + two subagents."""
    return {
        "root": {
            "type": "session_meta",
            "payload": {
                "id": ROOT_THREAD,
                "session_id": SESSION_ID,
                "parent_thread_id": "",
                "timestamp": "2026-07-16T01:14:00Z",
                "cwd": r"D:\workspace\MCServerLauncher-Future",
                "originator": "codex",
                "cli_version": "0.0.0",
                "thread_source": "user",
                "agent_nickname": "",
                "agent_role": "",
            },
        },
        "gibbs": {
            "type": "session_meta",
            "payload": {
                "id": SUB_A,
                "session_id": SESSION_ID,
                "parent_thread_id": ROOT_THREAD,
                "timestamp": "2026-07-16T01:15:00Z",
                "cwd": r"D:\workspace\MCServerLauncher-Future",
                "originator": "codex",
                "cli_version": "0.0.0",
                "thread_source": "subagent",
                "agent_nickname": "Gibbs",
                "agent_role": "worker",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "agent_nickname": "Gibbs",
                            "agent_role": "worker",
                            "agent_path": "root/Gibbs",
                            "depth": 1,
                        }
                    }
                },
            },
        },
        "popper": {
            "type": "session_meta",
            "payload": {
                "id": SUB_B,
                "session_id": SESSION_ID,
                "parent_thread_id": ROOT_THREAD,
                "timestamp": "2026-07-16T01:16:00Z",
                "cwd": r"D:\workspace\MCServerLauncher-Future",
                "originator": "codex",
                "cli_version": "0.0.0",
                "thread_source": "subagent",
                "agent_nickname": "Popper",
                "agent_role": "explorer",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "agent_nickname": "Popper",
                            "agent_role": "explorer",
                            "agent_path": "root/Popper",
                            "depth": 1,
                        }
                    }
                },
            },
        },
    }


def fixture_tool_rollout_events(call_id: str = "call_shell_1") -> list[dict[str, Any]]:
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": call_id,
                "name": "shell_command",
                "input": '{"cmd":"ls"}',
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "tool_result",
                "call_id": call_id,
                "status": "completed",
                "duration_ms": 42,
                "exit_code": 0,
                "output": "a.txt\nb.txt\n",
            },
        },
    ]
