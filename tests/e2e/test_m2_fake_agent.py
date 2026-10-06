from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.workspace.worktree import capture_repo_snapshot
from tests.helpers.m2 import (
    BAD_PATCH,
    FIX_BAD_PATCH,
    REPAIR_PATCH,
    copy_calculator_repo,
    make_app,
    scripted_transport,
    sse_event_names,
    submit,
    wait_for_status,
)


pytestmark = [pytest.mark.integration, pytest.mark.docker]


def test_first_patch_success_runs_full_http_graph_docker_and_sse_flow(tmp_path: Path) -> None:
    repo = copy_calculator_repo(tmp_path / "original")
    original = capture_repo_snapshot(repo).data
    transport = scripted_transport([REPAIR_PATCH])
    app = make_app(tmp_path, transport)

    with TestClient(app) as client:
        created = submit(client, repo)
        names = sse_event_names(client, created["id"])
        task = client.get(f"/tasks/{created['id']}").json()

    assert task["status"] == "succeeded"
    assert task["retry_count"] == 0
    assert task["worktree_path"] is not None
    assert "raise ValueError" in (
        Path(task["worktree_path"]) / "calculator.py"
    ).read_text(encoding="utf-8")
    assert capture_repo_snapshot(repo).data == original
    required = [
        "task_created", "task_started", "patch_applied",
        "test_completed", "task_succeeded",
    ]
    assert [name for name in names if name in required] == required


def test_failed_patch_is_analyzed_and_fixed_once(tmp_path: Path) -> None:
    repo = copy_calculator_repo(tmp_path / "original")
    transport = scripted_transport(
        [BAD_PATCH, FIX_BAD_PATCH], failure_analyses=1
    )
    app = make_app(tmp_path, transport)

    with TestClient(app) as client:
        created = submit(client, repo, max_retries=2)
        task = wait_for_status(client, created["id"], {"succeeded", "failed"})
        names = sse_event_names(client, created["id"])

    assert task["status"] == "succeeded"
    assert task["retry_count"] == 1
    assert names.count("retry_scheduled") == 1
    assert names.count("patch_applied") == 2
    assert names.count("test_completed") == 2


def test_persistent_failure_stops_at_exact_retry_limit(tmp_path: Path) -> None:
    repo = copy_calculator_repo(tmp_path / "original")
    transport = scripted_transport(
        [BAD_PATCH, BAD_PATCH, BAD_PATCH],
        failure_analyses=3,
        include_review=False,
    )
    app = make_app(tmp_path, transport)

    with TestClient(app) as client:
        created = submit(client, repo, max_retries=2)
        task = wait_for_status(client, created["id"], {"succeeded", "failed"})
        names = sse_event_names(client, created["id"])

    assert task["status"] == "failed"
    assert task["error_type"] == "retry_exhausted"
    assert task["retry_count"] == 2
    assert names.count("patch_applied") == 3
    assert names.count("test_completed") == 3
    assert names.count("retry_scheduled") == 2
