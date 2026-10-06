from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from repopilot.domain.tasks import (
    EventType,
    ModelCallRecord,
    TaskInput,
    TaskStatus,
)
from repopilot.persistence.database import SqliteDatabase
from repopilot.persistence.repositories import (
    EventRepository,
    InvalidTransitionError,
    InvalidUpdateFieldError,
    ModelCallRepository,
    TaskConflictError,
    TaskRepository,
)


NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def stores(tmp_path: Path):
    database = SqliteDatabase(tmp_path / "repopilot.sqlite3")
    await database.initialize()
    yield (
        database,
        TaskRepository(database),
        EventRepository(database),
        ModelCallRepository(database),
    )


def task_input(name: str = "one") -> TaskInput:
    return TaskInput(
        repo_path=Path(f"C:/repos/{name}"),
        user_request=f"Fix {name}",
        test_command="pytest -q",
        model_profile="fake",
        max_retries=2,
    )


@pytest.mark.asyncio
async def test_initialize_creates_only_m2_tables_and_required_pragmas(
    stores,
) -> None:
    database, _, _, _ = stores

    async with database.connection() as connection:
        table_rows = await (
            await connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ).fetchall()
        version = await (
            await connection.execute("SELECT version FROM schema_version")
        ).fetchone()
        foreign_keys = await (
            await connection.execute("PRAGMA foreign_keys")
        ).fetchone()
        journal_mode = await (
            await connection.execute("PRAGMA journal_mode")
        ).fetchone()
        busy_timeout = await (
            await connection.execute("PRAGMA busy_timeout")
        ).fetchone()

    assert [row["name"] for row in table_rows] == [
        "events",
        "model_calls",
        "schema_version",
        "tasks",
    ]
    assert version["version"] == 1
    assert foreign_keys[0] == 1
    assert journal_mode[0] == "wal"
    assert busy_timeout[0] == 5_000


@pytest.mark.asyncio
async def test_create_persists_task_and_created_event_atomically(stores) -> None:
    database, tasks, events, _ = stores

    created = await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")
    stored_events = await events.list_after("task-1", 0)

    assert created.status is TaskStatus.QUEUED
    assert created.repo_path == Path("C:/repos/one")
    assert [(event.event_type, dict(event.payload)) for event in stored_events] == [
        (EventType.TASK_CREATED, {"status": "queued"})
    ]

    async with database.connection() as connection:
        await connection.execute(
            "CREATE TRIGGER fail_created_event BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'event refused'); END"
        )
        await connection.commit()

    with pytest.raises(aiosqlite.IntegrityError, match="event refused"):
        await tasks.create(task_input("two"), task_id="task-2", thread_id="thread-2")
    assert await tasks.get("task-2") is None


@pytest.mark.asyncio
async def test_claim_prefers_recovery_without_duplicate_started_event(stores) -> None:
    _, tasks, events, _ = stores
    await tasks.create(task_input("old"), task_id="task-old", thread_id="thread-old")
    await tasks.create(task_input("new"), task_id="task-new", thread_id="thread-new")
    await tasks.transition(
        "task-old",
        TaskStatus.RUNNING,
        stage="starting",
        event_type=EventType.TASK_STARTED,
        payload={"status": "running"},
        dedupe_key="task-old:started",
    )

    recovered = await tasks.claim_next()
    recovered_events = await events.list_after("task-old", 0)

    assert recovered is not None
    assert recovered.id == "task-old"
    assert recovered.status is TaskStatus.RUNNING
    assert [event.event_type for event in recovered_events].count(EventType.TASK_STARTED) == 1

    await tasks.transition(
        "task-old",
        TaskStatus.SUCCEEDED,
        stage="complete",
        event_type=EventType.TASK_SUCCEEDED,
        payload={"status": "succeeded"},
    )
    claimed = await tasks.claim_next()
    assert claimed is not None
    assert claimed.id == "task-new"
    assert claimed.status is TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_invalid_transition_rolls_back_task_and_event(stores) -> None:
    _, tasks, events, _ = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")

    with pytest.raises(InvalidTransitionError):
        await tasks.transition(
            "task-1",
            TaskStatus.SUCCEEDED,
            stage="complete",
            event_type=EventType.TASK_SUCCEEDED,
            payload={"status": "succeeded"},
        )

    stored = await tasks.get("task-1")
    assert stored is not None
    assert stored.status is TaskStatus.QUEUED
    assert [event.event_type for event in await events.list_after("task-1", 0)] == [
        EventType.TASK_CREATED
    ]


