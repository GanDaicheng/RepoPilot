from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers.m2 import (
    DELETE_PATCH,
    copy_calculator_repo,
    make_app,
    persisted_event_names,
    scripted_transport,
    submit,
    wait_for_status,
)


pytestmark = pytest.mark.integration


def test_deletion_patch_pauses_without_deleting_file(tmp_path: Path) -> None:
    repo = copy_calculator_repo(tmp_path / "original")
    transport = scripted_transport([DELETE_PATCH], include_review=False)
    app = make_app(tmp_path, transport)

    with TestClient(app) as client:
        created = submit(client, repo)
        task = wait_for_status(client, created["id"], {"awaiting_approval", "failed"})
        names = persisted_event_names(client, app, created["id"])

    assert task["status"] == "awaiting_approval"
    assert "approval_required" in names
    assert Path(task["worktree_path"], "calculator.py").exists()


def test_unallowlisted_command_pauses_before_model_or_test(tmp_path: Path) -> None:
    repo = copy_calculator_repo(tmp_path / "original")
    transport = scripted_transport([], include_review=False)
    app = make_app(tmp_path, transport)

    with TestClient(app) as client:
        created = submit(client, repo, command="npm test")
        task = wait_for_status(client, created["id"], {"awaiting_approval", "failed"})
        names = persisted_event_names(client, app, created["id"])

    assert task["status"] == "awaiting_approval"
    assert "approval_required" in names
    assert transport.requests == []
    assert task["worktree_path"] is None
