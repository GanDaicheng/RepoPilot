"""Bounded public request and response schemas."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from repopilot.domain.tasks import TaskInput, TaskRecord, TaskStatus


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_path: Annotated[str, StringConstraints(min_length=1, max_length=4_096)]
    user_request: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    test_command: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    model_profile: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "fake"
    max_retries: int = Field(default=2, ge=0, le=5)

    def to_domain(self) -> TaskInput:
        return TaskInput(
            repo_path=Path(self.repo_path),
            user_request=self.user_request,
            test_command=self.test_command,
            model_profile=self.model_profile,
            max_retries=self.max_retries,
        )


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    thread_id: str
    model_profile: str
    status: TaskStatus
    current_stage: str
    base_commit: str | None
    worktree_path: str | None
    retry_count: int
    max_retries: int
    cancel_requested: bool
    error_type: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_record(cls, record: TaskRecord) -> TaskResponse:
        return cls(
            id=record.id,
            thread_id=record.thread_id,
            model_profile=record.model_profile,
            status=record.status,
            current_stage=record.current_stage,
            base_commit=record.base_commit,
            worktree_path=(
                None if record.worktree_path is None else str(record.worktree_path)
            ),
            retry_count=record.retry_count,
            max_retries=record.max_retries,
            cancel_requested=record.cancel_requested,
            error_type=record.error_type,
            error_message=record.error_message,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            updated_at=record.updated_at,
        )


class HealthResponse(BaseModel):
    service: str
    database: str
    worker: str
