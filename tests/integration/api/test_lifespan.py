from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot.api.app import create_app
from repopilot.api.dependencies import AppOverrides, AppSettings


class WorkerProbe:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def running(self) -> bool:
        return self.start_calls == 1 and self.stop_calls == 0

    def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1


def test_lifespan_initializes_resources_once_and_closes_checkpoint(tmp_path: Path) -> None:
    worker = WorkerProbe()
    observed: list[str] = []

    @asynccontextmanager
    async def saver_factory(path: str):
        assert os.environ.get("LANGGRAPH_STRICT_MSGPACK") == "true"
        observed.append(f"open:{path}")
        yield object()
        observed.append("closed")

    app = create_app(
        AppSettings(data_dir=tmp_path / "data", environ={}),
        overrides=AppOverrides(worker=worker, checkpointer_factory=saver_factory),
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert worker.start_calls == 1
        assert app.state.runtime.database.path.exists()

    assert worker.stop_calls == 1
    assert observed[0].startswith("open:")
    assert observed[-1] == "closed"
