"""Injectable API settings and application runtime assembly."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncContextManager

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from repopilot.agent.repo_agent import RepoAgent
from repopilot.agent.toolbox import ReadOnlyToolbox
from repopilot.graph.builder import build_graph
from repopilot.graph.nodes import GraphDependencies
from repopilot.models.fake import ScriptedFakeTransport
from repopilot.models.gateway import ModelGateway, OpenAIChatTransport
from repopilot.models.profiles import ModelProfileRegistry, ModelProvider
from repopilot.models.types import ModelTransport
from repopilot.persistence.database import SqliteDatabase
from repopilot.persistence.repositories import (
    EventRepository,
    ModelCallRepository,
    TaskRepository,
)
from repopilot.services.task_service import TaskService
from repopilot.services.worker import TaskWorker
from repopilot.tools.tests import DockerTestRunner
from repopilot.workspace.worktree import WorktreeManager


CheckpointerFactory = Callable[[str], AsyncContextManager[Any]]


@dataclass(frozen=True, slots=True)
class AppSettings:
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "REPOPILOT_DATA_DIR",
                str(Path.home() / ".repopilot"),
            )
        )
    )
    poll_interval: float = 0.25
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    allowed_repo_roots: tuple[Path, ...] = field(
        default_factory=lambda: tuple(
            Path(item)
            for item in os.environ.get(
                "REPOPILOT_ALLOWED_REPO_ROOTS", str(Path.cwd())
            ).split(os.pathsep)
            if item
        )
    )

    @property
    def database_path(self) -> Path:
        return Path(self.data_dir) / "repopilot.sqlite3"

    @property
    def checkpoint_path(self) -> Path:
        return Path(self.data_dir) / "checkpoints.sqlite3"

    @property
    def worktree_data_dir(self) -> Path:
        return Path(self.data_dir) / "managed"


@dataclass(slots=True)
class AppOverrides:
    worker: Any | None = None
    checkpointer_factory: CheckpointerFactory | None = None
    transports: Mapping[ModelProvider, ModelTransport] | None = None
    test_runner: Any | None = None
    worktree_manager: Any | None = None


@dataclass(slots=True)
class AppRuntime:
    settings: AppSettings
    database: SqliteDatabase
    task_repository: TaskRepository
    event_repository: EventRepository
    model_call_repository: ModelCallRepository
    profile_registry: ModelProfileRegistry
    task_service: TaskService
    worker: Any
    checkpointer: Any


def default_checkpointer_factory(path: str) -> AsyncContextManager[Any]:
    return AsyncSqliteSaver.from_conn_string(path)


async def assemble_runtime(
    settings: AppSettings,
    overrides: AppOverrides,
    checkpointer: Any,
) -> AppRuntime:
    database = SqliteDatabase(settings.database_path)
    await database.initialize()
    tasks = TaskRepository(database)
    events = EventRepository(database)
    model_calls = ModelCallRepository(database)
    profiles = ModelProfileRegistry.from_env(settings.environ)
    service = TaskService(tasks, profiles, settings.allowed_repo_roots)

    worker = overrides.worker
    if worker is None:
        remote_transport = OpenAIChatTransport()
        transports: Mapping[ModelProvider, ModelTransport] = overrides.transports or {
            ModelProvider.FAKE: ScriptedFakeTransport({}),
            ModelProvider.DEEPSEEK: remote_transport,
            ModelProvider.QWEN: remote_transport,
            ModelProvider.CUSTOM: remote_transport,
        }
        gateway = ModelGateway(transports=transports, model_call_sink=model_calls)
        worktrees = overrides.worktree_manager or WorktreeManager(
            settings.worktree_data_dir
        )
        runner = overrides.test_runner or DockerTestRunner()

        def graph_factory(task):
            profile = profiles.resolve(task.model_profile)
            agent = RepoAgent(gateway, ReadOnlyToolbox())
            dependencies = GraphDependencies(
                task_repository=tasks,
                event_repository=events,
                profile=profile,
                repo_agent=agent,
                worktree_manager=worktrees,
                test_runner=runner,
            )
            return build_graph(dependencies, checkpointer)

        worker = TaskWorker(
            tasks,
            graph_factory,
            poll_interval=settings.poll_interval,
        )

    return AppRuntime(
        settings=settings,
        database=database,
        task_repository=tasks,
        event_repository=events,
        model_call_repository=model_calls,
        profile_registry=profiles,
        task_service=service,
        worker=worker,
        checkpointer=checkpointer,
    )
