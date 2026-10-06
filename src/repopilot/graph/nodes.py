"""Deterministic, dependency-injected nodes for the RepoPilot graph."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from langgraph.types import interrupt

from repopilot.agent.repo_agent import RepoAgent
from repopilot.agent.schemas import ChangePlan
from repopilot.domain.results import ToolResult
from repopilot.domain.tasks import EventType, TaskStatus
from repopilot.graph.state import RepoPilotState
from repopilot.models.profiles import ResolvedModelProfile
from repopilot.security.commands import evaluate_test_command
from repopilot.tools.filesystem import list_files
from repopilot.tools.git import git_diff
from repopilot.tools.patch import PatchApplyData, apply_patch
from repopilot.tools.tests import DockerTestRunner
from repopilot.workspace.worktree import (
    RepoSnapshot,
    WorktreeManager,
    capture_repo_snapshot,
)


MAX_STATE_TEXT_BYTES = 200_000
MAX_SUMMARY_BYTES = 100_000


class TaskRepositoryLike(Protocol):
    async def get(self, task_id: str) -> Any: ...
    async def update_execution(self, task_id: str, **fields: object) -> Any: ...
    async def transition(self, task_id: str, target: TaskStatus, **kwargs: object) -> Any: ...


class EventRepositoryLike(Protocol):
    async def append(self, task_id: str, event_type: EventType, **kwargs: object) -> Any: ...


@dataclass(slots=True)
class GraphDependencies:
    task_repository: TaskRepositoryLike
    event_repository: EventRepositoryLike
    profile: ResolvedModelProfile
    repo_agent: RepoAgent
    worktree_manager: WorktreeManager
    test_runner: DockerTestRunner
    patcher: Callable[..., ToolResult[PatchApplyData]] = apply_patch
    base_capture: Callable[[Path], ToolResult[RepoSnapshot]] = capture_repo_snapshot


def operation_key(state: RepoPilotState, stage: str) -> str:
    return f"{state['task_id']}:{stage}:{state['retry_count']}"


def _bounded(value: str, max_bytes: int = MAX_STATE_TEXT_BYTES) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _safe_message(message: str) -> str:
    return _bounded(message, 4_096)


class GraphNodes:
    def __init__(self, dependencies: GraphDependencies) -> None:
        self.deps = dependencies

    async def _cancelled(self, state: RepoPilotState) -> bool:
        task = await self.deps.task_repository.get(state["task_id"])
        return bool(task is not None and task.cancel_requested)

    async def _start(self, state: RepoPilotState, stage: str) -> bool:
        if await self._cancelled(state):
            return True
        await self.deps.task_repository.update_execution(
            state["task_id"], current_stage=stage
        )
        await self.deps.event_repository.append(
            state["task_id"],
            EventType.STAGE_STARTED,
            stage=stage,
            payload={"retry_count": state["retry_count"]},
            dedupe_key=f"{operation_key(state, stage)}:started",
        )
        return False

    @staticmethod
    def _cancel_update(stage: str) -> dict[str, object]:
        return {"cancel_requested": True, "current_stage": stage}

    @staticmethod
    def _failure(stage: str, code: str, message: str) -> dict[str, object]:
        return {
            "current_stage": stage,
            "error_type": code,
            "error_message": _safe_message(message),
        }

    async def validate_request(self, state: RepoPilotState) -> dict[str, object]:
        stage = "validate_request"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        if not state["user_request"].strip() or not Path(state["repo_path"]).is_dir():
            return self._failure(stage, "validation_error", "The task request or repository is invalid.")
        if state["max_retries"] < 0 or state["max_retries"] > 5:
            return self._failure(stage, "validation_error", "The retry limit is outside the supported range.")
        decision = evaluate_test_command(state["test_command"])
        if decision.outcome == "deny":
            return self._failure(stage, "policy_denied", decision.reason)
        if decision.outcome == "approval_required":
            return {
                "current_stage": stage,
                "pending_approval": {"action": "test_command", "argv": list(decision.argv[:16])},
            }
        return {"current_stage": stage, "error_type": None, "error_message": None}

    async def capture_base_commit(self, state: RepoPilotState) -> dict[str, object]:
        stage = "capture_base_commit"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        result = await asyncio.to_thread(self.deps.base_capture, Path(state["repo_path"]))
        if not result.ok or result.data is None:
            return self._failure(stage, "workspace_error", result.message)
        await self.deps.task_repository.update_execution(
            state["task_id"], base_commit=result.data.head
        )
        return {"base_commit": result.data.head, "current_stage": stage}

    async def create_worktree(self, state: RepoPilotState) -> dict[str, object]:
        stage = "create_worktree"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        if await self._cancelled(state):
            return self._cancel_update(stage)
        if state["base_commit"] is None:
            return self._failure(stage, "workspace_error", "The base commit was not captured.")
        result = await asyncio.to_thread(
            self.deps.worktree_manager.create,
            Path(state["repo_path"]),
            state["task_id"],
            base_commit=state["base_commit"],
        )
        if not result.ok or result.data is None:
            return self._failure(stage, "workspace_error", result.message)
        path = str(result.data.path)
        await self.deps.task_repository.update_execution(state["task_id"], worktree_path=path)
        await self.deps.event_repository.append(
            state["task_id"],
            EventType.TOOL_COMPLETED,
            stage=stage,
            payload={"tool": "create_worktree", "ok": True},
            dedupe_key=operation_key(state, stage),
        )
        return {"worktree_path": path, "current_stage": stage}

    async def summarize_repository(self, state: RepoPilotState) -> dict[str, object]:
        stage = "summarize_repository"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        if state["worktree_path"] is None:
            return self._failure(stage, "workspace_error", "The worktree is unavailable.")
        result = await asyncio.to_thread(list_files, Path(state["worktree_path"]))
        if not result.ok:
            return self._failure(stage, "tool_error", result.message)
        summary = _bounded("\n".join(result.data or ()), MAX_SUMMARY_BYTES)
        await self.deps.event_repository.append(
            state["task_id"], EventType.TOOL_COMPLETED, stage=stage,
            payload={"tool": "list_files", "file_count": len(result.data or ())},
            dedupe_key=operation_key(state, stage),
        )
        return {"repo_summary": summary, "current_stage": stage}

    async def plan_change(self, state: RepoPilotState) -> dict[str, object]:
        stage = "plan_change"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        try:
            plan = await self.deps.repo_agent.plan(
                task_id=state["task_id"], profile=self.deps.profile,
                worktree_root=Path(state["worktree_path"] or ""),
                user_request=state["user_request"], repo_summary=state["repo_summary"] or "",
            )
        except Exception as exc:
            return self._failure(stage, getattr(exc, "error_code", "model_transport_error"), str(exc))
        await self._model_event(state, stage)
        return {"change_plan": plan.model_dump(mode="json"), "current_stage": stage}

    async def propose_patch(self, state: RepoPilotState) -> dict[str, object]:
        return await self._propose(state, "propose_patch")

    async def propose_fix(self, state: RepoPilotState) -> dict[str, object]:
        return await self._propose(state, "propose_fix")

    async def _propose(self, state: RepoPilotState, stage: str) -> dict[str, object]:
        if await self._start(state, stage):
            return self._cancel_update(stage)
        if state["change_plan"] is None:
            return self._failure(stage, "internal_error", "The change plan is missing.")
        try:
            plan = ChangePlan.model_validate(state["change_plan"])
            repair_context = state["repo_summary"] or ""
            if stage == "propose_fix":
                repair_context += "\nFailure summary: " + _bounded(state["test_output"], 20_000)
                repair_context += "\nPrior analysis: " + _bounded(
                    json.dumps(
                        {
                            "failure_analysis": state["failure_analysis"],
                            "review_decision": state["review_decision"],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    20_000,
                )
            proposal = await self.deps.repo_agent.propose_patch(
                task_id=state["task_id"], profile=self.deps.profile,
                worktree_root=Path(state["worktree_path"] or ""),
                plan=plan, repo_summary=repair_context,
            )
        except Exception as exc:
            return self._failure(stage, getattr(exc, "error_code", "model_transport_error"), str(exc))
        await self._model_event(state, stage)
        return {
            "patch_text": _bounded(proposal.patch_text),
            "current_stage": stage,
            "error_type": None,
            "error_message": None,
        }

    async def apply_patch(self, state: RepoPilotState) -> dict[str, object]:
        stage = "apply_patch"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        if await self._cancelled(state):
            return self._cancel_update(stage)
        if state["worktree_path"] is None or state["patch_text"] is None:
            return self._failure(stage, "patch_invalid", "The patch or worktree is missing.")
        result = await asyncio.to_thread(
            self.deps.patcher,
            Path(state["worktree_path"]),
            state["patch_text"],
            already_applied_ok=True,
        )
        if not result.ok:
            if result.error_code == "approval_required":
                return {
                    "current_stage": stage,
                    "pending_approval": {
                        "action": "delete_files",
                        "paths": list(result.metadata.get("paths", ()))[:20],
                    },
                }
            error_type = (
                "patch_invalid"
                if result.error_code in {"invalid_patch", "workspace_boundary_violation"}
                else "patch_apply_failed"
            )
            return self._failure(stage, error_type, result.message)
        data = result.data
        assert data is not None
        await self.deps.event_repository.append(
            state["task_id"], EventType.PATCH_APPLIED, stage=stage,
            payload={"files": list(data.changed_files)[:50], "already_applied": data.already_applied},
            dedupe_key=operation_key(state, stage),
        )
        return {
            "changed_files": list(data.changed_files),
            "current_stage": stage,
            "pending_approval": None,
            "error_type": None,
            "error_message": None,
        }

    async def run_tests(self, state: RepoPilotState) -> dict[str, object]:
        stage = "run_tests"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        if await self._cancelled(state):
            return self._cancel_update(stage)
        result = await asyncio.to_thread(
            self.deps.test_runner.run,
            Path(state["worktree_path"] or ""),
            state["test_command"],
        )
        if await self._cancelled(state):
            return self._cancel_update(stage)
        if not result.ok:
            if result.error_code == "approval_required":
                return {
                    "current_stage": stage,
                    "pending_approval": {"action": "test_command"},
                }
            return self._failure(stage, result.error_code or "tool_error", result.message)
        data = result.data
        assert data is not None
        output = _bounded(data.stdout + data.stderr)
        passed = data.exit_code == 0
        await self.deps.event_repository.append(
            state["task_id"], EventType.TEST_COMPLETED, stage=stage,
            payload={"exit_code": data.exit_code, "passed": passed},
            dedupe_key=operation_key(state, stage),
        )
        return {
            "test_exit_code": data.exit_code,
            "test_output": output,
            "tests_passed": passed,
            "current_stage": stage,
            "error_type": None,
            "error_message": None,
        }

    async def inspect_failure(self, state: RepoPilotState) -> dict[str, object]:
        stage = "inspect_failure"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        try:
            analysis = await self.deps.repo_agent.analyze_failure(
                task_id=state["task_id"], profile=self.deps.profile,
                worktree_root=Path(state["worktree_path"] or ""),
                plan=ChangePlan.model_validate(state["change_plan"]),
                diff=state["patch_text"] or "", test_summary=_bounded(state["test_output"], 20_000),
            )
        except Exception as exc:
            return self._failure(stage, getattr(exc, "error_code", "model_transport_error"), str(exc))
        await self._model_event(state, stage)
        return {"failure_analysis": analysis.model_dump(mode="json"), "current_stage": stage}

    async def review_change(self, state: RepoPilotState) -> dict[str, object]:
        stage = "review_change"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        diff_result = await asyncio.to_thread(git_diff, Path(state["worktree_path"] or ""))
        if not diff_result.ok or diff_result.data is None:
            return self._failure(stage, "tool_error", diff_result.message)
        try:
            decision = await self.deps.repo_agent.review(
                task_id=state["task_id"], profile=self.deps.profile,
                worktree_root=Path(state["worktree_path"] or ""),
                plan=ChangePlan.model_validate(state["change_plan"]),
                diff=_bounded(diff_result.data.patch),
                test_summary=_bounded(state["test_output"], 20_000),
            )
        except Exception as exc:
            return self._failure(stage, getattr(exc, "error_code", "model_transport_error"), str(exc))
        await self._model_event(state, stage)
        return {"review_decision": decision.model_dump(mode="json"), "current_stage": stage}

    async def schedule_retry(self, state: RepoPilotState) -> dict[str, object]:
        stage = "schedule_retry"
        if await self._start(state, stage):
            return self._cancel_update(stage)
        retry = state["retry_count"] + 1
        await self.deps.task_repository.update_execution(state["task_id"], retry_count=retry)
        await self.deps.event_repository.append(
            state["task_id"], EventType.RETRY_SCHEDULED, stage=stage,
            payload={"retry_count": retry}, dedupe_key=operation_key(state, stage),
        )
        return {"retry_count": retry, "current_stage": stage, "error_type": None, "error_message": None}

    async def awaiting_approval(self, state: RepoPilotState) -> dict[str, object]:
        stage = "awaiting_approval"
        payload = dict(state["pending_approval"] or {"action": "unknown"})
        await self.deps.task_repository.transition(
            state["task_id"], TaskStatus.AWAITING_APPROVAL,
            stage=stage, event_type=EventType.APPROVAL_REQUIRED,
            payload=payload, dedupe_key=operation_key(state, stage),
        )
        interrupt(payload)
        return {"current_stage": stage}

    async def success_report(self, state: RepoPilotState) -> dict[str, object]:
        await self.deps.task_repository.transition(
            state["task_id"], TaskStatus.SUCCEEDED, stage="succeeded",
            event_type=EventType.TASK_SUCCEEDED,
            payload={"changed_files": state["changed_files"][:50], "retry_count": state["retry_count"]},
            dedupe_key=f"{state['task_id']}:succeeded",
        )
        return {"current_stage": "succeeded"}

    async def failed_report(self, state: RepoPilotState) -> dict[str, object]:
        error_type = state["error_type"] or (
            "retry_exhausted" if state["retry_count"] >= state["max_retries"] else "test_failed"
        )
        message = state["error_message"] or "The task could not produce an approved passing change."
        await self.deps.task_repository.transition(
            state["task_id"], TaskStatus.FAILED, stage="failed",
            event_type=EventType.TASK_FAILED,
            payload={"error_type": error_type}, dedupe_key=f"{state['task_id']}:failed",
            error_type=error_type, error_message=message,
        )
        return {"current_stage": "failed", "error_type": error_type, "error_message": message}

    async def cancelled_report(self, state: RepoPilotState) -> dict[str, object]:
        await self.deps.task_repository.transition(
            state["task_id"], TaskStatus.CANCELLED, stage="cancelled",
            event_type=EventType.TASK_CANCELLED, payload={"status": "cancelled"},
            dedupe_key=f"{state['task_id']}:cancelled", error_type="cancelled",
            error_message="The task was cancelled.",
        )
        return {"current_stage": "cancelled", "cancel_requested": True, "error_type": "cancelled"}

    async def _model_event(self, state: RepoPilotState, stage: str) -> None:
        await self.deps.event_repository.append(
            state["task_id"], EventType.MODEL_COMPLETED, stage=stage,
            payload={"stage": stage}, dedupe_key=operation_key(state, stage),
        )
