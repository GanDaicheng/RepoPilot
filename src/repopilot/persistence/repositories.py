"""Transactional repositories for tasks, events, and model-call usage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import aiosqlite

from repopilot.domain.tasks import (
    TERMINAL_STATUSES,
    EventRecord,
    EventType,
    ModelCallRecord,
    TaskInput,
    TaskRecord,
    TaskStatus,
    can_transition,
)
from repopilot.persistence.database import SqliteDatabase


MAX_EVENT_PAYLOAD_BYTES = 65_536


class RepositoryError(RuntimeError):
    """Base class for stable persistence-layer failures."""


class TaskNotFoundError(RepositoryError):
    pass


class TaskConflictError(RepositoryError):
    pass


class InvalidTransitionError(TaskConflictError):
    pass


class InvalidUpdateFieldError(RepositoryError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _payload_json(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Event payload must be JSON serializable.") from exc
    if len(encoded.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("Event payload exceeds the configured size limit.")
    return encoded


def _task_from_row(row: aiosqlite.Row) -> TaskRecord:
    return TaskRecord(
        id=str(row["id"]),
        thread_id=str(row["thread_id"]),
        repo_path=Path(str(row["repo_path"])),
        user_request=str(row["user_request"]),
        test_command=str(row["test_command"]),
        model_profile=str(row["model_profile"]),
        status=TaskStatus(str(row["status"])),
        current_stage=str(row["current_stage"]),
        base_commit=None if row["base_commit"] is None else str(row["base_commit"]),
        worktree_path=(
            None if row["worktree_path"] is None else Path(str(row["worktree_path"]))
        ),
        retry_count=int(row["retry_count"]),
        max_retries=int(row["max_retries"]),
        cancel_requested=bool(row["cancel_requested"]),
        error_type=None if row["error_type"] is None else str(row["error_type"]),
        error_message=(
            None if row["error_message"] is None else str(row["error_message"])
        ),
        created_at=_parse_time(str(row["created_at"])),  # type: ignore[arg-type]
        started_at=_parse_time(row["started_at"]),
        finished_at=_parse_time(row["finished_at"]),
        updated_at=_parse_time(str(row["updated_at"])),  # type: ignore[arg-type]
    )


def _event_from_row(row: aiosqlite.Row) -> EventRecord:
    return EventRecord(
        id=int(row["id"]),
        task_id=str(row["task_id"]),
        event_type=EventType(str(row["event_type"])),
        stage=str(row["stage"]),
        payload=json.loads(str(row["payload_json"])),
        dedupe_key=None if row["dedupe_key"] is None else str(row["dedupe_key"]),
        created_at=_parse_time(str(row["created_at"])),  # type: ignore[arg-type]
    )


async def _select_task(
    connection: aiosqlite.Connection,
    task_id: str,
) -> TaskRecord | None:
    row = await (
        await connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    ).fetchone()
    return None if row is None else _task_from_row(row)


async def _select_event_by_dedupe(
    connection: aiosqlite.Connection,
    task_id: str,
    dedupe_key: str | None,
) -> EventRecord | None:
    if dedupe_key is None:
        return None
    row = await (
        await connection.execute(
            "SELECT * FROM events WHERE task_id = ? AND dedupe_key = ?",
            (task_id, dedupe_key),
        )
    ).fetchone()
    return None if row is None else _event_from_row(row)


async def _insert_event(
    connection: aiosqlite.Connection,
    *,
    task_id: str,
    event_type: EventType,
    stage: str,
    payload: Mapping[str, object],
    dedupe_key: str | None,
    created_at: datetime,
) -> EventRecord:
    cursor = await connection.execute(
        "INSERT INTO events(task_id, event_type, stage, payload_json, dedupe_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            task_id,
            event_type.value,
            stage,
            _payload_json(payload),
            dedupe_key,
            _serialize_time(created_at),
        ),
    )
    row = await (
        await connection.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,))
    ).fetchone()
    if row is None:
        raise RepositoryError("The inserted event could not be read back.")
    return _event_from_row(row)


class TaskRepository:
    def __init__(self, database: SqliteDatabase):
        self.database = database

    async def create(
        self,
        task_input: TaskInput,
        *,
        task_id: str,
        thread_id: str,
    ) -> TaskRecord:
        now = _now()
        async with self.database.connection() as connection:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                await connection.execute(
                    "INSERT INTO tasks("
                    "id, thread_id, repo_path, user_request, test_command, model_profile, "
                    "status, current_stage, base_commit, worktree_path, retry_count, "
                    "max_retries, cancel_requested, error_type, error_message, created_at, "
                    "started_at, finished_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, 0, NULL, NULL, ?, NULL, NULL, ?)",
                    (
                        task_id,
                        thread_id,
                        str(task_input.repo_path),
                        task_input.user_request,
                        task_input.test_command,
                        task_input.model_profile,
                        TaskStatus.QUEUED.value,
                        TaskStatus.QUEUED.value,
                        task_input.max_retries,
                        _serialize_time(now),
                        _serialize_time(now),
                    ),
                )
                await _insert_event(
                    connection,
                    task_id=task_id,
                    event_type=EventType.TASK_CREATED,
                    stage=TaskStatus.QUEUED.value,
                    payload={"status": TaskStatus.QUEUED.value},
                    dedupe_key=f"{task_id}:created",
                    created_at=now,
                )
                task = await _select_task(connection, task_id)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        if task is None:
            raise RepositoryError("The created task could not be read back.")
        return task

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self.database.connection() as connection:
            return await _select_task(connection, task_id)

    async def claim_next(self) -> TaskRecord | None:
        async with self.database.connection() as connection:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                row = await (
                    await connection.execute(
                        "SELECT * FROM tasks WHERE status IN (?, ?) "
                        "ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, created_at, id LIMIT 1",
                        (
                            TaskStatus.RUNNING.value,
                            TaskStatus.QUEUED.value,
                            TaskStatus.RUNNING.value,
                        ),
                    )
                ).fetchone()
                if row is None:
                    await connection.commit()
                    return None
                task = _task_from_row(row)
                if task.status is TaskStatus.RUNNING:
                    await connection.commit()
                    return task
                now = _now()
                await connection.execute(
                    "UPDATE tasks SET status = ?, current_stage = ?, started_at = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        TaskStatus.RUNNING.value,
                        "starting",
                        _serialize_time(now),
                        _serialize_time(now),
                        task.id,
                        TaskStatus.QUEUED.value,
                    ),
                )
                await _insert_event(
                    connection,
                    task_id=task.id,
                    event_type=EventType.TASK_STARTED,
                    stage="starting",
                    payload={"status": TaskStatus.RUNNING.value},
                    dedupe_key=f"{task.id}:started",
                    created_at=now,
                )
                claimed = await _select_task(connection, task.id)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return claimed

    async def transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        stage: str,
        event_type: EventType,
        payload: Mapping[str, object],
        dedupe_key: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> TaskRecord:
        async with self.database.connection() as connection:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                task = await _select_task(connection, task_id)
                if task is None:
                    raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
                if task.cancel_requested and target in {
                    TaskStatus.SUCCEEDED,
                    TaskStatus.FAILED,
                    TaskStatus.AWAITING_APPROVAL,
                }:
                    target = TaskStatus.CANCELLED
                    stage = TaskStatus.CANCELLED.value
                    event_type = EventType.TASK_CANCELLED
                    payload = {"status": TaskStatus.CANCELLED.value}
                    dedupe_key = f"{task_id}:cancelled"
                    error_type = "cancelled"
                    error_message = "The task was cancelled."
                existing = await _select_event_by_dedupe(
                    connection, task_id, dedupe_key
                )
                if existing is not None:
                    await connection.commit()
                    return task
                if not can_transition(task.status, target):
                    raise InvalidTransitionError(
                        f"Cannot transition task from {task.status.value} to {target.value}."
                    )
                now = _now()
                started_at = task.started_at
                if target is TaskStatus.RUNNING and started_at is None:
                    started_at = now
                finished_at = now if target in TERMINAL_STATUSES else task.finished_at
                await connection.execute(
                    "UPDATE tasks SET status = ?, current_stage = ?, error_type = ?, "
                    "error_message = ?, started_at = ?, finished_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        target.value,
                        stage,
                        error_type,
                        error_message,
                        _serialize_time(started_at),
                        _serialize_time(finished_at),
                        _serialize_time(now),
                        task_id,
                    ),
                )
                await _insert_event(
                    connection,
                    task_id=task_id,
                    event_type=event_type,
                    stage=stage,
                    payload=payload,
                    dedupe_key=dedupe_key,
                    created_at=now,
                )
                updated = await _select_task(connection, task_id)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        if updated is None:
            raise RepositoryError("The transitioned task could not be read back.")
        return updated

    async def set_base_commit_once(self, task_id: str, base_commit: str) -> TaskRecord:
        if not base_commit or len(base_commit) > 128:
            raise InvalidUpdateFieldError("base_commit is invalid.")
        async with self.database.connection() as connection:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                task = await _select_task(connection, task_id)
                if task is None:
                    raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
                if task.base_commit is None:
                    now = _now()
                    await connection.execute(
                        "UPDATE tasks SET base_commit = ?, updated_at = ? "
                        "WHERE id = ? AND base_commit IS NULL",
                        (base_commit, _serialize_time(now), task_id),
                    )
                updated = await _select_task(connection, task_id)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        if updated is None:
            raise RepositoryError("The updated task could not be read back.")
        return updated

    async def request_cancel(self, task_id: str) -> TaskRecord:
        async with self.database.connection() as connection:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                task = await _select_task(connection, task_id)
                if task is None:
                    raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
                if task.status in TERMINAL_STATUSES or task.cancel_requested:
                    raise TaskConflictError("The task cannot be cancelled again.")
                now = _now()
                immediate = task.status in {
                    TaskStatus.QUEUED,
                    TaskStatus.AWAITING_APPROVAL,
                }
                next_status = TaskStatus.CANCELLED if immediate else task.status
                next_stage = TaskStatus.CANCELLED.value if immediate else task.current_stage
                await connection.execute(
                    "UPDATE tasks SET status = ?, current_stage = ?, cancel_requested = 1, "
                    "finished_at = ?, updated_at = ? WHERE id = ?",
                    (
                        next_status.value,
                        next_stage,
                        _serialize_time(now) if immediate else None,
                        _serialize_time(now),
                        task_id,
                    ),
                )
                if immediate:
                    await _insert_event(
                        connection,
                        task_id=task_id,
                        event_type=EventType.TASK_CANCELLED,
                        stage=TaskStatus.CANCELLED.value,
                        payload={"status": TaskStatus.CANCELLED.value},
                        dedupe_key=f"{task_id}:cancelled",
                        created_at=now,
                    )
                updated = await _select_task(connection, task_id)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        if updated is None:
            raise RepositoryError("The cancelled task could not be read back.")
        return updated

    async def update_execution(self, task_id: str, **bounded_fields: object) -> TaskRecord:
        allowed = {
            "current_stage",
            "base_commit",
            "worktree_path",
            "retry_count",
            "error_type",
            "error_message",
        }
        unknown = set(bounded_fields) - allowed
        if unknown:
            raise InvalidUpdateFieldError(
                f"Unsupported task update fields: {', '.join(sorted(unknown))}."
            )
        values = dict(bounded_fields)
        if "worktree_path" in values and values["worktree_path"] is not None:
            values["worktree_path"] = str(values["worktree_path"])
        if "retry_count" in values and (
            not isinstance(values["retry_count"], int) or values["retry_count"] < 0
        ):
            raise InvalidUpdateFieldError("retry_count must be a non-negative integer.")
        for field, limit in (
            ("current_stage", 128),
            ("base_commit", 128),
            ("error_type", 128),
            ("error_message", 4_096),
        ):
            value = values.get(field)
            if value is not None and (
                not isinstance(value, str) or len(value) > limit
            ):
                raise InvalidUpdateFieldError(f"{field} is invalid or too long.")
        if not values:
            task = await self.get(task_id)
            if task is None:
                raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
            return task

        values["updated_at"] = _serialize_time(_now())
        assignments = ", ".join(f"{field} = ?" for field in values)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",  # noqa: S608 - allowlisted identifiers
                (*values.values(), task_id),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
            await connection.commit()
        updated = await self.get(task_id)
        if updated is None:
            raise RepositoryError("The updated task could not be read back.")
        return updated


class EventRepository:
    def __init__(self, database: SqliteDatabase):
        self.database = database

    async def append(
        self,
        task_id: str,
        event_type: EventType,
        *,
        stage: str,
        payload: Mapping[str, object],
        dedupe_key: str | None = None,
    ) -> EventRecord:
        async with self.database.connection() as connection:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                existing = await _select_event_by_dedupe(
                    connection, task_id, dedupe_key
                )
                if existing is not None:
                    await connection.commit()
                    return existing
                event = await _insert_event(
                    connection,
                    task_id=task_id,
                    event_type=event_type,
                    stage=stage,
                    payload=payload,
                    dedupe_key=dedupe_key,
                    created_at=_now(),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return event

    async def list_after(
        self,
        task_id: str,
        event_id: int,
        *,
        limit: int = 100,
    ) -> tuple[EventRecord, ...]:
        if event_id < 0 or limit < 1 or limit > 100:
            raise ValueError("event_id and limit are outside the supported range.")
        async with self.database.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT * FROM events WHERE task_id = ? AND id > ? "
                    "ORDER BY id LIMIT ?",
                    (task_id, event_id, limit),
                )
            ).fetchall()
        return tuple(_event_from_row(row) for row in rows)


class ModelCallRepository:
    def __init__(self, database: SqliteDatabase):
        self.database = database

    async def add(self, record: ModelCallRecord) -> None:
        async with self.database.connection() as connection:
            await connection.execute(
                "INSERT INTO model_calls("
                "task_id, stage, provider, model, input_tokens, output_tokens, total_tokens, "
                "duration_ms, attempt_count, outcome, error_type, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.task_id,
                    record.stage,
                    record.provider,
                    record.model,
                    record.input_tokens,
                    record.output_tokens,
                    record.total_tokens,
                    record.duration_ms,
                    record.attempt_count,
                    record.outcome,
                    record.error_type,
                    _serialize_time(record.created_at),
                ),
            )
            await connection.commit()
