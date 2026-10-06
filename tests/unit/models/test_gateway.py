from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr

from repopilot.domain.tasks import ModelCallRecord
from repopilot.models.fake import ScriptedFakeTransport
from repopilot.models.gateway import (
    MAX_RESPONSE_CHARS,
    ModelGateway,
    ModelGatewayError,
    OpenAIChatTransport,
)
from repopilot.models.profiles import ModelProvider, ResolvedModelProfile
from repopilot.models.types import (
    ChatMessage,
    ModelToolCall,
    ModelTurn,
    ModelUsage,
    ToolDefinition,
)


class CollectingSink:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def add(self, record: ModelCallRecord) -> None:
        self.records.append(record)


def profile(provider: ModelProvider = ModelProvider.FAKE) -> ResolvedModelProfile:
    if provider is ModelProvider.FAKE:
        return ResolvedModelProfile(
            name="fake",
            provider=provider,
            model="scripted",
            base_url=None,
            api_key=None,
        )
    return ResolvedModelProfile(
        name=provider.value,
        provider=provider,
        model="coder-model",
        base_url="https://models.example.test/v1",
        api_key=SecretStr("provider-test-value"),
    )


def turn(content: str = '{"answer":"ok"}') -> ModelTurn:
    return ModelTurn(
        content=content,
        tool_calls=(),
        usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
    )


@pytest.mark.asyncio
async def test_scripted_fake_returns_stage_turn_and_records_one_logical_call() -> None:
    sink = CollectingSink()
    transport = ScriptedFakeTransport({"planning": [turn()]})
    gateway = ModelGateway(
        transports={ModelProvider.FAKE: transport},
        model_call_sink=sink,
    )

    result = await gateway.complete(
        task_id="task-1",
        stage="planning",
        profile=profile(),
        messages=(ChatMessage(role="user", content="Plan the change"),),
    )

    assert result == turn()
    assert len(sink.records) == 1
    assert (
        sink.records[0].input_tokens,
        sink.records[0].output_tokens,
        sink.records[0].total_tokens,
        sink.records[0].attempt_count,
        sink.records[0].outcome,
    ) == (3, 5, 8, 1, "succeeded")
    assert transport.remaining("planning") == 0


class FakeCompletions:
    def __init__(self, response: object):
        self.response = response
        self.kwargs: dict[str, object] | None = None

    async def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return self.response


