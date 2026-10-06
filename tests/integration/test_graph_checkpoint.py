from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from repopilot.agent.schemas import ChangePlan, PatchProposal, ReviewDecision
from repopilot.domain.results import ToolResult
from repopilot.domain.tasks import EventType
from repopilot.graph.builder import build_graph, graph_config
from repopilot.graph.nodes import GraphDependencies, GraphNodes
from repopilot.graph.state import initial_state
from repopilot.models.profiles import ModelProvider, ResolvedModelProfile
from repopilot.tools.patch import PatchApplyData, apply_patch
from repopilot.tools.tests import TestRunResult as RunResult
from repopilot.workspace.worktree import RepoSnapshot, WorktreeInfo
from tests.helpers.git import init_repo


pytestmark = pytest.mark.integration


class TaskRepo:
    def __init__(self) -> None:
        self.stage = "running"
        self.base_commit: str | None = None

    async def get(self, task_id: str):
        del task_id
        return SimpleNamespace(cancel_requested=False, base_commit=self.base_commit)

    async def update_execution(self, task_id: str, **fields: object):
        del task_id
        self.stage = str(fields.get("current_stage", self.stage))
        return SimpleNamespace(cancel_requested=False, base_commit=self.base_commit)

    async def set_base_commit_once(self, task_id: str, base_commit: str):
        del task_id
        if self.base_commit is None:
            self.base_commit = base_commit
        return SimpleNamespace(cancel_requested=False, base_commit=self.base_commit)

    async def transition(self, task_id, target, **kwargs):
        del task_id
        self.stage = str(kwargs["stage"])
        return SimpleNamespace(cancel_requested=False, status=target)


class DedupeEvents:
    def __init__(self) -> None:
        self.by_key: dict[str, EventType] = {}
        self.patch_event_attempts = 0

    async def append(self, task_id, event_type, *, stage, payload, dedupe_key=None):
        del task_id, stage, payload
        if event_type is EventType.PATCH_APPLIED:
            self.patch_event_attempts += 1
        if dedupe_key is not None:
            self.by_key.setdefault(dedupe_key, event_type)
        return SimpleNamespace()


class Agent:
    async def plan(self, **kwargs):
        del kwargs
        return ChangePlan(
            goal="Change", relevant_files=("app.py",), steps=("Edit",),
            risks=(), suggested_tests=("pytest -q",),
        )

    async def propose_patch(self, **kwargs):
        del kwargs
        return PatchProposal(
            patch_text="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            summary="Change", expected_files=("app.py",),
        )

    async def review(self, **kwargs):
        del kwargs
        return ReviewDecision(
            approved=True, critical_findings=(), warnings=(), conclusion="Good"
        )

    async def analyze_failure(self, **kwargs):
        raise AssertionError(kwargs)


class Runner:
    def run(self, root: Path, command: str):
        del root, command
        return ToolResult.success(RunResult(("pytest",), 0, "passed\n", "", False, 1))


class CrashOnceWorktrees:
    def __init__(self, repo: Path, base: str) -> None:
        self.repo = repo
        self.base = base
        self.calls = 0

    def create(self, repo_path: Path, task_id: str, *, base_commit: str | None = None):
        del repo_path
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("injected crash")
        snapshot = RepoSnapshot(self.base, "main", "", "digest")
        return ToolResult.success(
            WorktreeInfo(task_id, self.repo, base_commit or self.base, f"repopilot/{task_id}", self.repo, snapshot)
        )


def profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        name="fake", provider=ModelProvider.FAKE, model="scripted",
        base_url=None, api_key=None,
    )


@pytest.mark.asyncio
async def test_sqlite_checkpoint_resumes_same_thread_without_repeating_completed_nodes(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo", {"app.py": "value = 1\n"})
    base = __import__("subprocess").run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    capture_calls = 0

    def capture(root: Path):
        nonlocal capture_calls
        del root
        capture_calls += 1
        return ToolResult.success(RepoSnapshot(base, "main", "", "digest"))

    worktrees = CrashOnceWorktrees(repo, base)
    events = DedupeEvents()
    dependencies = GraphDependencies(
        task_repository=TaskRepo(), event_repository=events, profile=profile(),
        repo_agent=Agent(), worktree_manager=worktrees, test_runner=Runner(),
        patcher=lambda root, patch, **kwargs: ToolResult.success(
            PatchApplyData(("app.py",), False, False)
        ),
        base_capture=capture,
    )
    thread_id = "11111111-1111-4111-8111-111111111111"
    config = graph_config(thread_id)
    checkpoint_path = str(tmp_path / "checkpoint.sqlite")
    state = initial_state(
        task_id="task-1", thread_id=thread_id, repo_path=str(repo),
        user_request="Change", test_command="pytest -q", max_retries=1,
    )

    async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as saver:
        with pytest.raises(RuntimeError, match="injected crash"):
            await build_graph(dependencies, saver).ainvoke(state, config)

    async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as saver:
        result = await build_graph(dependencies, saver).ainvoke(None, config)

    assert result["current_stage"] == "succeeded"
    assert capture_calls == 1
    assert worktrees.calls == 2


@pytest.mark.asyncio
async def test_patch_node_replay_is_write_idempotent_and_event_dedupable(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"app.py": "value = 1\n"})
    events = DedupeEvents()
    dependencies = GraphDependencies(
        task_repository=TaskRepo(), event_repository=events, profile=profile(),
        repo_agent=Agent(), worktree_manager=CrashOnceWorktrees(repo, "a" * 40),
        test_runner=Runner(), patcher=apply_patch,
    )
    nodes = GraphNodes(dependencies)
    state = initial_state(
        task_id="task-1", thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(repo), user_request="Change", test_command="pytest -q", max_retries=1,
    )
    state.update(
        worktree_path=str(repo),
        patch_text="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n",
    )

    first = await nodes.apply_patch(state)
    second = await nodes.apply_patch(state)

    assert first["changed_files"] == ["app.py"]
    assert second["changed_files"] == ["app.py"]
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    patch_keys = [key for key, value in events.by_key.items() if value is EventType.PATCH_APPLIED]
    assert patch_keys == ["task-1:apply_patch:0"]
