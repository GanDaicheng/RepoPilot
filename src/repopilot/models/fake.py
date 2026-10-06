"""Deterministic scripted model transport for tests and default local runs."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from repopilot.models.profiles import ResolvedModelProfile
from repopilot.models.types import ChatMessage, ModelTurn, ToolDefinition


@dataclass(frozen=True, slots=True)
class FakeRequest:
    stage: str
    profile: ResolvedModelProfile
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDefinition, ...]


class ScriptedFakeTransport:
    def __init__(
        self,
        scripts: Mapping[str, Sequence[ModelTurn | BaseException]],
    ) -> None:
        self._scripts = {stage: deque(items) for stage, items in scripts.items()}
        self.requests: list[FakeRequest] = []

    async def complete(
        self,
        *,
        stage: str,
        profile: ResolvedModelProfile,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
    ) -> ModelTurn:
        self.requests.append(
            FakeRequest(
                stage=stage,
                profile=profile,
                messages=tuple(messages),
                tools=tuple(tools),
            )
        )
        queue = self._scripts.get(stage)
        if queue is None or not queue:
            raise RuntimeError("The Fake Model has no scripted turn for this stage.")
        result = queue.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def remaining(self, stage: str) -> int:
        queue = self._scripts.get(stage)
        return 0 if queue is None else len(queue)
