from __future__ import annotations

from repopilot.graph.routing import (
    route_after_apply,
    route_after_capture,
    route_after_failure,
    route_after_plan,
    route_after_proposal,
    route_after_retry,
    route_after_review,
    route_after_summary,
    route_after_tests,
    route_after_validate,
    route_after_worktree,
)
from repopilot.graph.state import RepoPilotState


def test_graph_state_has_only_the_persisted_contract_fields() -> None:
    assert set(RepoPilotState.__annotations__) == {
        "task_id", "thread_id", "repo_path", "user_request", "test_command",
        "base_commit", "worktree_path", "repo_summary", "change_plan", "patch_text",
        "changed_files", "test_exit_code", "test_output", "tests_passed",
        "failure_analysis", "review_decision", "retry_count", "max_retries",
        "cancel_requested", "pending_approval", "current_stage", "error_type",
        "error_message",
    }


def state(**overrides: object) -> RepoPilotState:
    value: RepoPilotState = {
        "task_id": "task-1",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "repo_path": "/repo",
        "user_request": "Change code",
        "test_command": "pytest -q",
        "base_commit": None,
        "worktree_path": None,
        "repo_summary": None,
        "change_plan": None,
        "patch_text": None,
        "changed_files": [],
        "test_exit_code": None,
        "test_output": "",
        "tests_passed": False,
        "failure_analysis": None,
        "review_decision": None,
        "retry_count": 0,
        "max_retries": 2,
        "cancel_requested": False,
        "pending_approval": None,
        "current_stage": "apply_patch",
        "error_type": None,
        "error_message": None,
    }
    value.update(overrides)  # type: ignore[typeddict-item]
    return value


def test_initial_patch_success_routes_to_tests() -> None:
    assert route_after_apply(state()) == "run_tests"


def test_test_failure_routes_to_failure_analysis() -> None:
    assert route_after_tests(state(current_stage="run_tests", test_exit_code=1)) == "inspect_failure"


def test_infrastructure_failure_does_not_consume_code_retry() -> None:
    failed = state(current_stage="run_tests", error_type="docker_run_failed")
    assert route_after_tests(failed) == "failed_report"
    assert failed["retry_count"] == 0


def test_fixable_failure_and_critical_review_route_to_retry_scheduler() -> None:
    failure = state(failure_analysis={"fixable": True}, retry_count=1)
    review = state(
        review_decision={
            "approved": False,
            "critical_findings": [{"severity": "critical"}],
        },
        retry_count=1,
    )
    assert route_after_failure(failure) == "schedule_retry"
    assert route_after_review(review) == "schedule_retry"


def test_exact_retry_limit_is_exhausted() -> None:
    exhausted = state(failure_analysis={"fixable": True}, retry_count=2, max_retries=2)
    assert route_after_failure(exhausted) == "failed_report"


def test_review_approval_routes_to_success() -> None:
    approved = state(review_decision={"approved": True, "critical_findings": []})
    assert route_after_review(approved) == "success_report"


def test_approval_and_cancellation_override_every_nonterminal_route() -> None:
    approval = state(pending_approval={"action": "delete_files"})
    cancelled = state(cancel_requested=True)
    for router in (
        route_after_validate,
        route_after_capture,
        route_after_worktree,
        route_after_summary,
        route_after_plan,
        route_after_proposal,
        route_after_apply,
        route_after_tests,
        route_after_failure,
        route_after_review,
        route_after_retry,
    ):
        assert router(approval) == "awaiting_approval"
        assert router(cancelled) == "cancelled_report"


def test_terminal_stage_routes_are_stable() -> None:
    assert route_after_review(state(current_stage="succeeded")) == "end"
    assert route_after_failure(state(current_stage="failed")) == "end"
    assert route_after_apply(state(current_stage="cancelled")) == "end"
