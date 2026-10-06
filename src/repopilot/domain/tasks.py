"""Persistent task, event, and model-call domain contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    STAGE_STARTED = "stage_started"
    TOOL_COMPLETED = "tool_completed"
    MODEL_COMPLETED = "model_completed"
    PATCH_APPLIED = "patch_applied"
    TEST_COMPLETED = "test_completed"
    RETRY_SCHEDULED = "retry_scheduled"
    APPROVAL_REQUIRED = "approval_required"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"


TERMINAL_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

_ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset({TaskStatus.CANCELLED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """Return whether the public task lifecycle permits a transition."""

    return target in _ALLOWED_TRANSITIONS[current]


def _require_aware(value: datetime | None) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("Task timestamps must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class TaskInput:
    repo_path: Path
    user_request: str
    test_command: str
    model_profile: str
    max_retries: int


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    thread_id: str
    repo_path: Path
    user_request: str
    test_command: str
    model_profile: str
    status: TaskStatus
    current_stage: str
    base_commit: str | None
    worktree_path: Path | None
    retry_count: int
    max_retries: int
    cancel_requested: bool
    error_type: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.created_at,
            self.started_at,
            self.finished_at,
            self.updated_at,
        ):
            _require_aware(value)


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    task_id: str
    event_type: EventType
    stage: str
    payload: Mapping[str, object]
    dedupe_key: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.created_at)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    id: int | None
    task_id: str
    stage: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    duration_ms: int
    attempt_count: int
    outcome: str
    error_type: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.created_at)
