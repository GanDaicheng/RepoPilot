from __future__ import annotations

import os
from pathlib import Path

import pytest

from repopilot.agent.repo_agent import RepoAgent
from repopilot.agent.toolbox import ReadOnlyToolbox
from repopilot.domain.tasks import ModelCallRecord
from repopilot.models.gateway import ModelGateway, OpenAIChatTransport
from repopilot.models.profiles import ModelProfileRegistry, ModelProvider


class Sink:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def add(self, record: ModelCallRecord) -> None:
        self.records.append(record)


def require_qwen_configuration() -> None:
    required = ("DASHSCOPE_API_KEY", "QWEN_MODEL", "QWEN_BASE_URL")
    missing = [name for name in required if not os.environ.get(name)]
    if os.environ.get("RUN_QWEN_SMOKE") != "1" or missing:
        details = ["RUN_QWEN_SMOKE=1"]
        details.extend(missing)
        pytest.skip("Qwen smoke requires explicit opt-in/config: " + ", ".join(details))


@pytest.mark.qwen_smoke
@pytest.mark.asyncio
async def test_qwen_returns_valid_tiny_change_plan(tmp_path: Path) -> None:
    require_qwen_configuration()
    profile = ModelProfileRegistry.from_env(os.environ).resolve("qwen")
    sink = Sink()
    agent = RepoAgent(
        ModelGateway(
            transports={ModelProvider.QWEN: OpenAIChatTransport()},
            model_call_sink=sink,
        ),
        ReadOnlyToolbox(),
    )

    plan = await agent.plan(
        task_id="qwen-smoke",
        profile=profile,
        worktree_root=tmp_path,
        user_request="Plan adding a one-line greeting to hello.py.",
        repo_summary="The repository is empty.",
    )

    assert plan.goal
    assert plan.steps
    assert sink.records
    assert all(
        record.input_tokens >= 0
        and record.output_tokens >= 0
        and record.total_tokens >= 0
        for record in sink.records
    )
