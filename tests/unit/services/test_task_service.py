from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from repopilot.domain.tasks import EventType, TaskInput, TaskStatus
from repopilot.models.profiles import ModelProfileRegistry
from repopilot.persistence.database import SqliteDatabase
from repopilot.persistence.repositories import EventRepository, TaskRepository
from repopilot.services.task_service import TaskService, TaskServiceError
from tests.helpers.git import init_repo


async def make_service(tmp_path: Path) -> tuple[TaskService, TaskRepository]:
    database = SqliteDatabase(tmp_path / "service.sqlite3")
    await database.initialize()
    tasks = TaskRepository(database)
    return TaskService(tasks, ModelProfileRegistry.from_env({}), (tmp_path,)), tasks


def task_input(repo: Path, **overrides: object) -> TaskInput:
    values: dict[str, object] = {
        "repo_path": repo,
        "user_request": "Change the value safely",
        "test_command": "pytest -q",
        "model_profile": "fake",
        "max_retries": 2,
    }
    values.update(overrides)
    return TaskInput(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_validates_and_persists_uuid_identifiers(
    git_repo: Path, tmp_path: Path
) -> None:
    service, _ = await make_service(tmp_path)

    task = await service.create_task(task_input(git_repo))

    assert UUID(task.id).version == 4
    assert UUID(task.thread_id).version == 4
    assert task.status is TaskStatus.QUEUED
    assert task.repo_path == git_repo.resolve()
    assert await service.get_task(task.id) == task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"user_request": "   "}, "invalid_request"),
        ({"user_request": "x" * 20_001}, "invalid_request"),
        ({"user_request": "use " + "sk-" + "x" * 20}, "secret_detected"),
        ({"test_command": "pytest && rm everything"}, "command_denied"),
        ({"model_profile": "not-allowed"}, "unknown_model_profile"),
        ({"max_retries": -1}, "invalid_retry_limit"),
        ({"max_retries": 6}, "invalid_retry_limit"),
    ],
)
async def test_rejects_invalid_input_before_persistence(
    git_repo: Path,
    tmp_path: Path,
    overrides: dict[str, object],
    error_code: str,
) -> None:
    service, tasks = await make_service(tmp_path)

    with pytest.raises(TaskServiceError) as caught:
        await service.create_task(task_input(git_repo, **overrides))

    assert caught.value.error_code == error_code
    assert await tasks.claim_next() is None


@pytest.mark.asyncio
async def test_rejects_non_repository_but_accepts_command_requiring_later_approval(
    git_repo: Path, tmp_path: Path
) -> None:
    service, _ = await make_service(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(TaskServiceError) as caught:
        await service.create_task(task_input(plain))
    approved_later = await service.create_task(
        task_input(git_repo, test_command="npm test")
    )

    assert caught.value.error_code == "invalid_repository"
    assert approved_later.status is TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_rejects_repository_outside_configured_roots_before_persistence(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = init_repo(tmp_path / "outside", {"app.py": "value = 1\n"})
    database = SqliteDatabase(tmp_path / "roots.sqlite3")
    await database.initialize()
    tasks = TaskRepository(database)
    service = TaskService(tasks, ModelProfileRegistry.from_env({}), (allowed,))

    with pytest.raises(TaskServiceError) as caught:
        await service.create_task(task_input(outside))

    assert caught.value.error_code == "repository_not_allowed"
    assert await tasks.claim_next() is None


@pytest.mark.asyncio
async def test_missing_duplicate_and_terminal_cancel_conflicts(
    git_repo: Path, tmp_path: Path
) -> None:
    service, tasks = await make_service(tmp_path)
    with pytest.raises(TaskServiceError) as missing:
        await service.get_task("missing")
    assert missing.value.error_code == "task_not_found"

    queued = await service.create_task(task_input(git_repo))
    cancelled = await service.cancel_task(queued.id)
    assert cancelled.status is TaskStatus.CANCELLED
    with pytest.raises(TaskServiceError) as duplicate:
        await service.cancel_task(queued.id)
    assert duplicate.value.error_code == "task_conflict"

    terminal = await service.create_task(task_input(git_repo))
    await tasks.claim_next()
    await tasks.transition(
        terminal.id,
        TaskStatus.SUCCEEDED,
        stage="succeeded",
        event_type=EventType.TASK_SUCCEEDED,
        payload={},
    )
    with pytest.raises(TaskServiceError) as ended:
        await service.cancel_task(terminal.id)
    assert ended.value.error_code == "task_conflict"
