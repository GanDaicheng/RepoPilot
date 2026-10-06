"""Serializable LangGraph state for one RepoPilot task."""

from __future__ import annotations

from typing import TypedDict


class RepoPilotState(TypedDict):
    task_id: str
    thread_id: str
    repo_path: str
    user_request: str
    test_command: str
    base_commit: str | None
    worktree_path: str | None
    repo_summary: str | None
    change_plan: dict[str, object] | None
    patch_text: str | None
    changed_files: list[str]
    test_exit_code: int | None
    test_output: str
    tests_passed: bool
    failure_analysis: dict[str, object] | None
    review_decision: dict[str, object] | None
    retry_count: int
    max_retries: int
    cancel_requested: bool
    pending_approval: dict[str, object] | None
    current_stage: str
    error_type: str | None
    error_message: str | None


def initial_state(
    *,
    task_id: str,
    thread_id: str,
    repo_path: str,
    user_request: str,
    test_command: str,
    max_retries: int,
) -> RepoPilotState:
    return RepoPilotState(
        task_id=task_id,
        thread_id=thread_id,
        repo_path=repo_path,
        user_request=user_request,
        test_command=test_command,
        base_commit=None,
        worktree_path=None,
        repo_summary=None,
        change_plan=None,
        patch_text=None,
        changed_files=[],
        test_exit_code=None,
        test_output="",
        tests_passed=False,
        failure_analysis=None,
        review_decision=None,
        retry_count=0,
        max_retries=max_retries,
        cancel_requested=False,
        pending_approval=None,
        current_stage="starting",
        error_type=None,
        error_message=None,
    )
