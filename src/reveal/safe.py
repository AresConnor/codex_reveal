"""Coerce untrusted Codex log/session values so parsers never crash."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def as_finite_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def as_finite_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(x):
        return default
    return x


def as_aware_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts
