"""Provider-neutral chat, tool-call, and usage value objects."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Protocol, Sequence

from repopilot.models.profiles import ResolvedModelProfile


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    id: str
    name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    tool_call_id: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ModelTurn:
    content: str | None
    tool_calls: tuple[ModelToolCall, ...]
    usage: ModelUsage


class ModelTransport(Protocol):
    async def complete(
        self,
        *,
        stage: str,
        profile: ResolvedModelProfile,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
    ) -> ModelTurn: ...
