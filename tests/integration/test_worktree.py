from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from repopilot.workspace.worktree import WorktreeManager, capture_repo_snapshot


pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _file_hashes(repo: Path) -> dict[str, str]:
    return {
        path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def test_create_worktree_at_committed_head_without_touching_original(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(tmp_path / "data")
    before = capture_repo_snapshot(git_repo)

    result = manager.create(git_repo, "task-1")
    after = capture_repo_snapshot(git_repo)

    assert result.ok is True
    assert result.data is not None
    assert before.data == after.data
    assert result.data.base_commit == before.data.head  # type: ignore[union-attr]
    assert result.data.branch == "repopilot/task-1"
    assert (result.data.path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert manager.cleanup(result.data, approved=True).ok is True


def test_dirty_original_state_is_byte_for_byte_unchanged(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    app = git_repo / "app.py"
    app.write_text("value = 2\n", encoding="utf-8", newline="")
    _git(git_repo, "add", "app.py")
    app.write_text("value = 3\n", encoding="utf-8", newline="")
    (git_repo / "untracked.txt").write_text("new\n", encoding="utf-8", newline="")
    before_snapshot = capture_repo_snapshot(git_repo)
    before_files = _file_hashes(git_repo)

    manager = WorktreeManager(tmp_path / "data")
    result = manager.create(git_repo, "dirty-task")

    assert result.ok is True
    assert result.data is not None
    assert capture_repo_snapshot(git_repo).data == before_snapshot.data
    assert _file_hashes(git_repo) == before_files
    assert (result.data.path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert manager.cleanup(result.data, approved=True).ok is True


def test_create_is_idempotent_for_registered_task_worktree(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(tmp_path / "data")

    first = manager.create(git_repo, "repeatable")
    second = manager.create(git_repo, "repeatable")

    assert first.ok is True
    assert second.ok is True
    assert second.data == first.data
    assert first.data is not None
    assert manager.cleanup(first.data, approved=True).ok is True


def test_rejects_invalid_task_id(git_repo: Path, tmp_path: Path) -> None:
    result = WorktreeManager(tmp_path / "data").create(git_repo, "../escape")

    assert result.ok is False
    assert result.error_code == "invalid_task_id"


def test_rejects_data_directory_inside_original_repository(git_repo: Path) -> None:
    result = WorktreeManager(git_repo / ".repopilot-data").create(git_repo, "task-2")

    assert result.ok is False
    assert result.error_code == "data_dir_inside_repository"


def test_cleanup_requires_explicit_approval(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path / "data")
    created = manager.create(git_repo, "keep-me")
    assert created.ok is True
    assert created.data is not None

    result = manager.cleanup(created.data)

    assert result.ok is False
    assert result.error_code == "approval_required"
    assert created.data.path.exists()
    assert manager.cleanup(created.data, approved=True).ok is True


def test_approved_cleanup_removes_only_dirty_managed_worktree(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sentinel = data_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    manager = WorktreeManager(data_dir)
    created = manager.create(git_repo, "remove-me")
    assert created.ok is True
    assert created.data is not None
    (created.data.path / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = manager.cleanup(created.data, approved=True)

    assert result.ok is True
    assert not created.data.path.exists()
    assert git_repo.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"

