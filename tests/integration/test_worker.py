from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from repopilot.domain.tasks import EventType, TaskInput, TaskStatus
from repopilot.models.profiles import ModelProfileRegistry
from repopilot.persistence.database import SqliteDatabase
from repopilot.persistence.repositories import TaskRepository
from repopilot.services.task_service import TaskService
from repopilot.services.worker import TaskWorker


pytestmark = pytest.mark.integration


async def setup(tmp_path: Path) -> tuple[TaskService, TaskRepository]:
    database = SqliteDatabase(tmp_path / "worker.sqlite3")
    await database.initialize()
    tasks = TaskRepository(database)
    return TaskService(tasks, ModelProfileRegistry.from_env({}), (tmp_path,)), tasks


def value(repo: Path) -> TaskInput:
    return TaskInput(repo, "Change", "pytest -q", "fake", 1)


class Graph:
    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.result = result or {"current_stage": "succeeded", "changed_files": []}
        self.error = error
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def ainvoke(self, graph_input, config):
        self.calls.append((graph_input, config))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_worker_claims_new_task_once_and_maps_graph_success(
    git_repo: Path, tmp_path: Path
) -> None:
    service, tasks = await setup(tmp_path)
    created = await service.create_task(value(git_repo))
    graph = Graph()
    worker = TaskWorker(tasks, lambda task: graph, poll_interval=0.01)

    assert await worker.run_once() is True
    assert await worker.run_once() is False

    finished = await service.get_task(created.id)
    assert finished.status is TaskStatus.SUCCEEDED
    assert graph.calls[0][0]["task_id"] == created.id
    assert graph.calls[0][1]["configurable"]["thread_id"] == created.thread_id


@pytest.mark.asyncio
async def test_worker_reuses_original_thread_and_checkpoint_for_running_task(
    git_repo: Path, tmp_path: Path
) -> None:
    service, tasks = await setup(tmp_path)
    created = await service.create_task(value(git_repo))
    await tasks.claim_next()
    await tasks.update_execution(created.id, current_stage="apply_patch")
    graph = Graph()
    worker = TaskWorker(tasks, lambda task: graph)

    assert await worker.run_once() is True

    assert graph.calls[0][0] is None
    assert graph.calls[0][1]["configurable"]["thread_id"] == created.thread_id


@pytest.mark.asyncio
async def test_worker_ignores_awaiting_and_terminal_tasks(
    git_repo: Path, tmp_path: Path
) -> None:
    service, tasks = await setup(tmp_path)
    awaiting = await service.create_task(value(git_repo))
    await tasks.claim_next()
    await tasks.transition(
        awaiting.id, TaskStatus.AWAITING_APPROVAL, stage="awaiting_approval",
        event_type=EventType.APPROVAL_REQUIRED, payload={},
    )
    graph = Graph()
    worker = TaskWorker(tasks, lambda task: graph)

    assert await worker.run_once() is False
    assert graph.calls == []


@pytest.mark.asyncio
async def test_worker_redacts_top_level_exception_and_fails_task(
    git_repo: Path, tmp_path: Path
) -> None:
    service, tasks = await setup(tmp_path)
    created = await service.create_task(value(git_repo))
    marker = "private-provider-detail"
    worker = TaskWorker(tasks, lambda task: Graph(error=RuntimeError(marker)))

    assert await worker.run_once() is True

    failed = await service.get_task(created.id)
    assert failed.status is TaskStatus.FAILED
    assert failed.error_type == "internal_error"
    assert marker not in (failed.error_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"current_stage": "failed", "error_type": "retry_exhausted"}, TaskStatus.FAILED),
        (
            {"current_stage": "awaiting_approval", "__interrupt__": ()},
            TaskStatus.AWAITING_APPROVAL,
        ),
    ],
)
async def test_worker_maps_graph_failure_and_interrupt(
    git_repo: Path,
    tmp_path: Path,
    result: dict[str, object],
    expected: TaskStatus,
) -> None:
    service, tasks = await setup(tmp_path)
    created = await service.create_task(value(git_repo))
    worker = TaskWorker(tasks, lambda task: Graph(result=result))

    assert await worker.run_once() is True

    assert (await service.get_task(created.id)).status is expected


@pytest.mark.asyncio
async def test_cancellation_during_await_prevents_later_side_effect(
    git_repo: Path, tmp_path: Path
) -> None:
    service, tasks = await setup(tmp_path)
    created = await service.create_task(value(git_repo))
    started = asyncio.Event()
    release = asyncio.Event()
    later_side_effects = 0

    class CancelAwareGraph:
        async def ainvoke(self, graph_input, config):
            nonlocal later_side_effects
            del graph_input, config
            started.set()
            await release.wait()
            persisted = await tasks.get(created.id)
            if persisted is not None and persisted.cancel_requested:
                return {"current_stage": "cancelled"}
            later_side_effects += 1
            return {"current_stage": "succeeded"}

    worker = TaskWorker(tasks, lambda task: CancelAwareGraph())
    running = asyncio.create_task(worker.run_once())
    await started.wait()
    await service.cancel_task(created.id)
    release.set()
    await running

    assert later_side_effects == 0
    assert (await service.get_task(created.id)).status is TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_start_and_stop_cleanly_end_single_worker_loop(tmp_path: Path) -> None:
    _, tasks = await setup(tmp_path)
    worker = TaskWorker(tasks, lambda task: Graph(), poll_interval=0.01)

    worker.start()
    await asyncio.sleep(0)
    await worker.stop()

    assert worker.running is False


@pytest.mark.asyncio
async def test_worker_loop_recovers_after_transient_claim_failure() -> None:
    recovered = asyncio.Event()

    class FlakyRepository:
        def __init__(self) -> None:
            self.calls = 0

        async def claim_next(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database temporarily unavailable")
            recovered.set()
            return None

    repository = FlakyRepository()
    worker = TaskWorker(repository, lambda task: Graph(), poll_interval=0.001)  # type: ignore[arg-type]

    worker.start()
    await asyncio.wait_for(recovered.wait(), timeout=1)
    await worker.stop()

    assert repository.calls >= 2
    assert worker.running is False
