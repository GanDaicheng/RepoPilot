from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot.api.app import create_app
from repopilot.api.dependencies import AppOverrides, AppSettings


class IdleWorker:
    running = False

    def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False


def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        poll_interval=0.01,
        environ={},
        allowed_repo_roots=(tmp_path,),
    )


def payload(repo: Path, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "repo_path": str(repo),
        "user_request": "Change the value safely",
        "test_command": "pytest -q",
        "model_profile": "fake",
        "max_retries": 1,
    }
    value.update(overrides)
    return value


async def business_table_counts(database) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with database.connection() as connection:
        for table in ("tasks", "events", "model_calls"):
            row = await (
                await connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"  # noqa: S608
                )
            ).fetchone()
            counts[table] = int(row["count"])
    return counts


def test_create_get_and_cancel_contract(git_repo: Path, tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), overrides=AppOverrides(worker=IdleWorker()))
    with TestClient(app) as client:
        created = client.post("/tasks", json=payload(git_repo))
        assert created.status_code == 202
        body = created.json()
        task_id = body["id"]
        assert body["status"] == "queued"
        assert "user_request" not in body
        assert "test_output" not in body
        assert "patch_text" not in body

        fetched = client.get(f"/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == task_id

        cancelled = client.post(f"/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert client.post(f"/tasks/{task_id}/cancel").status_code == 409
        assert client.get("/tasks/missing").status_code == 404
        assert client.post("/tasks/missing/cancel").status_code == 404


def test_invalid_inputs_map_to_422_without_persisting_secret(
    git_repo: Path, tmp_path: Path
) -> None:
    app = create_app(settings(tmp_path), overrides=AppOverrides(worker=IdleWorker()))
    cases = [
        payload(tmp_path / "missing"),
        payload(git_repo, test_command="pytest && remove all"),
        payload(git_repo, model_profile="unknown"),
        payload(git_repo, max_retries=6),
        payload(git_repo, user_request="use " + "sk-" + "x" * 20),
        payload(git_repo, user_request="use " + "sk-" + "proj-" + "x" * 20),
    ]
    with TestClient(app) as client:
        for item in cases:
            response = client.post("/tasks", json=item)
            assert response.status_code == 422
        counts = client.portal.call(
            business_table_counts, app.state.runtime.database
        )
        assert counts == {"tasks": 0, "events": 0, "model_calls": 0}


def test_request_body_and_field_lengths_are_bounded(
    git_repo: Path, tmp_path: Path
) -> None:
    app = create_app(settings(tmp_path), overrides=AppOverrides(worker=IdleWorker()))
    with TestClient(app) as client:
        oversized = client.post(
            "/tasks",
            content=b"x" * 65_537,
            headers={"content-type": "application/json"},
        )
        bounded_field = client.post(
            "/tasks", json=payload(git_repo, test_command="x" * 2_001)
        )

    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"
    assert bounded_field.status_code == 422


def test_unallowlisted_executable_is_accepted_then_safely_paused(
    git_repo: Path, tmp_path: Path
) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/tasks", json=payload(git_repo, test_command="npm test")
        )
        assert created.status_code == 202
        task_id = created.json()["id"]
        deadline = time.monotonic() + 5
        status = "queued"
        while time.monotonic() < deadline and status not in {"awaiting_approval", "failed"}:
            status = client.get(f"/tasks/{task_id}").json()["status"]
            time.sleep(0.02)
        assert status == "awaiting_approval"


def test_health_is_readiness_only_and_does_not_probe_paid_models(tmp_path: Path) -> None:
    worker = IdleWorker()
    app = create_app(settings(tmp_path), overrides=AppOverrides(worker=worker))
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "ready",
        "database": "ready",
        "worker": "running",
    }


def test_health_reports_worker_persistence_degradation(tmp_path: Path) -> None:
    worker = IdleWorker()
    worker.last_error = "persistence_unavailable"  # type: ignore[attr-defined]
    app = create_app(settings(tmp_path), overrides=AppOverrides(worker=worker))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["worker"] == "degraded"
