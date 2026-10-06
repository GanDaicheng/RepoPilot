"""Durable, bounded Server-Sent Event serialization and polling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

from repopilot.domain.tasks import TERMINAL_STATUSES, EventRecord
from repopilot.security.secrets import find_secret_kind


MAX_EVENT_ID = 9_223_372_036_854_775_807
TERMINAL_EVENTS = frozenset({"task_succeeded", "task_failed", "task_cancelled"})


class EventRepositoryLike(Protocol):
    async def list_after(
        self, task_id: str, event_id: int, *, limit: int = 100
    ) -> tuple[EventRecord, ...]: ...


class TaskRepositoryLike(Protocol):
    async def get(self, task_id: str) -> Any: ...


def parse_last_event_id(value: str | None) -> int:
    if value is None:
        return 0
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise ValueError("Last-Event-ID must be a non-negative decimal event ID.")
    parsed = int(value)
    if parsed > MAX_EVENT_ID:
        raise ValueError("Last-Event-ID is outside the supported range.")
    return parsed


def _redact(value: object) -> object:
    if isinstance(value, str):
        if find_secret_kind(value) is not None:
            return "[redacted]"
        return value[:8_192]
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _redact(item)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1_024]


def serialize_event(event: EventRecord) -> bytes:
    data = json.dumps(
        {
            "task_id": event.task_id,
            "stage": event.stage,
            "payload": _redact(event.payload),
            "created_at": event.created_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"id: {event.id}\n"
        f"event: {event.event_type.value}\n"
        f"data: {data}\n\n"
    ).encode("utf-8")


async def event_stream(
    task_id: str,
    after_id: int,
    event_repository: EventRepositoryLike,
    *,
    task_repository: TaskRepositoryLike | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    poll_interval: float = 0.25,
    heartbeat_interval: float = 15.0,
    batch_size: int = 100,
) -> AsyncIterator[bytes]:
    if after_id < 0 or poll_interval <= 0 or heartbeat_interval < 0:
        raise ValueError("Invalid event stream bounds.")
    limit = max(1, min(batch_size, 100))
    cursor = after_id
    loop = asyncio.get_running_loop()
    last_emission = loop.time()
    while True:
        if is_disconnected is not None and await is_disconnected():
            return
        events = await event_repository.list_after(task_id, cursor, limit=limit)
        if events:
            for event in events:
                cursor = event.id
                yield serialize_event(event)
                last_emission = loop.time()
                if event.event_type.value in TERMINAL_EVENTS:
                    return
            continue

        if task_repository is not None:
            task = await task_repository.get(task_id)
            if task is None or task.status in TERMINAL_STATUSES:
                return
        now = loop.time()
        if now - last_emission >= heartbeat_interval:
            yield b": heartbeat\n\n"
            last_emission = now
        if is_disconnected is not None and await is_disconnected():
            return
        await asyncio.sleep(poll_interval)
