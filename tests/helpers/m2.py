from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot.api.app import create_app
from repopilot.api.dependencies import AppOverrides, AppSettings
from repopilot.models.fake import ScriptedFakeTransport
from repopilot.models.profiles import ModelProvider
from repopilot.models.types import ModelTurn, ModelUsage


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "calculator"
REPAIR_PATCH = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,4 @@
 def divide(a: float, b: float) -> float:
+    if b == 0:
+        raise ValueError("division by zero")
     return a / b
"""
BAD_PATCH = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def divide(a: float, b: float) -> float:
-    return a / b
+    return a // b
"""
FIX_BAD_PATCH = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,4 @@
 def divide(a: float, b: float) -> float:
-    return a // b
+    if b == 0:
+        raise ValueError("division by zero")
+    return a / b
"""
DELETE_PATCH = """diff --git a/calculator.py b/calculator.py
deleted file mode 100644
--- a/calculator.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def divide(a: float, b: float) -> float:
-    return a / b
"""


def turn(payload: dict[str, object]) -> ModelTurn:
    return ModelTurn(
        content=json.dumps(payload),
        tool_calls=(),
        usage=ModelUsage(5, 5, 10),
    )


def plan_turn() -> ModelTurn:
    return turn(
        {
            "goal": "Reject division by zero",
            "relevant_files": ["calculator.py", "test_calculator.py"],
            "steps": ["Add a zero guard", "Run tests"],
            "risks": [],
            "suggested_tests": ["pytest -q"],
        }
    )


def patch_turn(patch: str) -> ModelTurn:
    return turn(
        {
            "patch_text": patch,
            "summary": "Update calculator behavior",
            "expected_files": ["calculator.py"],
        }
    )


def analysis_turn(*, fixable: bool = True) -> ModelTurn:
    return turn(
        {
            "root_cause": "The zero divisor still raises the wrong exception",
            "evidence": ["zero divisor test failed"],
            "fixable": fixable,
            "strategy": "Add the required guard" if fixable else None,
            "suggested_files": ["calculator.py"],
        }
    )


def review_turn() -> ModelTurn:
    return turn(
        {
            "approved": True,
            "critical_findings": [],
            "warnings": [],
            "conclusion": "Tests pass and the change is scoped",
        }
    )


def scripted_transport(
    patches: list[str],
    *,
    failure_analyses: int = 0,
    include_review: bool = True,
) -> ScriptedFakeTransport:
    scripts: dict[str, list[ModelTurn]] = {
        "planning": [plan_turn()],
        "patching": [patch_turn(patch) for patch in patches],
    }
    if failure_analyses:
        scripts["failure_analysis"] = [analysis_turn() for _ in range(failure_analyses)]
    if include_review:
        scripts["review"] = [review_turn()]
    return ScriptedFakeTransport(scripts)


def copy_calculator_repo(destination: Path) -> Path:
    shutil.copytree(FIXTURE_DIR, destination)
    subprocess.run(["git", "init", "--quiet"], cwd=destination, check=True)
    subprocess.run(["git", "add", "--all"], cwd=destination, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=RepoPilotTest",
            "-c", "user.email=test@repopilot.invalid",
            "commit", "--quiet", "-m", "calculator fixture",
        ],
        cwd=destination,
        check=True,
    )
    return destination


def make_app(
    tmp_path: Path,
    transport: ScriptedFakeTransport,
    *,
    test_runner=None,
):
    return create_app(
        AppSettings(data_dir=tmp_path / "data", poll_interval=0.01, environ={}),
        overrides=AppOverrides(
            transports={ModelProvider.FAKE: transport},
            test_runner=test_runner,
        ),
    )


def submit(client: TestClient, repo: Path, *, max_retries: int = 2, command: str = "pytest -q") -> dict:
    response = client.post(
        "/tasks",
        json={
            "repo_path": str(repo),
            "user_request": "Raise ValueError for division by zero",
            "test_command": command,
            "model_profile": "fake",
            "max_retries": max_retries,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def wait_for_status(
    client: TestClient,
    task_id: str,
    expected: set[str],
    *,
    timeout: float = 20,
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in expected:
            return last
        time.sleep(0.02)
    raise AssertionError(f"Task did not reach {expected}; last snapshot: {last}")


def sse_event_names(client: TestClient, task_id: str) -> list[str]:
    response = client.get(f"/tasks/{task_id}/events")
    assert response.status_code == 200
    return [line[7:] for line in response.text.splitlines() if line.startswith("event: ")]


def persisted_event_names(client: TestClient, app, task_id: str) -> list[str]:
    events = client.portal.call(
        app.state.runtime.event_repository.list_after, task_id, 0
    )
    return [event.event_type.value for event in events]