@pytest.mark.asyncio
async def test_transition_dedupe_key_is_idempotent(stores) -> None:
    _, tasks, events, _ = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")

    first = await tasks.transition(
        "task-1",
        TaskStatus.RUNNING,
        stage="starting",
        event_type=EventType.TASK_STARTED,
        payload={"status": "running"},
        dedupe_key="task-1:started",
    )
    repeated = await tasks.transition(
        "task-1",
        TaskStatus.RUNNING,
        stage="starting",
        event_type=EventType.TASK_STARTED,
        payload={"status": "running"},
        dedupe_key="task-1:started",
    )

    assert repeated == first
    stored_events = await events.list_after("task-1", 0)
    assert [event.dedupe_key for event in stored_events] == [
        "task-1:created",
        "task-1:started",
    ]


@pytest.mark.asyncio
async def test_event_ids_are_monotonic_and_list_after_is_task_scoped(stores) -> None:
    _, tasks, events, _ = stores
    await tasks.create(task_input("one"), task_id="task-1", thread_id="thread-1")
    await tasks.create(task_input("two"), task_id="task-2", thread_id="thread-2")
    appended = await events.append(
        "task-1",
        EventType.STAGE_STARTED,
        stage="planning",
        payload={"stage": "planning"},
        dedupe_key="task-1:planning:0",
    )

    initial = await events.list_after("task-1", 0)
    resumed = await events.list_after("task-1", initial[0].id)

    assert [event.id for event in initial] == sorted(event.id for event in initial)
    assert [event.id for event in resumed] == [appended.id]
    assert all(event.task_id == "task-1" for event in initial)
    assert all(event.id != 2 for event in initial)


@pytest.mark.asyncio
async def test_event_append_with_same_dedupe_key_returns_original(stores) -> None:
    _, tasks, events, _ = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")

    first = await events.append(
        "task-1",
        EventType.STAGE_STARTED,
        stage="planning",
        payload={"attempt": 1},
        dedupe_key="task-1:planning:0",
    )
    repeated = await events.append(
        "task-1",
        EventType.STAGE_STARTED,
        stage="planning",
        payload={"attempt": 999},
        dedupe_key="task-1:planning:0",
    )

    assert repeated == first
    assert dict(repeated.payload) == {"attempt": 1}


@pytest.mark.asyncio
async def test_request_cancel_handles_queued_running_and_waiting(stores) -> None:
    _, tasks, events, _ = stores
    await tasks.create(task_input("queued"), task_id="queued", thread_id="thread-q")
    await tasks.create(task_input("running"), task_id="running", thread_id="thread-r")
    await tasks.create(task_input("waiting"), task_id="waiting", thread_id="thread-w")
    await tasks.transition(
        "running",
        TaskStatus.RUNNING,
        stage="starting",
        event_type=EventType.TASK_STARTED,
        payload={},
    )
    await tasks.transition(
        "waiting",
        TaskStatus.RUNNING,
        stage="starting",
        event_type=EventType.TASK_STARTED,
        payload={},
    )
    await tasks.transition(
        "waiting",
        TaskStatus.AWAITING_APPROVAL,
        stage="apply_patch",
        event_type=EventType.APPROVAL_REQUIRED,
        payload={"action": "delete"},
    )

    queued = await tasks.request_cancel("queued")
    running = await tasks.request_cancel("running")
    waiting = await tasks.request_cancel("waiting")

    assert (queued.status, queued.cancel_requested) == (TaskStatus.CANCELLED, True)
    assert (running.status, running.cancel_requested) == (TaskStatus.RUNNING, True)
    assert (waiting.status, waiting.cancel_requested) == (TaskStatus.CANCELLED, True)
    assert EventType.TASK_CANCELLED in {
        event.event_type for event in await events.list_after("queued", 0)
    }
    assert EventType.TASK_CANCELLED in {
        event.event_type for event in await events.list_after("waiting", 0)
    }
    assert EventType.TASK_CANCELLED not in {
        event.event_type for event in await events.list_after("running", 0)
    }

    with pytest.raises(TaskConflictError):
        await tasks.request_cancel("running")


