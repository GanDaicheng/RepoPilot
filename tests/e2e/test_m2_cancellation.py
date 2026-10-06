from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.domain.results import ToolResult
from repopilot.tools.tests import TestRunResult as RunResult
from tests.helpers.m2 import (
    REPAIR_PATCH,
    copy_calculator_repo,
    make_app,
    scripted_transport,
    submit,
    wait_for_status,
)


pytestmark = pytest.mark.integration


class BlockingRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def run(self, root: Path, command: str):
        del root, command
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=10)
        return ToolResult.success(
            RunResult(("pytest", "-q"), 0, "passed\n", "", False, 1)
        )


def test_cancel_during_test_prevents_review_or_later_side_effect(tmp_path: Path) -> None:
    repo = copy_calculator_repo(tmp_path / "original")
    runner = BlockingRunner()
    transport = scripted_transport([REPAIR_PATCH])
    app = make_app(tmp_path, transport, test_runner=runner)

    with TestClient(app) as client:
        created = submit(client, repo)
        assert runner.started.wait(timeout=10)
        cancelled = client.post(f"/tasks/{created['id']}/cancel")
        assert cancelled.status_code == 200
        runner.release.set()
        task = wait_for_status(client, created["id"], {"cancelled", "succeeded"})

    assert task["status"] == "cancelled"
    assert runner.calls == 1
    assert [request.stage for request in transport.requests] == ["planning", "patching"]
