"""Validated business entry points for persistent RepoPilot tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from repopilot.domain.tasks import TaskInput, TaskRecord
from repopilot.models.profiles import ModelProfileError, ModelProfileRegistry
from repopilot.persistence.repositories import (
    TaskConflictError,
    TaskNotFoundError,
    TaskRepository,
)
from repopilot.security.commands import evaluate_test_command
from repopilot.security.secrets import find_secret_kind
from repopilot.workspace.worktree import capture_repo_snapshot


MAX_REQUEST_CHARS = 20_000
MAX_COMMAND_CHARS = 2_000


class TaskServiceError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class TaskService:
    def __init__(
        self,
        task_repository: TaskRepository,
        profile_registry: ModelProfileRegistry,
    ) -> None:
        self.task_repository = task_repository
        self.profile_registry = profile_registry

    async def create_task(self, task_input: TaskInput) -> TaskRecord:
        request = task_input.user_request.strip()
        if not request or len(request) > MAX_REQUEST_CHARS:
            raise TaskServiceError(
                "invalid_request",
                "The request must be non-empty and within the configured length limit.",
            )
        if find_secret_kind(request) is not None:
            raise TaskServiceError(
                "secret_detected",
                "The request appears to contain a credential and was not stored.",
            )
        command = task_input.test_command.strip()
        if not command or len(command) > MAX_COMMAND_CHARS:
            raise TaskServiceError("command_denied", "The test command is invalid.")
        if find_secret_kind(command) is not None:
            raise TaskServiceError(
                "secret_detected",
                "The test command appears to contain a credential and was not stored.",
            )
        decision = evaluate_test_command(command)
        if decision.outcome == "deny":
            raise TaskServiceError("command_denied", decision.reason)
        if task_input.max_retries < 0 or task_input.max_retries > 5:
            raise TaskServiceError(
                "invalid_retry_limit",
                "max_retries must be between zero and five.",
            )
        try:
            self.profile_registry.resolve(task_input.model_profile)
        except ModelProfileError as exc:
            raise TaskServiceError(exc.error_code, str(exc)) from None

        try:
            repo_path = Path(task_input.repo_path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise TaskServiceError(
                "invalid_repository", "The repository path is not accessible."
            ) from None
        snapshot = await asyncio.to_thread(capture_repo_snapshot, repo_path)
        if not snapshot.ok:
            raise TaskServiceError(
                "invalid_repository", "The path is not an accessible Git repository."
            )

        normalized = TaskInput(
            repo_path=repo_path,
            user_request=request,
            test_command=command,
            model_profile=task_input.model_profile,
            max_retries=task_input.max_retries,
        )
        return await self.task_repository.create(
            normalized,
            task_id=str(uuid4()),
            thread_id=str(uuid4()),
        )

    async def get_task(self, task_id: str) -> TaskRecord:
        task = await self.task_repository.get(task_id)
        if task is None:
            raise TaskServiceError("task_not_found", "The requested task does not exist.")
        return task

    async def cancel_task(self, task_id: str) -> TaskRecord:
        try:
            return await self.task_repository.request_cancel(task_id)
        except TaskNotFoundError:
            raise TaskServiceError("task_not_found", "The requested task does not exist.") from None
        except TaskConflictError:
            raise TaskServiceError("task_conflict", "The task cannot be cancelled.") from None
