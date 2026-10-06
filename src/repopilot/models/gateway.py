"""OpenAI-compatible transport and bounded provider-neutral model gateway."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Protocol

import openai

from repopilot.domain.tasks import ModelCallRecord
from repopilot.models.profiles import (
    ModelProvider,
    ResolvedModelProfile,
)
from repopilot.models.types import (
    ChatMessage,
    ModelToolCall,
    ModelTransport,
    ModelTurn,
    ModelUsage,
    ToolDefinition,
)


MAX_RESPONSE_CHARS = 200_000
MAX_REPAIR_FRAGMENT_CHARS = 20_000


class ModelCallSink(Protocol):
    async def add(self, record: ModelCallRecord) -> None: ...


class ModelGatewayError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class OpenAIChatTransport:
    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] = openai.AsyncOpenAI,
    ) -> None:
        self._client_factory = client_factory

    async def complete(
        self,
        *,
        stage: str,
        profile: ResolvedModelProfile,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
    ) -> ModelTurn:
        del stage
        if profile.api_key is None or profile.base_url is None:
            raise ValueError("A remote model profile must include a key and base URL.")
        client = self._client_factory(
            api_key=profile.api_key.get_secret_value(),
            base_url=profile.base_url,
        )
        request: dict[str, object] = {
            "model": profile.model,
            "messages": [_message_payload(message) for message in messages],
        }
        if tools:
            request["tools"] = [_tool_payload(tool) for tool in tools]
            request["parallel_tool_calls"] = False
        try:
            response = await client.chat.completions.create(**request)
        finally:
            await client.close()
        if not response.choices:
            raise ValueError("The model response contained no choices.")
        message = response.choices[0].message
        tool_calls: list[ModelToolCall] = []
        for tool_call in message.tool_calls or ():
            arguments = json.loads(tool_call.function.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool-call arguments must be a JSON object.")
            tool_calls.append(
                ModelToolCall(
                    id=str(tool_call.id),
                    name=str(tool_call.function.name),
                    arguments=arguments,
                )
            )
        usage = response.usage
        normalized_usage = ModelUsage(
            input_tokens=0 if usage is None else int(usage.prompt_tokens or 0),
            output_tokens=0 if usage is None else int(usage.completion_tokens or 0),
            total_tokens=0 if usage is None else int(usage.total_tokens or 0),
        )
        return ModelTurn(
            content=None if message.content is None else str(message.content),
            tool_calls=tuple(tool_calls),
            usage=normalized_usage,
        )


def _message_payload(message: ChatMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role, "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        dict(call.arguments),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _tool_payload(tool: ToolDefinition) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }


class ModelGateway:
    def __init__(
        self,
        *,
        transports: Mapping[ModelProvider, ModelTransport],
        model_call_sink: ModelCallSink,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1 or max_attempts > 3:
            raise ValueError("max_attempts must be between one and three.")
        self._transports = dict(transports)
        self._model_call_sink = model_call_sink
        self._sleeper = sleeper
        self._max_attempts = max_attempts

    async def complete(
        self,
        *,
        task_id: str,
        stage: str,
        profile: ResolvedModelProfile,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        transport = self._transports.get(profile.provider)
        if transport is None:
            raise ModelGatewayError(
                "model_transport_unavailable",
                "No transport is configured for the requested model provider.",
            )
        started = perf_counter()
        attempts = 0
        last_error_code: str | None = None
        while attempts < self._max_attempts:
            attempts += 1
            try:
                turn = await transport.complete(
                    stage=stage,
                    profile=profile,
                    messages=messages,
                    tools=tools,
                )
                bounded = ModelTurn(
                    content=(
                        None
                        if turn.content is None
                        else turn.content[:MAX_RESPONSE_CHARS]
                    ),
                    tool_calls=turn.tool_calls,
                    usage=turn.usage,
                )
                await self._record(
                    task_id=task_id,
                    stage=stage,
                    profile=profile,
                    usage=bounded.usage,
                    duration_ms=_elapsed_ms(started),
                    attempts=attempts,
                    outcome="succeeded",
                    error_type=None,
                )
                return bounded
            except Exception as exc:
                error_code, transient = _classify_provider_error(exc)
                last_error_code = error_code
                if transient and attempts < self._max_attempts:
                    await self._sleeper(0.25 * (2 ** (attempts - 1)))
                    continue
                await self._record(
                    task_id=task_id,
                    stage=stage,
                    profile=profile,
                    usage=ModelUsage(0, 0, 0),
                    duration_ms=_elapsed_ms(started),
                    attempts=attempts,
                    outcome="failed",
                    error_type=error_code,
                )
                raise ModelGatewayError(
                    error_code,
                    _safe_error_message(error_code),
                ) from None
        raise ModelGatewayError(
            last_error_code or "model_transport_error",
            "The model request did not complete.",
        )

    async def repair_json(
        self,
        *,
        task_id: str,
        stage: str,
        profile: ResolvedModelProfile,
        schema: Mapping[str, object],
        invalid_output: str,
        validation_error: str,
    ) -> ModelTurn:
        schema_json = json.dumps(
            dict(schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )[:MAX_REPAIR_FRAGMENT_CHARS]
        user_content = (
            "Schema:\n"
            + schema_json
            + "\nValidation error:\n"
            + validation_error[:MAX_REPAIR_FRAGMENT_CHARS]
            + "\nInvalid output:\n"
            + invalid_output[:MAX_REPAIR_FRAGMENT_CHARS]
        )
        return await self.complete(
            task_id=task_id,
            stage=f"{stage}.format_repair",
            profile=profile,
            messages=(
                ChatMessage(
                    role="system",
                    content="Return only corrected JSON that matches the supplied schema.",
                ),
                ChatMessage(role="user", content=user_content),
            ),
        )

    async def _record(
        self,
        *,
        task_id: str,
        stage: str,
        profile: ResolvedModelProfile,
        usage: ModelUsage,
        duration_ms: int,
        attempts: int,
        outcome: str,
        error_type: str | None,
    ) -> None:
        await self._model_call_sink.add(
            ModelCallRecord(
                id=None,
                task_id=task_id,
                stage=stage,
                provider=profile.provider.value,
                model=profile.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                duration_ms=duration_ms,
                attempt_count=attempts,
                outcome=outcome,
                error_type=error_type,
                created_at=datetime.now(timezone.utc),
            )
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1_000))


def _classify_provider_error(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, openai.APITimeoutError):
        return "model_timeout", True
    if isinstance(exc, openai.APIConnectionError):
        return "model_connection", True
    if isinstance(exc, openai.RateLimitError):
        return "model_rate_limited", True
    if isinstance(exc, openai.AuthenticationError):
        return "model_authentication", False
    if isinstance(exc, openai.BadRequestError):
        return "model_request_invalid", False
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return "model_provider_error", True
        return "model_request_invalid", False
    return "model_transport_error", False


def _safe_error_message(error_code: str) -> str:
    return {
        "model_timeout": "The model request timed out.",
        "model_connection": "The model provider could not be reached.",
        "model_rate_limited": "The model provider rate-limited the request.",
        "model_provider_error": "The model provider returned a temporary error.",
        "model_authentication": "The model provider rejected its configured credential.",
        "model_request_invalid": "The model provider rejected the request.",
        "model_transport_error": "The model response could not be processed safely.",
    }.get(error_code, "The model request failed.")
