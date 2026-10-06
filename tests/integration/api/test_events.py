from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.api.app import create_app
from repopilot.api.dependencies import AppOverrides, AppSettings
from repopilot.api.sse import event_stream, parse_last_event_id
from repopilot.domain.tasks import EventRecord, EventType, TaskStatus


pytestmark = pytest.mark.integration


class IdleWorker:
    running = False

    def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False


def payload(repo: Path) -> dict[str, object]:
    return {
        "repo_path": str(repo),
        "user_request": "Change",
        "test_command": "pytest -q",
        "model_profile": "fake",
        "max_retries": 1,
    }


async def finish_task(runtime, task_id: str) -> tuple[int, ...]:
    await runtime.task_repository.claim_next()
    await runtime.event_repository.append(
        task_id,
        EventType.STAGE_STARTED,
        stage="planning",
        payload={"stage": "planning"},
        dedupe_key=f"{task_id}:planning",
    )
    await runtime.task_repository.transition(
        task_id,
        TaskStatus.SUCCEEDED,
        stage="succeeded",
        event_type=EventType.TASK_SUCCEEDED,
        payload={"status": "succeeded"},
        dedupe_key=f"{task_id}:succeeded",
    )
    events = await runtime.event_repository.list_after(task_id, 0)
    return tuple(event.id for event in events)


def test_endpoint_serializes_ordered_events_and_closes_on_terminal(
    git_repo: Path, tmp_path: Path
) -> None:
    app = create_app(
        AppSettings(data_dir=tmp_path / "data", environ={}),
        overrides=AppOverrides(worker=IdleWorker()),
    )
    with TestClient(app) as client:
        task_id = client.post("/tasks", json=payload(git_repo)).json()["id"]
        ids = client.portal.call(finish_task, app.state.runtime, task_id)

        response = client.get(f"/tasks/{task_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")] == list(ids)
    assert "event: task_created" in response.text
    assert "event: task_succeeded" in response.text
    data_lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert all(json.loads(line)["task_id"] == task_id for line in data_lines)


def test_last_event_id_and_foreign_global_cursor_never_reveal_other_task(
    git_repo: Path, tmp_path: Path
) -> None:
    app = create_app(
        AppSettings(data_dir=tmp_path / "data", environ={}),
        overrides=AppOverrides(worker=IdleWorker()),
    )
    with TestClient(app) as client:
        first = client.post("/tasks", json=payload(git_repo)).json()["id"]
        first_ids = client.portal.call(finish_task, app.state.runtime, first)
        resumed = client.get(
            f"/tasks/{first}/events", headers={"Last-Event-ID": str(first_ids[-2])}
        )
        assert f"id: {first_ids[-2]}\n" not in resumed.text
        assert f"id: {first_ids[-1]}\n" in resumed.text

        second = client.post("/tasks", json=payload(git_repo)).json()["id"]
        second_created = client.portal.call(
            app.state.runtime.event_repository.list_after, second, 0
        )[0]
        foreign = client.get(
            f"/tasks/{first}/events",
            headers={"Last-Event-ID": str(second_created.id)},
        )
        assert second not in foreign.text


@pytest.mark.parametrize(
    "value",
    ["-1", "abc", " 1", "1.5", "9" * 40],
)
def test_invalid_last_event_id_is_422(
    git_repo: Path, tmp_path: Path, value: str
) -> None:
    app = create_app(
        AppSettings(data_dir=tmp_path / "data", environ={}),
        overrides=AppOverrides(worker=IdleWorker()),
    )
    with TestClient(app) as client:
        task_id = client.post("/tasks", json=payload(git_repo)).json()["id"]
        response = client.get(
            f"/tasks/{task_id}/events", headers={"Last-Event-ID": value}
        )
    assert response.status_code == 422


def test_missing_task_is_404(tmp_path: Path) -> None:
    app = create_app(
        AppSettings(data_dir=tmp_path / "data", environ={}),
        overrides=AppOverrides(worker=IdleWorker()),
    )
    with TestClient(app) as client:
        assert client.get("/tasks/missing/events").status_code == 404


def test_parse_last_event_id_contract() -> None:
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id("0") == 0
    assert parse_last_event_id("42") == 42
    with pytest.raises(ValueError):
        parse_last_event_id("-1")


@pytest.mark.asyncio
async def test_stream_redacts_credential_shaped_payload() -> None:
    event = EventRecord(
        id=1,
        task_id="task-1",
        event_type=EventType.TASK_FAILED,
        stage="failed",
        payload={"message": "token " + "sk-" + "x" * 20},
        dedupe_key=None,
        created_at=datetime.now(timezone.utc),
    )

    class Events:
        async def list_after(self, task_id, event_id, *, limit=100):
            del task_id, event_id, limit
            return (event,)

    chunk = await anext(event_stream("task-1", 0, Events()))

    assert b"[redacted]" in chunk
    assert ("sk-" + "x" * 20).encode() not in chunk


@pytest.mark.asyncio
async def test_stream_heartbeats_and_never_requests_unbounded_batches() -> None:
    class EmptyEvents:
        def __init__(self) -> None:
            self.limits: list[int] = []

        async def list_after(self, task_id, event_id, *, limit=100):
            del task_id, event_id
            self.limits.append(limit)
            return ()

    events = EmptyEvents()
    stream = event_stream(
        "task-1", 0, events,
        poll_interval=0.001, heartbeat_interval=0, batch_size=1_000,
    )

    assert await anext(stream) == b": heartbeat\n\n"
    assert events.limits == [100]
    await stream.aclose()


@pytest.mark.asyncio
async def test_concurrent_sqlite_write_becomes_visible_to_open_stream(
    tmp_path: Path,
) -> None:
    from repopilot.domain.tasks import TaskInput
    from repopilot.models.profiles import ModelProfileRegistry
    from repopilot.persistence.database import SqliteDatabase
    from repopilot.persistence.repositories import EventRepository, TaskRepository
    from repopilot.services.task_service import TaskService
    from tests.helpers.git import init_repo

    repo = init_repo(tmp_path / "repo", {"app.py": "value = 1\n"})
    database = SqliteDatabase(tmp_path / "events.sqlite3")
    await database.initialize()
    tasks = TaskRepository(database)
    events = EventRepository(database)
    service = TaskService(tasks, ModelProfileRegistry.from_env({}))
    task = await service.create_task(TaskInput(repo, "Change", "pytest -q", "fake", 1))
    created = (await events.list_after(task.id, 0))[0]
    stream = event_stream(
        task.id, created.id, events, task_repository=tasks,
        poll_interval=0.001, heartbeat_interval=60,
    )
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    appended = await events.append(
        task.id, EventType.TASK_CANCELLED, stage="cancelled",
        payload={"status": "cancelled"}, dedupe_key=f"{task.id}:cancelled",
    )

    chunk = await asyncio.wait_for(next_event, timeout=2)

    assert f"id: {appended.id}\n".encode() in chunk
    await stream.aclose()
