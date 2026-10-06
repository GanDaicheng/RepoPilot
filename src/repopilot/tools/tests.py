"""Deterministic extraction of useful details from test output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import perf_counter

from repopilot.domain.results import ToolResult


FAILED_RE = re.compile(r"^FAILED\s+(\S+)")
EXCEPTION_RE = re.compile(r"^\s*E\s+([A-Za-z_][A-Za-z0-9_.]*)\s*:")
LOCATION_RE = re.compile(r"((?:[A-Za-z]:)?[^:\r\n]*?\.py:\d+)")


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    failed_tests: tuple[str, ...]
    exception_types: tuple[str, ...]
    locations: tuple[str, ...]
    tail: str
    truncated: bool


def _append_unique(items: list[str], value: str, limit: int) -> None:
    if value not in items and len(items) < limit:
        items.append(value)


def inspect_error(
    output: str,
    *,
    max_chars: int = 20_000,
    max_items: int = 20,
) -> ToolResult[ErrorSummary]:
    """Extract bounded pytest failures, exception types, locations, and tail."""

    started = perf_counter()
    if max_chars < 1 or max_items < 1:
        return ToolResult.failure(
            "invalid_limit",
            "max_chars and max_items must both be positive.",
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
        )

    failed_tests: list[str] = []
    exception_types: list[str] = []
    locations: list[str] = []
    for raw_line in output.splitlines():
        failed_match = FAILED_RE.match(raw_line)
        if failed_match:
            _append_unique(failed_tests, failed_match.group(1), max_items)
        exception_match = EXCEPTION_RE.match(raw_line)
        if exception_match:
            _append_unique(exception_types, exception_match.group(1), max_items)
        location_match = LOCATION_RE.search(raw_line.strip())
        if location_match:
            _append_unique(locations, location_match.group(1), max_items)

    summary = ErrorSummary(
        failed_tests=tuple(failed_tests),
        exception_types=tuple(exception_types),
        locations=tuple(locations),
        tail=output[-max_chars:],
        truncated=len(output) > max_chars,
    )
    duration_ms = max(0, int((perf_counter() - started) * 1000))
    return ToolResult.success(summary, duration_ms=duration_ms)