@pytest.mark.asyncio
async def test_terminal_task_cancel_is_a_conflict(stores) -> None:
    _, tasks, _, _ = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")
    await tasks.transition(
        "task-1",
        TaskStatus.RUNNING,
        stage="starting",
        event_type=EventType.TASK_STARTED,
        payload={},
    )
    await tasks.transition(
        "task-1",
        TaskStatus.SUCCEEDED,
        stage="complete",
        event_type=EventType.TASK_SUCCEEDED,
        payload={},
    )

    with pytest.raises(TaskConflictError):
        await tasks.request_cancel("task-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "event_type"),
    [
        (TaskStatus.SUCCEEDED, EventType.TASK_SUCCEEDED),
        (TaskStatus.FAILED, EventType.TASK_FAILED),
        (TaskStatus.AWAITING_APPROVAL, EventType.APPROVAL_REQUIRED),
    ],
)
async def test_persisted_cancel_atomically_wins_over_finalization(
    stores, target: TaskStatus, event_type: EventType
) -> None:
    _, tasks, events, _ = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")
    await tasks.claim_next()
    await tasks.request_cancel("task-1")

    result = await tasks.transition(
        "task-1",
        target,
        stage=target.value,
        event_type=event_type,
        payload={"unsafe": "result"},
        dedupe_key=f"task-1:{target.value}",
    )

    assert result.status is TaskStatus.CANCELLED
    assert result.current_stage == "cancelled"
    emitted = await events.list_after("task-1", 0)
    assert emitted[-1].event_type is EventType.TASK_CANCELLED
    assert all(event.event_type is not event_type for event in emitted)


@pytest.mark.asyncio
async def test_base_commit_is_set_once(stores) -> None:
    _, tasks, _, _ = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")

    first = await tasks.set_base_commit_once("task-1", "a" * 40)
    repeated = await tasks.set_base_commit_once("task-1", "b" * 40)

    assert first.base_commit == "a" * 40
    assert repeated.base_commit == "a" * 40


@pytest.mark.asyncio
async def test_update_execution_accepts_only_whitelisted_fields(stores) -> None:
    _, tasks, _, _ = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")

    updated = await tasks.update_execution(
        "task-1",
        current_stage="planning",
        worktree_path=Path("C:/managed/task-1"),
        retry_count=1,
    )

    assert updated.current_stage == "planning"
    assert updated.base_commit is None
    assert updated.worktree_path == Path("C:/managed/task-1")
    assert updated.retry_count == 1
    with pytest.raises(InvalidUpdateFieldError):
        await tasks.update_execution("task-1", status="succeeded")
    with pytest.raises(InvalidUpdateFieldError):
        await tasks.update_execution("task-1", base_commit="a" * 40)


@pytest.mark.asyncio
async def test_model_call_persists_usage_without_prompt_columns(stores) -> None:
    database, tasks, _, model_calls = stores
    await tasks.create(task_input(), task_id="task-1", thread_id="thread-1")
    record = ModelCallRecord(
        id=None,
        task_id="task-1",
        stage="planning",
        provider="fake",
        model="scripted",
        input_tokens=3,
        output_tokens=5,
        total_tokens=8,
        duration_ms=4,
        attempt_count=1,
        outcome="succeeded",
        error_type=None,
        created_at=NOW,
    )

    await model_calls.add(record)

    async with database.connection() as connection:
        columns = await (
            await connection.execute("PRAGMA table_info(model_calls)")
        ).fetchall()
        stored = await (
            await connection.execute(
                "SELECT input_tokens, output_tokens, total_tokens FROM model_calls"
            )
        ).fetchone()

    names = {row["name"] for row in columns}
    assert {"prompt", "response", "api_key", "headers"}.isdisjoint(names)
    assert tuple(stored) == (3, 5, 8)
