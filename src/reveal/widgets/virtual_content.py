"""Virtualized multiline content view for large raw/tool payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual.geometry import Size
from textual.message import Message
from textual.reactive import reactive
from textual.scroll_view import ScrollView
from textual.strip import Strip


LARGE_LINE_THRESHOLD = 200
LARGE_BYTE_THRESHOLD = 65536
EMBEDDED_VIEWPORT_ROWS = 18


def is_large_content(text: str) -> bool:
    if len(text.encode("utf-8", errors="replace")) > LARGE_BYTE_THRESHOLD:
        return True
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0) > LARGE_LINE_THRESHOLD


@dataclass
class VirtualContentSource:
    """Append-only logical rows with lazy wrap mapping."""

    rows: list[str] = field(default_factory=list)
    _width: int = 80
    _wrap_cache: dict[int, list[str]] = field(default_factory=dict)
    _total_display: int | None = None

    @classmethod
    def from_text(cls, text: str) -> "VirtualContentSource":
        # Preserve exact text; splitlines keeps content without dropping final empty
        if text == "":
            return cls(rows=[])
        rows = text.split("\n")
        return cls(rows=rows)

    @classmethod
    def from_raw_events(cls, headers_and_bodies: Iterable[tuple[str, str]]) -> "VirtualContentSource":
        rows: list[str] = []
        for header, body in headers_and_bodies:
            rows.append(header)
            if body:
                rows.extend(body.split("\n"))
            rows.append("")
        return cls(rows=rows)

    def append_row(self, row: str) -> None:
        self.rows.append(row)
        self._wrap_cache.pop(len(self.rows) - 1, None)
        self._total_display = None

    def set_width(self, width: int) -> None:
        width = max(1, width)
        if width != self._width:
            self._width = width
            self._wrap_cache.clear()
            self._total_display = None

    def wrapped_lines_for_row(self, idx: int) -> list[str]:
        if idx in self._wrap_cache:
            return self._wrap_cache[idx]
        text = self.rows[idx] if 0 <= idx < len(self.rows) else ""
        width = self._width
        if not text:
            lines = [""]
        elif len(text) <= width:
            lines = [text]
        else:
            lines = [text[i : i + width] for i in range(0, len(text), width)]
        self._wrap_cache[idx] = lines
        return lines

    def total_display_lines(self) -> int:
        if self._total_display is None:
            total = 0
            for i in range(len(self.rows)):
                total += len(self.wrapped_lines_for_row(i))
            self._total_display = total
        return self._total_display

    def iter_display(self) -> Iterable[tuple[int, int, str]]:
        """Yield (row_index, subline_index, text)."""
        for i in range(len(self.rows)):
            for j, line in enumerate(self.wrapped_lines_for_row(i)):
                yield i, j, line

    def display_line(self, y: int) -> tuple[int, int, str]:
        if y < 0:
            return 0, 0, ""
        remaining = y
        for i in range(len(self.rows)):
            lines = self.wrapped_lines_for_row(i)
            if remaining < len(lines):
                return i, remaining, lines[remaining]
            remaining -= len(lines)
        return max(0, len(self.rows) - 1), 0, ""

    def search(self, query: str, *, regex: bool = False, case_sensitive: bool = False) -> list[tuple[int, int, int]]:
        """Return list of (display_line, start, end) matches over full content."""
        matches: list[tuple[int, int, int]] = []
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error:
            return matches
        y = 0
        for i in range(len(self.rows)):
            for line in self.wrapped_lines_for_row(i):
                for m in pattern.finditer(line):
                    matches.append((y, m.start(), m.end()))
                y += 1
        return matches


class VirtualContentView(ScrollView):
    """Custom ScrollView rendering only visible lines."""

    COMPONENT_CLASSES = {"virtual-content--match"}

    source: reactive[VirtualContentSource | None] = reactive(None)
    search_query: reactive[str] = reactive("")
    search_regex: reactive[bool] = reactive(False)

    class OpenFullScreen(Message):
        def __init__(self, source: VirtualContentSource, y: int) -> None:
            super().__init__()
            self.source = source
            self.y = y

    def __init__(self, source: VirtualContentSource | None = None, *, max_height: int = EMBEDDED_VIEWPORT_ROWS, **kwargs):
        super().__init__(**kwargs)
        self._source = source or VirtualContentSource()
        self.max_height = max_height
        self._matches: list[tuple[int, int, int]] = []
        self._match_index = -1
        self._logical_y = 0

    @property
    def content_source(self) -> VirtualContentSource:
        return self._source

    def set_source(self, source: VirtualContentSource) -> None:
        self._source = source
        self._refresh_virtual_size()
        self.refresh()

    def on_mount(self) -> None:
        self._refresh_virtual_size()

    def on_resize(self, event) -> None:
        width = max(1, self.size.width - 1)
        self._source.set_width(width)
        self._refresh_virtual_size()
        if self.search_query:
            self._matches = self._source.search(self.search_query, regex=self.search_regex)

    def _refresh_virtual_size(self) -> None:
        width = max(1, self.size.width - 1) if self.size.width else 80
        self._source.set_width(width)
        lines = self._source.total_display_lines()
        self.virtual_size = Size(width, max(lines, 1))

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        line_y = y + scroll_y
        width = max(1, self.size.width)
        rich_style = self.rich_style
        total = self._source.total_display_lines()
        if line_y < 0 or line_y >= total:
            return Strip.blank(width, rich_style)

        _row, _sub, text = self._source.display_line(line_y)
        style = Style()
        highlights = [(s, e) for (ly, s, e) in self._matches if ly == line_y]
        if highlights:
            segments: list[Segment] = []
            cursor = 0
            match_style = Style(bold=True, reverse=True)
            for start, end in sorted(highlights):
                start = max(0, min(start, len(text)))
                end = max(start, min(end, len(text)))
                if start > cursor:
                    segments.append(Segment(text[cursor:start], style))
                segments.append(Segment(text[start:end], match_style))
                cursor = end
            if cursor < len(text):
                segments.append(Segment(text[cursor:], style))
            strip = Strip(segments, cell_len(text))
        else:
            strip = Strip([Segment(text, style)], cell_len(text))

        strip = strip.crop_extend(scroll_x, scroll_x + width, rich_style)
        return strip.apply_offsets(scroll_x, y)

    def set_search(self, query: str, *, regex: bool = False) -> tuple[int, str]:
        self.search_query = query
        self.search_regex = regex
        if not query:
            self._matches = []
            self._match_index = -1
            self.refresh()
            return 0, ""
        try:
            if regex:
                re.compile(query)
        except re.error as exc:
            self._matches = []
            self._match_index = -1
            self.refresh()
            return 0, f"invalid regex: {exc}"
        self._matches = self._source.search(query, regex=regex)
        self._match_index = 0 if self._matches else -1
        if self._match_index >= 0:
            self._scroll_to_match()
        self.refresh()
        return len(self._matches), ""

    def next_match(self) -> None:
        if not self._matches:
            return
        self._match_index = (self._match_index + 1) % len(self._matches)
        self._scroll_to_match()
        self.refresh()

    def prev_match(self) -> None:
        if not self._matches:
            return
        self._match_index = (self._match_index - 1) % len(self._matches)
        self._scroll_to_match()
        self.refresh()

    def _scroll_to_match(self) -> None:
        if self._match_index < 0 or self._match_index >= len(self._matches):
            return
        y, _, _ = self._matches[self._match_index]
        self._logical_y = y
        self.scroll_to(y=y, animate=False)

    @property
    def match_count(self) -> int:
        return len(self._matches)

    @property
    def current_match(self) -> int:
        return self._match_index + 1 if self._match_index >= 0 else 0

    def visible_range(self) -> tuple[int, int, int]:
        """Return (first_visible, last_visible, total)."""
        total = max(1, self._source.total_display_lines())
        first = int(self.scroll_offset.y)
        last = min(total, first + max(1, self.size.height))
        return first + 1, last, total
