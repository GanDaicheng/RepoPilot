from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from repopilot.tools.filesystem import list_files, read_file
from repopilot.tools.git import git_diff, git_status
from repopilot.tools.patch import apply_patch
from repopilot.tools.search import search_code
from repopilot.tools.tests import DockerTestRunner
from repopilot.workspace.worktree import WorktreeManager, capture_repo_snapshot


pytestmark = [pytest.mark.integration, pytest.mark.docker]
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _copy_fixture_repo(destination: Path) -> Path:
    shutil.copytree(FIXTURE_DIR, destination)
    _git(destination, "init", "--quiet")
    _git(destination, "add", "--all")
    _git(
        destination,
        "-c",
        "user.name=RepoPilotTest",
        "-c",
        "user.email=test@repopilot.invalid",
        "commit",
        "--quiet",
        "-m",
        "calculator fixture",
    )
    return destination


def test_m1_tools_fix_fixture_in_isolated_worktree(tmp_path: Path) -> None:
    original_repo = _copy_fixture_repo(tmp_path / "original")
    original_before = capture_repo_snapshot(original_repo)
    assert original_before.ok is True
    manager = WorktreeManager(tmp_path / "repopilot-data")
    created = manager.create(original_repo, "calculator-fix")
    assert created.ok is True
    assert created.data is not None
    info = created.data

    try:
        listed = list_files(info.path)
        assert listed.ok is True
        assert listed.data == ("calculator.py", "test_calculator.py")

        matches = search_code(info.path, "return a / b", file_glob="*.py")
        assert matches.ok is True
        assert matches.data is not None
        assert [match.path for match in matches.data] == ["calculator.py"]

        source = read_file(info.path, "calculator.py")
        assert source.ok is True
        assert source.data is not None
        assert "return a / b" in source.data.text

        repaired = apply_patch(info.path, REPAIR_PATCH)
        assert repaired.ok is True
        assert repaired.data is not None
        assert repaired.data.changed_files == ("calculator.py",)

        status = git_status(info.path)
        diff = git_diff(info.path)
        assert status.ok is True and status.data is not None
        assert diff.ok is True and diff.data is not None
        assert status.data.changed_files == ("calculator.py",)
        assert diff.data.changed_files == ("calculator.py",)
        assert "raise ValueError" in diff.data.patch

        runner = DockerTestRunner()
        availability = runner.is_available()
        if not availability.ok and availability.error_code == "docker_unavailable":
            pytest.skip(f"Docker daemon unavailable: {availability.message}")
        assert availability.ok, availability.message
        test_run = runner.run(info.path, "pytest -q")
        assert test_run.ok is True
        assert test_run.data is not None
        assert test_run.data.exit_code == 0

        original_after = capture_repo_snapshot(original_repo)
        assert original_after.data == original_before.data

        refused = manager.cleanup(info)
        assert refused.ok is False
        assert refused.error_code == "approval_required"
        removed = manager.cleanup(info, approved=True)
        assert removed.ok is True
    finally:
        if info.path.exists():
            manager.cleanup(info, approved=True)
