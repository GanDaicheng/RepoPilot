"""Single-process repository-backed task worker with checkpoint recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from repopilot.domain.tasks import EventType, TaskRecord, TaskStatus
from repopilot.graph.builder import graph_config
from repopilot.graph.state import initial_state
from repopilot.persistence.repositories import TaskRepository


class InvokableGraph(Protocol):
    async def ainvoke(self, graph_input: object, config: object) -> Mapping[str, object]: ...


GraphFactory = Callable[[TaskRecord], InvokableGraph]


class TaskWorker:
    def __init__(
        self,
        task_repository: TaskRepository,
        graph_factory: GraphFactory,
        *,
        poll_interval: float = 0.25,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive.")
        self.task_repository = task_repository
        self.graph_factory = graph_factory
        self.poll_interval = poll_interval
        self._stop_event = asyncio.Event()
        self._runner_task: asyncio.Task[None] | None = None
        self._run_lock = asyncio.Lock()
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._runner_task is not None and not self._runner_task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(
            self.run_forever(), name="repopilot-task-worker"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._runner_task
        if task is not None:
            await task
        self._runner_task = None

    async def run_forever(self) -> None:
        backoff = self.poll_interval
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
                self.last_error = None
                backoff = self.poll_interval
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "persistence_unavailable"
                processed = False
                backoff = min(max(backoff * 2, self.poll_interval), 5.0)
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=backoff
                )
            except TimeoutError:
                pass

    async def run_once(self) -> bool:
        async with self._run_lock:
            task = await self.task_repository.claim_next()
            if task is None:
                return False
            recovering = task.current_stage != "starting"
            graph_input: object = None
            if not recovering:
                graph_input = initial_state(
                    task_id=task.id,
                    thread_id=task.thread_id,
                    repo_path=str(task.repo_path),
                    user_request=task.user_request,
                    test_command=task.test_command,
                    max_retries=task.max_retries,
                )
            try:
                result = await self.graph_factory(task).ainvoke(
                    graph_input, graph_config(task.thread_id)
                )
                await self._reconcile_result(task.id, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_internal_error(task.id)
            return True

    async def _reconcile_result(
        self,
        task_id: str,
        result: Mapping[str, object],
    ) -> None:
        current = await self.task_repository.get(task_id)
        if current is None or current.status is not TaskStatus.RUNNING:
            return
        stage = str(result.get("current_stage", ""))
        if stage == "cancelled" or current.cancel_requested:
            await self.task_repository.transition(
                task_id, TaskStatus.CANCELLED, stage="cancelled",
                event_type=EventType.TASK_CANCELLED,
                payload={"status": "cancelled"}, dedupe_key=f"{task_id}:cancelled",
                error_type="cancelled", error_message="The task was cancelled.",
            )
        elif stage == "succeeded":
            await self.task_repository.transition(
                task_id, TaskStatus.SUCCEEDED, stage="succeeded",
                event_type=EventType.TASK_SUCCEEDED,
                payload={"status": "succeeded"}, dedupe_key=f"{task_id}:succeeded",
            )
        elif stage == "failed":
            code = str(result.get("error_type") or "internal_error")[:128]
            await self.task_repository.transition(
                task_id, TaskStatus.FAILED, stage="failed",
                event_type=EventType.TASK_FAILED, payload={"error_type": code},
                dedupe_key=f"{task_id}:failed", error_type=code,
                error_message="The task failed during execution.",
            )
        elif "__interrupt__" in result:
            await self.task_repository.transition(
                task_id, TaskStatus.AWAITING_APPROVAL, stage="awaiting_approval",
                event_type=EventType.APPROVAL_REQUIRED,
                payload={"action": "approval_required"},
                dedupe_key=f"{task_id}:awaiting_approval",
            )

    async def _record_internal_error(self, task_id: str) -> None:
        current = await self.task_repository.get(task_id)
        if current is None or current.status is not TaskStatus.RUNNING:
            return
        await self.task_repository.transition(
            task_id, TaskStatus.FAILED, stage="failed",
            event_type=EventType.TASK_FAILED,
            payload={"error_type": "internal_error"},
            dedupe_key=f"{task_id}:failed", error_type="internal_error",
            error_message="The task stopped because of an internal execution error.",
        )
