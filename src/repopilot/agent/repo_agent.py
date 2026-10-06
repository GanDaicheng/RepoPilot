"""Bounded model/tool loop for repository planning, patching, analysis, and review."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from repopilot.agent.schemas import (
    ChangePlan,
    FailureAnalysis,
    PatchProposal,
    ReviewDecision,
)
from repopilot.agent.toolbox import ReadOnlyToolbox
from repopilot.models.gateway import ModelGateway
from repopilot.models.profiles import ResolvedModelProfile
from repopilot.models.types import ChatMessage


OutputT = TypeVar("OutputT", bound=BaseModel)
MAX_MODEL_ROUNDS = 4


class RepoAgentError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class RepoAgent:
    def __init__(self, gateway: ModelGateway, toolbox: ReadOnlyToolbox) -> None:
        self._gateway = gateway
        self._toolbox = toolbox

    async def plan(
        self,
        *,
        task_id: str,
        profile: ResolvedModelProfile,
        worktree_root: Path,
        user_request: str,
        repo_summary: str,
    ) -> ChangePlan:
        return await self._run_stage(
            task_id=task_id,
            stage="planning",
            profile=profile,
            worktree_root=worktree_root,
            schema=ChangePlan,
            objective="Create a safe implementation plan for the requested repository change.",
            context={"request": user_request, "repository_summary": repo_summary},
        )

    async def propose_patch(
        self,
        *,
        task_id: str,
        profile: ResolvedModelProfile,
        worktree_root: Path,
        plan: ChangePlan,
        repo_summary: str,
    ) -> PatchProposal:
        return await self._run_stage(
            task_id=task_id,
            stage="patching",
            profile=profile,
            worktree_root=worktree_root,
            schema=PatchProposal,
            objective="Propose one bounded unified diff that implements the supplied plan.",
            context={"plan": plan.model_dump(mode="json"), "repository_summary": repo_summary},
        )

    async def analyze_failure(
        self,
        *,
        task_id: str,
        profile: ResolvedModelProfile,
        worktree_root: Path,
        plan: ChangePlan,
        diff: str,
        test_summary: str,
    ) -> FailureAnalysis:
        return await self._run_stage(
            task_id=task_id,
            stage="failure_analysis",
            profile=profile,
            worktree_root=worktree_root,
            schema=FailureAnalysis,
            objective="Explain the test failure and decide whether a focused repair is possible.",
            context={"plan": plan.model_dump(mode="json"), "diff": diff, "test_summary": test_summary},
        )

    async def review(
        self,
        *,
        task_id: str,
        profile: ResolvedModelProfile,
        worktree_root: Path,
        plan: ChangePlan,
        diff: str,
        test_summary: str,
    ) -> ReviewDecision:
        return await self._run_stage(
            task_id=task_id,
            stage="review",
            profile=profile,
            worktree_root=worktree_root,
            schema=ReviewDecision,
            objective="Review the diff for correctness, safety, scope, and test coverage.",
            context={"plan": plan.model_dump(mode="json"), "diff": diff, "test_summary": test_summary},
        )

    async def _run_stage(
        self,
        *,
        task_id: str,
        stage: str,
        profile: ResolvedModelProfile,
        worktree_root: Path,
        schema: type[OutputT],
        objective: str,
        context: Mapping[str, object],
    ) -> OutputT:
        messages: list[ChatMessage] = [
            ChatMessage(
                role="system",
                content=(
                    "You are a repository engineering agent. Use only the supplied read-only "
                    "tools. Return only JSON matching the requested schema. " + objective
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(
                    {"context": dict(context), "output_schema": schema.model_json_schema()},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        ]
        final_content: str | None = None
        for round_number in range(1, MAX_MODEL_ROUNDS + 1):
            turn = await self._gateway.complete(
                task_id=task_id,
                stage=stage,
                profile=profile,
                messages=tuple(messages),
                tools=self._toolbox.definitions,
            )
            if turn.tool_calls:
                if round_number == MAX_MODEL_ROUNDS:
                    raise RepoAgentError(
                        "model_output_invalid",
                        "The model did not produce a final structured output within the round limit.",
                    )
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=turn.content,
                        tool_calls=turn.tool_calls,
                    )
                )
                for call in turn.tool_calls:
                    result = await asyncio.to_thread(self._toolbox.execute, worktree_root, call)
                    messages.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            content=json.dumps(
                                {
                                    "ok": result.ok,
                                    "data": result.data,
                                    "error_code": result.error_code,
                                    "message": result.message,
                                    "metadata": dict(result.metadata),
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    )
                continue
            final_content = turn.content
            break

        if final_content is None:
            raise RepoAgentError("model_output_invalid", "The model returned no structured output.")
        try:
            return schema.model_validate_json(final_content)
        except ValidationError as error:
            repaired = await self._gateway.repair_json(
                task_id=task_id,
                stage=stage,
                profile=profile,
                schema=schema.model_json_schema(),
                invalid_output=final_content,
                validation_error=str(error),
            )
            if repaired.content is None or repaired.tool_calls:
                raise RepoAgentError("model_output_invalid", "The repaired model output was invalid.")
            try:
                return schema.model_validate_json(repaired.content)
            except ValidationError:
                raise RepoAgentError(
                    "model_output_invalid",
                    "The model output did not match the required schema.",
                ) from None
