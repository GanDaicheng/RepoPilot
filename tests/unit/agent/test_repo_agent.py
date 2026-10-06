from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from repopilot.agent.repo_agent import RepoAgent, RepoAgentError
from repopilot.agent.toolbox import ReadOnlyToolbox
from repopilot.domain.tasks import ModelCallRecord
from repopilot.models.fake import ScriptedFakeTransport
from repopilot.models.gateway import ModelGateway
from repopilot.models.profiles import ModelProvider, ResolvedModelProfile
from repopilot.models.types import ModelToolCall, ModelTurn, ModelUsage


class MemorySink:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def add(self, record: ModelCallRecord) -> None:
        self.records.append(record)


def model_turn(
    content: str | None,
    *,
    tool_calls: tuple[ModelToolCall, ...] = (),
) -> ModelTurn:
    return ModelTurn(
        content=content,
        tool_calls=tool_calls,
        usage=ModelUsage(input_tokens=2, output_tokens=3, total_tokens=5),
    )


def fake_profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        name="fake",
        provider=ModelProvider.FAKE,
        model="scripted",
        base_url=None,
        api_key=None,
    )


def remote_profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        name="deepseek",
        provider=ModelProvider.DEEPSEEK,
        model="coder-model",
        base_url="https://models.example.test/v1",
        api_key=SecretStr("never-include-this-credential"),
    )


def plan_json() -> str:
    return json.dumps(
        {
            "goal": "Reject division by zero",
            "relevant_files": ["app.py"],
            "steps": ["Add a guard", "Run tests"],
            "risks": [],
            "suggested_tests": ["pytest -q"],
        }
    )


def make_agent(
    scripts: dict[str, list[ModelTurn | BaseException]],
    *,
    provider: ModelProvider = ModelProvider.FAKE,
) -> tuple[RepoAgent, ScriptedFakeTransport, MemorySink]:
    sink = MemorySink()
    transport = ScriptedFakeTransport(scripts)
    gateway = ModelGateway(transports={provider: transport}, model_call_sink=sink)
    return RepoAgent(gateway, ReadOnlyToolbox()), transport, sink


@pytest.mark.asyncio
async def test_plan_accepts_direct_structured_output(tmp_path: Path) -> None:
    agent, _, _ = make_agent({"planning": [model_turn(plan_json())]})
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    plan = await agent.plan(
        task_id="task-1",
        profile=fake_profile(),
        worktree_root=tmp_path,
        user_request="Reject division by zero",
        repo_summary="One Python file",
    )

    assert plan.goal == "Reject division by zero"
    assert plan.relevant_files == ("app.py",)


