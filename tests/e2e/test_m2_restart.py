from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from repopilot.agent.repo_agent import RepoAgent
from repopilot.agent.toolbox import ReadOnlyToolbox
from repopilot.api.app import create_app
from repopilot.api.dependencies import AppOverrides, AppSettings
from repopilot.domain.tasks import TaskInput
from repopilot.graph.builder import build_graph, graph_config
from repopilot.graph.nodes import GraphDependencies
from repopilot.graph.state import initial_state
from repopilot.models.gateway import ModelGateway
from repopilot.models.profiles import ModelProfileRegistry, ModelProvider
from repopilot.persistence.database import SqliteDatabase
from repopilot.persistence.repositories import (
    EventRepository,
    ModelCallRepository,
    TaskRepository,
)
from repopilot.services.task_service import TaskService
from repopilot.tools.patch import apply_patch
from repopilot.tools.tests import DockerTestRunner
from repopilot.workspace.worktree import WorktreeManager, capture_repo_snapshot
from tests.helpers.m2 import (
    REPAIR_PATCH,
    copy_calculator_repo,
    review_turn,
    scripted_transport,
    wait_for_status,
)
from repopilot.models.fake import ScriptedFakeTransport


pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.asyncio
async def test_restart_reconciles_written_patch_and_resumes_same_checkpoint(
    tmp_path: Path,
) -> None:
    repo = copy_calculator_repo(tmp_path / "original")
    original = capture_repo_snapshot(repo).data
    settings = AppSettings(
        data_dir=tmp_path / "data",
        poll_interval=0.01,
        environ={},
        allowed_repo_roots=(tmp_path,),
    )
    settings.data_dir.mkdir(parents=True)
    database = SqliteDatabase(settings.database_path)
    await database.initialize()
    tasks = TaskRepository(database)
    events = EventRepository(database)
    calls = ModelCallRepository(database)
    profiles = ModelProfileRegistry.from_env({})
    service = TaskService(tasks, profiles, (tmp_path,))
    task = await service.create_task(
        TaskInput(repo, "Raise ValueError for zero", "pytest -q", "fake", 1)
    )
    claimed = await tasks.claim_next()
    assert claimed is not None
    first_transport = scripted_transport([REPAIR_PATCH], include_review=False)
    first_gateway = ModelGateway(
        transports={ModelProvider.FAKE: first_transport}, model_call_sink=calls
    )
    wrote_patch = False

    def crash_after_write(root: Path, patch_text: str, **kwargs):
        nonlocal wrote_patch
        result = apply_patch(root, patch_text, **kwargs)
        assert result.ok is True
        wrote_patch = True
        raise RuntimeError("injected process crash after patch write")

    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        await saver.setup()
        graph = build_graph(
            GraphDependencies(
                task_repository=tasks,
                event_repository=events,
                profile=profiles.resolve("fake"),
                repo_agent=RepoAgent(first_gateway, ReadOnlyToolbox()),
                worktree_manager=WorktreeManager(settings.worktree_data_dir),
                test_runner=DockerTestRunner(),
                patcher=crash_after_write,
            ),
            saver,
        )
        with pytest.raises(RuntimeError, match="injected process crash"):
            await graph.ainvoke(
                initial_state(
                    task_id=task.id,
                    thread_id=task.thread_id,
                    repo_path=str(repo),
                    user_request=task.user_request,
                    test_command=task.test_command,
                    max_retries=task.max_retries,
                ),
                graph_config(task.thread_id),
            )

    interrupted = await tasks.get(task.id)
    assert interrupted is not None
    assert interrupted.status.value == "running"
    assert interrupted.thread_id == task.thread_id
    assert interrupted.worktree_path is not None
    assert wrote_patch is True
    assert "raise ValueError" in (
        interrupted.worktree_path / "calculator.py"
    ).read_text(encoding="utf-8")

    second_transport = ScriptedFakeTransport({"review": [review_turn()]})
    app = create_app(
        settings,
        overrides=AppOverrides(transports={ModelProvider.FAKE: second_transport}),
    )
    with TestClient(app) as client:
        completed = wait_for_status(client, task.id, {"succeeded", "failed"}, timeout=20)
        persisted_events = client.portal.call(
            app.state.runtime.event_repository.list_after, task.id, 0
        )

    assert completed["status"] == "succeeded"
    assert completed["thread_id"] == task.thread_id
    assert [request.stage for request in first_transport.requests] == ["planning", "patching"]
    assert [request.stage for request in second_transport.requests] == ["review"]
    assert sum(
        event.event_type.value == "patch_applied" for event in persisted_events
    ) == 1
    assert capture_repo_snapshot(repo).data == original
