from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

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


NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def test_task_statuses_match_the_public_lifecycle() -> None:
    assert [status.value for status in TaskStatus] == [
        "queued",
        "running",
        "awaiting_approval",
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert TERMINAL_STATUSES == frozenset(
        {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    )


def test_event_types_match_the_sse_contract() -> None:
    assert [event.value for event in EventType] == [
        "task_created",
        "task_started",
        "stage_started",
        "tool_completed",
        "model_completed",
        "patch_applied",
        "test_completed",
        "retry_scheduled",
        "approval_required",
        "task_succeeded",
        "task_failed",
        "task_cancelled",
    ]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.QUEUED, TaskStatus.RUNNING),
        (TaskStatus.QUEUED, TaskStatus.CANCELLED),
        (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
        (TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL),
        (TaskStatus.AWAITING_APPROVAL, TaskStatus.CANCELLED),
    ],
)
def test_allows_only_documented_status_transitions(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    assert can_transition(current, target) is True


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES, key=str))
@pytest.mark.parametrize("target", list(TaskStatus))
def test_terminal_statuses_reject_every_transition(
    terminal: TaskStatus,
    target: TaskStatus,
) -> None:
    assert can_transition(terminal, target) is False


def test_rejects_undocumented_and_self_transitions() -> None:
    assert can_transition(TaskStatus.QUEUED, TaskStatus.SUCCEEDED) is False
    assert can_transition(TaskStatus.AWAITING_APPROVAL, TaskStatus.RUNNING) is False
    assert can_transition(TaskStatus.RUNNING, TaskStatus.RUNNING) is False


def test_domain_records_are_frozen_and_use_aware_timestamps() -> None:
    task_input = TaskInput(
        repo_path=Path("C:/repo"),
        user_request="Fix division by zero",
        test_command="pytest -q",
        model_profile="fake",
        max_retries=2,
    )
    task = TaskRecord(
        id="task-1",
        thread_id="thread-1",
        repo_path=task_input.repo_path,
        user_request=task_input.user_request,
        test_command=task_input.test_command,
        model_profile=task_input.model_profile,
        status=TaskStatus.QUEUED,
        current_stage="queued",
        base_commit=None,
        worktree_path=None,
        retry_count=0,
        max_retries=task_input.max_retries,
        cancel_requested=False,
        error_type=None,
        error_message=None,
        created_at=NOW,
        started_at=None,
        finished_at=None,
        updated_at=NOW,
    )
    event = EventRecord(
        id=1,
        task_id=task.id,
        event_type=EventType.TASK_CREATED,
        stage="queued",
        payload={"status": "queued"},
        dedupe_key=None,
        created_at=NOW,
    )
    model_call = ModelCallRecord(
        id=None,
        task_id=task.id,
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

    with pytest.raises(FrozenInstanceError):
        task.status = TaskStatus.RUNNING  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        event.stage = "running"  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.payload["status"] = "running"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        model_call.total_tokens = 0  # type: ignore[misc]


def test_domain_records_reject_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        EventRecord(
            id=1,
            task_id="task-1",
            event_type=EventType.TASK_CREATED,
            stage="queued",
            payload={},
            dedupe_key=None,
            created_at=datetime(2026, 10, 6, 8, 0),
        )