@pytest.mark.asyncio
async def test_tool_call_result_keeps_provider_call_id_then_reaches_final_output(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    agent, transport, _ = make_agent(
        {
            "planning": [
                model_turn(
                    None,
                    tool_calls=(
                        ModelToolCall(
                            id="call-read-1",
                            name="read_file",
                            arguments={"path": "app.py"},
                        ),
                    ),
                ),
                model_turn(plan_json()),
            ]
        }
    )

    plan = await agent.plan(
        task_id="task-1",
        profile=fake_profile(),
        worktree_root=tmp_path,
        user_request="Change the value",
        repo_summary="One file",
    )

    assert plan.relevant_files == ("app.py",)
    second_request = transport.requests[1]
    assert second_request.messages[-1].role == "tool"
    assert second_request.messages[-1].tool_call_id == "call-read-1"
    assert "value = 1" in (second_request.messages[-1].content or "")


@pytest.mark.asyncio
async def test_unknown_tool_is_denied_and_model_can_recover(tmp_path: Path) -> None:
    agent, transport, _ = make_agent(
        {
            "planning": [
                model_turn(
                    None,
                    tool_calls=(
                        ModelToolCall(id="call-shell", name="shell", arguments={}),
                    ),
                ),
                model_turn(plan_json()),
            ]
        }
    )

    await agent.plan(
        task_id="task-1",
        profile=fake_profile(),
        worktree_root=tmp_path,
        user_request="Plan safely",
        repo_summary="Empty repo",
    )

    result_payload = json.loads(transport.requests[1].messages[-1].content or "{}")
    assert result_payload["ok"] is False
    assert result_payload["error_code"] == "tool_not_allowed"


@pytest.mark.asyncio
async def test_repeated_tool_requests_stop_after_four_model_rounds(
    tmp_path: Path,
) -> None:
    tool_turn = model_turn(
        None,
        tool_calls=(
            ModelToolCall(id="call-list", name="list_files", arguments={}),
        ),
    )
    agent, transport, sink = make_agent({"planning": [tool_turn] * 5})

    with pytest.raises(RepoAgentError) as caught:
        await agent.plan(
            task_id="task-1",
            profile=fake_profile(),
            worktree_root=tmp_path,
            user_request="Never finish",
            repo_summary="Empty repo",
        )

    assert caught.value.error_code == "model_output_invalid"
    assert len(transport.requests) == 4
    assert len(sink.records) == 4
    assert transport.remaining("planning") == 1


@pytest.mark.asyncio
async def test_invalid_json_is_repaired_exactly_once(tmp_path: Path) -> None:
    agent, transport, sink = make_agent(
        {
            "planning": [model_turn("not json")],
            "planning.format_repair": [model_turn(plan_json())],
        }
    )

    plan = await agent.plan(
        task_id="task-1",
        profile=fake_profile(),
        worktree_root=tmp_path,
        user_request="Plan",
        repo_summary="Repo",
    )

    assert plan.goal == "Reject division by zero"
    assert [request.stage for request in transport.requests] == [
        "planning",
        "planning.format_repair",
    ]
    assert len(sink.records) == 2


@pytest.mark.asyncio
async def test_second_invalid_json_fails_with_stable_error(tmp_path: Path) -> None:
    agent, transport, _ = make_agent(
        {
            "planning": [model_turn("not json")],
            "planning.format_repair": [model_turn("still not json")],
        }
    )

    with pytest.raises(RepoAgentError) as caught:
        await agent.plan(
            task_id="task-1",
            profile=fake_profile(),
            worktree_root=tmp_path,
            user_request="Plan",
            repo_summary="Repo",
        )

    assert caught.value.error_code == "model_output_invalid"
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_stage_methods_use_distinct_prompts_without_profile_credentials(
    tmp_path: Path,
) -> None:
    scripts = {
        "planning": [model_turn(plan_json())],
        "patching": [
            model_turn(
                json.dumps(
                    {
                        "patch_text": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                        "summary": "Update value",
                        "expected_files": ["app.py"],
                    }
                )
            )
        ],
        "failure_analysis": [
            model_turn(
                json.dumps(
                    {
                        "root_cause": "Assertion failed",
                        "evidence": ["expected new"],
                        "fixable": True,
                        "strategy": "Correct the value",
                        "suggested_files": ["app.py"],
                    }
                )
            )
        ],
        "review": [
            model_turn(
                json.dumps(
                    {
                        "approved": True,
                        "critical_findings": [],
                        "warnings": [],
                        "conclusion": "Safe change",
                    }
                )
            )
        ],
    }
    agent, transport, _ = make_agent(scripts, provider=ModelProvider.DEEPSEEK)
    profile = remote_profile()
    plan = await agent.plan(
        task_id="task-1",
        profile=profile,
        worktree_root=tmp_path,
        user_request="Update value",
        repo_summary="One file",
    )
    proposal = await agent.propose_patch(
        task_id="task-1",
        profile=profile,
        worktree_root=tmp_path,
        plan=plan,
        repo_summary="One file",
    )
    analysis = await agent.analyze_failure(
        task_id="task-1",
        profile=profile,
        worktree_root=tmp_path,
        plan=plan,
        diff=proposal.patch_text,
        test_summary="one failure",
    )
    review = await agent.review(
        task_id="task-1",
        profile=profile,
        worktree_root=tmp_path,
        plan=plan,
        diff=proposal.patch_text,
        test_summary="all passed",
    )

    assert analysis.fixable is True
    assert review.approved is True
    assert [request.stage for request in transport.requests] == [
        "planning",
        "patching",
        "failure_analysis",
        "review",
    ]
    prompts = "\n".join(
        message.content or ""
        for request in transport.requests
        for message in request.messages
    )
    assert "never-include-this-credential" not in prompts
    assert "DEEPSEEK_API_KEY" not in prompts
    assert "environment" not in prompts.lower()
