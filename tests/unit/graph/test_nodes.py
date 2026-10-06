from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from repopilot.agent.schemas import ChangePlan, PatchProposal, ReviewDecision
from repopilot.domain.results import ToolResult
from repopilot.domain.tasks import EventType
from repopilot.graph.nodes import GraphDependencies, GraphNodes
from repopilot.graph.state import initial_state
from repopilot.models.profiles import ModelProvider, ResolvedModelProfile
from repopilot.tools.patch import PatchApplyData
from repopilot.tools.tests import TestRunResult as RunResult
from repopilot.workspace.worktree import RepoSnapshot, WorktreeInfo


@dataclass
class TaskView:
    cancel_requested: bool = False
    base_commit: str | None = None
    status: object | None = None


class TaskRepo:
    def __init__(
        self,
        cancel_sequence: list[bool] | None = None,
        persisted_base_commit: str | None = None,
    ) -> None:
        self.cancel_sequence = list(cancel_sequence or [False] * 20)
        self.updates: list[dict[str, object]] = []
        self.persisted_base_commit = persisted_base_commit

    async def get(self, task_id: str) -> TaskView:
        del task_id
        value = self.cancel_sequence.pop(0) if self.cancel_sequence else False
        return TaskView(value, self.persisted_base_commit)

    async def update_execution(self, task_id: str, **fields: object) -> TaskView:
        del task_id
        self.updates.append(fields)
        return TaskView()

    async def set_base_commit_once(self, task_id: str, base_commit: str) -> TaskView:
        del task_id
        self.updates.append({"base_commit": base_commit})
        if self.persisted_base_commit is None:
            self.persisted_base_commit = base_commit
        return TaskView(base_commit=self.persisted_base_commit)

    async def transition(self, task_id: str, target, **kwargs: object) -> TaskView:
        del task_id, kwargs
        return TaskView(base_commit=self.persisted_base_commit, status=target)


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[EventType, str, dict[str, object], str | None]] = []

    async def append(self, task_id, event_type, *, stage, payload, dedupe_key=None):
        del task_id
        self.items.append((event_type, stage, dict(payload), dedupe_key))
        return SimpleNamespace()


class Worktrees:
    def __init__(self, root: Path, base: str) -> None:
        self.root = root
        self.base = base
        self.calls: list[str | None] = []

    def create(self, repo_path: Path, task_id: str, *, base_commit: str | None = None):
        del repo_path
        self.calls.append(base_commit)
        snapshot = RepoSnapshot(self.base, "main", "", "digest")
        return ToolResult.success(
            WorktreeInfo(task_id, self.root, self.base, f"repopilot/{task_id}", self.root, snapshot)
        )


class Agent:
    async def plan(self, **kwargs):
        assert "task_id" in kwargs and "worktree_root" in kwargs
        return ChangePlan(
            goal="Change value",
            relevant_files=("app.py",),
            steps=("Edit",),
            risks=(),
            suggested_tests=("pytest -q",),
        )

    async def propose_patch(self, **kwargs):
        return PatchProposal(
            patch_text="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            summary="Change value",
            expected_files=("app.py",),
        )

    async def analyze_failure(self, **kwargs):
        raise AssertionError("not used")

    async def review(self, **kwargs):
        return ReviewDecision(
            approved=True,
            critical_findings=(),
            warnings=(),
            conclusion="Good",
        )


class Runner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, root: Path, command: str):
        del root, command
        self.calls += 1
        return ToolResult.success(RunResult(("pytest",), 0, "ok\n", "", False, 1))


def deps(tmp_path: Path, task_repo: TaskRepo | None = None, patcher=None) -> GraphDependencies:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return GraphDependencies(
        task_repository=task_repo or TaskRepo(),
        event_repository=Events(),
        profile=ResolvedModelProfile(
            name="fake",
            provider=ModelProvider.FAKE,
            model="scripted",
            base_url=None,
            api_key=None,
        ),
        repo_agent=Agent(),
        worktree_manager=Worktrees(repo, "a" * 40),
        test_runner=Runner(),
        patcher=patcher
        or (lambda root, patch, **kwargs: ToolResult.success(PatchApplyData(("app.py",), False, False))),
        base_capture=lambda root: ToolResult.success(RepoSnapshot("a" * 40, "main", "", "digest")),
    )


@pytest.mark.asyncio
async def test_base_is_captured_before_exact_worktree_creation(tmp_path: Path) -> None:
    dependencies = deps(tmp_path)
    nodes = GraphNodes(dependencies)
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=2,
    )

    captured = await nodes.capture_base_commit(state)
    created = await nodes.create_worktree({**state, **captured})

    assert captured == {"base_commit": "a" * 40, "current_stage": "capture_base_commit"}
    assert created["worktree_path"] == str(tmp_path / "repo")
    assert dependencies.worktree_manager.calls == ["a" * 40]