class FakeOpenAIClient:
    def __init__(self, response: object):
        self.chat = SimpleNamespace(completions=FakeCompletions(response))
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_openai_transport_builds_client_and_translates_messages_and_tools() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="read_file",
                                arguments='{"path":"src/app.py"}',
                            ),
                        )
                    ],
                )
            )
        ],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2, total_tokens=9),
    )
    client = FakeOpenAIClient(response)
    factory_args: dict[str, object] = {}

    def factory(**kwargs: object) -> FakeOpenAIClient:
        factory_args.update(kwargs)
        return client

    transport = OpenAIChatTransport(client_factory=factory)
    result = await transport.complete(
        stage="planning",
        profile=profile(ModelProvider.DEEPSEEK),
        messages=(
            ChatMessage(role="system", content="Return JSON"),
            ChatMessage(role="user", content="Inspect the file"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=(
                    ModelToolCall(
                        id="prior-call",
                        name="search_code",
                        arguments={"query": "divide"},
                    ),
                ),
            ),
            ChatMessage(
                role="tool",
                content='{"matches":[]}',
                tool_call_id="prior-call",
            ),
        ),
        tools=(
            ToolDefinition(
                name="read_file",
                description="Read one file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
        ),
    )

    assert factory_args == {
        "api_key": "provider-test-value",
        "base_url": "https://models.example.test/v1",
    }
    assert client.closed is True
    assert client.chat.completions.kwargs == {
        "model": "coder-model",
        "messages": [
            {"role": "system", "content": "Return JSON"},
            {"role": "user", "content": "Inspect the file"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "prior-call",
                        "type": "function",
                        "function": {
                            "name": "search_code",
                            "arguments": '{"query":"divide"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": '{"matches":[]}',
                "tool_call_id": "prior-call",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read one file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
        "parallel_tool_calls": False,
    }
    assert result.tool_calls == (
        ModelToolCall(
            id="call-1",
            name="read_file",
            arguments={"path": "src/app.py"},
        ),
    )
    assert result.usage == ModelUsage(7, 2, 9)


def transient_error(kind: str) -> BaseException:
    request = httpx2.Request("POST", "https://models.example.test/v1/chat/completions")
    if kind == "timeout":
        return openai.APITimeoutError(request)
    if kind == "connection":
        return openai.APIConnectionError(request=request)
    status = 429 if kind == "rate_limit" else 503
    response = httpx2.Response(status, request=request)
    if kind == "rate_limit":
        return openai.RateLimitError("limited", response=response, body=None)
    return openai.InternalServerError("unavailable", response=response, body=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["timeout", "connection", "rate_limit", "server"])
async def test_transient_provider_failures_retry_with_bounded_backoff(kind: str) -> None:
    delays: list[float] = []
    sink = CollectingSink()
    transport = ScriptedFakeTransport(
        {"planning": [transient_error(kind), transient_error(kind), turn()]}
    )

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    gateway = ModelGateway(
        transports={ModelProvider.FAKE: transport},
        model_call_sink=sink,
        sleeper=sleeper,
    )

    result = await gateway.complete(
        task_id="task-1",
        stage="planning",
        profile=profile(),
        messages=(ChatMessage(role="user", content="Plan"),),
    )

    assert result.content == '{"answer":"ok"}'
    assert delays == [0.25, 0.5]
    assert len(sink.records) == 1
    assert sink.records[0].attempt_count == 3
    assert sink.records[0].outcome == "succeeded"


def permanent_error(kind: str) -> BaseException:
    request = httpx2.Request("POST", "https://models.example.test/v1/chat/completions")
    response = httpx2.Response(401 if kind == "auth" else 400, request=request)
    if kind == "auth":
        return openai.AuthenticationError(
            "credential details must stay hidden", response=response, body=None
        )
    return openai.BadRequestError(
        "request details must stay hidden", response=response, body=None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "error_code"),
    [("auth", "model_authentication"), ("request", "model_request_invalid")],
)
async def test_permanent_provider_failures_do_not_retry_or_leak_details(
    kind: str,
    error_code: str,
) -> None:
    sink = CollectingSink()
    transport = ScriptedFakeTransport({"planning": [permanent_error(kind), turn()]})
    gateway = ModelGateway(
        transports={ModelProvider.FAKE: transport},
        model_call_sink=sink,
    )

    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete(
            task_id="task-1",
            stage="planning",
            profile=profile(),
            messages=(ChatMessage(role="user", content="Plan"),),
        )

    assert caught.value.error_code == error_code
    assert "details" not in str(caught.value)
    assert transport.remaining("planning") == 1
    assert len(sink.records) == 1
    assert sink.records[0].attempt_count == 1
    assert sink.records[0].outcome == "failed"
    assert sink.records[0].error_type == error_code


@pytest.mark.asyncio
async def test_transient_failure_stops_after_three_attempts() -> None:
    sink = CollectingSink()
    transport = ScriptedFakeTransport(
        {
            "planning": [
                transient_error("timeout"),
                transient_error("timeout"),
                transient_error("timeout"),
                turn(),
            ]
        }
    )
    gateway = ModelGateway(
        transports={ModelProvider.FAKE: transport},
        model_call_sink=sink,
        sleeper=_no_sleep,
    )

    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete(
            task_id="task-1",
            stage="planning",
            profile=profile(),
            messages=(ChatMessage(role="user", content="Plan"),),
        )

    assert caught.value.error_code == "model_timeout"
    assert transport.remaining("planning") == 1
    assert sink.records[0].attempt_count == 3


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_gateway_caps_provider_response_text() -> None:
    sink = CollectingSink()
    gateway = ModelGateway(
        transports={
            ModelProvider.FAKE: ScriptedFakeTransport(
                {"planning": [turn("x" * (MAX_RESPONSE_CHARS + 20))]}
            )
        },
        model_call_sink=sink,
    )

    result = await gateway.complete(
        task_id="task-1",
        stage="planning",
        profile=profile(),
        messages=(ChatMessage(role="user", content="Plan"),),
    )

    assert result.content == "x" * MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_gateway_does_not_swallow_async_cancellation() -> None:
    sink = CollectingSink()
    gateway = ModelGateway(
        transports={
            ModelProvider.FAKE: ScriptedFakeTransport(
                {"planning": [asyncio.CancelledError()]}
            )
        },
        model_call_sink=sink,
    )

    with pytest.raises(asyncio.CancelledError):
        await gateway.complete(
            task_id="task-1",
            stage="planning",
            profile=profile(),
            messages=(ChatMessage(role="user", content="Plan"),),
        )

    assert sink.records == []


@pytest.mark.asyncio
async def test_repair_json_is_a_separate_bounded_logical_call() -> None:
    sink = CollectingSink()
    transport = ScriptedFakeTransport({"planning.format_repair": [turn('{"ok":true}')]})
    gateway = ModelGateway(
        transports={ModelProvider.FAKE: transport},
        model_call_sink=sink,
    )

    result = await gateway.repair_json(
        task_id="task-1",
        stage="planning",
        profile=profile(),
        schema={"type": "object", "required": ["ok"]},
        invalid_output="bad" * 100_000,
        validation_error="invalid" * 100_000,
    )

    assert result.content == '{"ok":true}'
    assert [record.stage for record in sink.records] == ["planning.format_repair"]
    request = transport.requests[0]
    assert request.stage == "planning.format_repair"
    assert sum(len(message.content or "") for message in request.messages) < 50_000
    assert set(sink.records[0].__dataclass_fields__) == {
        "id",
        "task_id",
        "stage",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "duration_ms",
        "attempt_count",
        "outcome",
        "error_type",
        "created_at",
    }
