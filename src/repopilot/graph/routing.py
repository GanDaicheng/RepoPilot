"""Pure routing decisions for the RepoPilot state machine."""

from __future__ import annotations

from repopilot.graph.state import RepoPilotState


TERMINAL_STAGES = frozenset({"succeeded", "failed", "cancelled"})


def _override(state: RepoPilotState) -> str | None:
    if state["current_stage"] in TERMINAL_STAGES:
        return "end"
    if state["cancel_requested"]:
        return "cancelled_report"
    if state["pending_approval"] is not None:
        return "awaiting_approval"
    if state["error_type"] is not None:
        return "failed_report"
    return None


def route_after_apply(state: RepoPilotState) -> str:
    return _override(state) or "run_tests"


def route_after_validate(state: RepoPilotState) -> str:
    return _override(state) or "capture_base_commit"


def route_after_capture(state: RepoPilotState) -> str:
    return _override(state) or "create_worktree"


def route_after_worktree(state: RepoPilotState) -> str:
    return _override(state) or "summarize_repository"


def route_after_summary(state: RepoPilotState) -> str:
    return _override(state) or "plan_change"


def route_after_plan(state: RepoPilotState) -> str:
    return _override(state) or "propose_patch"


def route_after_proposal(state: RepoPilotState) -> str:
    return _override(state) or "apply_patch"


def route_after_retry(state: RepoPilotState) -> str:
    return _override(state) or "propose_fix"


def route_after_tests(state: RepoPilotState) -> str:
    overridden = _override(state)
    if overridden is not None:
        return overridden
    return "review_change" if state["tests_passed"] else "inspect_failure"


def route_after_failure(state: RepoPilotState) -> str:
    overridden = _override(state)
    if overridden is not None:
        return overridden
    analysis = state["failure_analysis"] or {}
    if bool(analysis.get("fixable")) and state["retry_count"] < state["max_retries"]:
        return "schedule_retry"
    return "failed_report"


def route_after_review(state: RepoPilotState) -> str:
    overridden = _override(state)
    if overridden is not None:
        return overridden
    decision = state["review_decision"] or {}
    if bool(decision.get("approved")):
        return "success_report"
    critical = decision.get("critical_findings")
    if critical and state["retry_count"] < state["max_retries"]:
        return "schedule_retry"
    return "failed_report"