@pytest.mark.asyncio
async def test_capture_reuses_persisted_base_after_checkpoint_gap(tmp_path: Path) -> None:
    task_repo = TaskRepo(persisted_base_commit="a" * 40)
    dependencies = deps(tmp_path, task_repo=task_repo)
    dependencies.base_capture = lambda root: (_ for _ in ()).throw(
        AssertionError("HEAD must not be recaptured")
    )
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=1,
    )

    update = await GraphNodes(dependencies).capture_base_commit(state)

    assert update["base_commit"] == "a" * 40


@pytest.mark.asyncio
async def test_patch_and_test_results_are_application_truth_and_outputs_are_capped(tmp_path: Path) -> None:
    dependencies = deps(tmp_path)
    nodes = GraphNodes(dependencies)
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=2,
    )
    state.update(
        worktree_path=str(tmp_path / "repo"),
        patch_text="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
    )

    patched = await nodes.apply_patch(state)
    tested = await nodes.run_tests({**state, **patched})

    assert patched["changed_files"] == ["app.py"]
    assert tested["tests_passed"] is True
    assert tested["test_exit_code"] == 0
    assert len(tested["test_output"].encode("utf-8")) <= 200_000
    emitted = [item[0] for item in dependencies.event_repository.items]
    assert EventType.PATCH_APPLIED in emitted
    assert EventType.TEST_COMPLETED in emitted
    assert set(emitted) <= set(EventType)


@pytest.mark.asyncio
async def test_patch_tool_infrastructure_failure_does_not_consume_code_retry(
    tmp_path: Path,
) -> None:
    nodes = GraphNodes(
        deps(
            tmp_path,
            patcher=lambda root, patch, **kwargs: ToolResult.failure(
                "tool_unavailable", "Git is unavailable."
            ),
        )
    )
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=2,
    )
    state.update(worktree_path=str(tmp_path / "repo"), patch_text="patch")

    update = await nodes.apply_patch(state)

    assert update["error_type"] == "tool_error"


@pytest.mark.asyncio
async def test_exhausted_patch_retry_reports_retry_exhausted(tmp_path: Path) -> None:
    nodes = GraphNodes(deps(tmp_path))
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=1,
    )
    state.update(error_type="patch_apply_failed", retry_count=1)

    update = await nodes.failed_report(state)

    assert update["error_type"] == "retry_exhausted"


@pytest.mark.asyncio
async def test_side_effect_rechecks_persisted_cancellation_immediately_before_call(tmp_path: Path) -> None:
    task_repo = TaskRepo([False, True])
    calls = 0

    def patcher(root, patch, **kwargs):
        nonlocal calls
        del root, patch, kwargs
        calls += 1
        return ToolResult.success(PatchApplyData(("app.py",), False, False))

    nodes = GraphNodes(deps(tmp_path, task_repo=task_repo, patcher=patcher))
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=2,
    )
    state.update(worktree_path=str(tmp_path / "repo"), patch_text="patch")

    update = await nodes.apply_patch(state)

    assert update["cancel_requested"] is True
    assert calls == 0


@pytest.mark.asyncio
async def test_test_node_observes_cancellation_that_arrives_during_runner(tmp_path: Path) -> None:
    task_repo = TaskRepo([False, False, True])
    dependencies = deps(tmp_path, task_repo=task_repo)
    nodes = GraphNodes(dependencies)
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=2,
    )
    state.update(worktree_path=str(tmp_path / "repo"))

    update = await nodes.run_tests(state)

    assert update["cancel_requested"] is True
    assert dependencies.test_runner.calls == 1
    assert not any(
        item[0] is EventType.TEST_COMPLETED
        for item in dependencies.event_repository.items
    )


@pytest.mark.asyncio
async def test_model_stage_cannot_replace_identity_or_retry_configuration(tmp_path: Path) -> None:
    nodes = GraphNodes(deps(tmp_path))
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=2,
    )
    state.update(worktree_path=str(tmp_path / "repo"), repo_summary="app.py")

    update = await nodes.plan_change(state)

    assert set(update) == {"change_plan", "current_stage"}
    assert "task_id" not in update
    assert "repo_path" not in update
    assert "max_retries" not in update


@pytest.mark.asyncio
async def test_schedule_retry_is_only_code_retry_increment(tmp_path: Path) -> None:
    nodes = GraphNodes(deps(tmp_path))
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="pytest -q",
        max_retries=2,
    )

    update = await nodes.schedule_retry(state)

    assert update["retry_count"] == 1
    assert set(update) == {
        "retry_count",
        "current_stage",
        "failure_analysis",
        "error_type",
        "error_message",
    }


@pytest.mark.asyncio
async def test_approval_payload_describes_command_without_raw_arguments(
    tmp_path: Path,
) -> None:
    nodes = GraphNodes(deps(tmp_path))
    state = initial_state(
        task_id="task-1",
        thread_id="11111111-1111-4111-8111-111111111111",
        repo_path=str(tmp_path / "repo"),
        user_request="Change",
        test_command="npm test -- private-argument",
        max_retries=1,
    )

    update = await nodes.validate_request(state)

    approval = update["pending_approval"]
    assert approval == {
        "action": "test_command",
        "executable": "npm",
        "argument_count": 3,
        "category": "command_policy",
        "reason": "executable_not_allowlisted",
    }
    assert "private-argument" not in str(approval)
