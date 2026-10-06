from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
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


def test_create_uses_exact_captured_commit_after_original_advances(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    commit_a = _git(git_repo, "rev-parse", "HEAD").strip()
    (git_repo / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    _git(git_repo, "add", "app.py")
    _git(git_repo, "commit", "-m", "advance original")
    commit_b = _git(git_repo, "rev-parse", "HEAD").strip()
    before = capture_repo_snapshot(git_repo)

    manager = WorktreeManager(tmp_path / "data")
    result = manager.create(git_repo, "fixed-base", base_commit=commit_a)

    assert result.ok is True
    assert result.data is not None
    assert result.data.base_commit == commit_a
    assert _git(result.data.path, "rev-parse", "HEAD").strip() == commit_a
    assert (result.data.path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _git(git_repo, "rev-parse", "HEAD").strip() == commit_b
    assert capture_repo_snapshot(git_repo).data == before.data
    assert manager.cleanup(result.data, approved=True).ok is True


@pytest.mark.parametrize(
    "base_factory",
    [
        lambda repo: _git(repo, "symbolic-ref", "--short", "HEAD").strip(),
        lambda repo: _git(repo, "rev-parse", "--short", "HEAD").strip(),
        lambda repo: "0" * 40,
        lambda repo: _git(repo, "hash-object", "app.py").strip(),
    ],
    ids=["branch-name", "abbreviated-hash", "missing-object", "non-commit-object"],
)
def test_rejects_base_that_is_not_an_existing_full_commit_id(
    git_repo: Path,
    tmp_path: Path,
    base_factory,
) -> None:
    result = WorktreeManager(tmp_path / "data").create(
        git_repo,
        "invalid-base",
        base_commit=base_factory(git_repo),
    )

    assert result.ok is False
    assert result.error_code == "invalid_base_commit"


def test_existing_task_worktree_must_match_requested_base(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(tmp_path / "data")
    commit_a = _git(git_repo, "rev-parse", "HEAD").strip()
    first = manager.create(git_repo, "same-task", base_commit=commit_a)
    assert first.ok is True
    assert first.data is not None
    (git_repo / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    _git(git_repo, "add", "app.py")
    _git(git_repo, "commit", "-m", "advance original")
    commit_b = _git(git_repo, "rev-parse", "HEAD").strip()

    second = manager.create(git_repo, "same-task", base_commit=commit_b)

    assert second.ok is False
    assert second.error_code == "workspace_conflict"
    assert _git(first.data.path, "rev-parse", "HEAD").strip() == commit_a
    assert manager.cleanup(first.data, approved=True).ok is True


def test_rejects_invalid_task_id(git_repo: Path, tmp_path: Path) -> None:
    result = WorktreeManager(tmp_path / "data").create(git_repo, "../escape")

    assert result.ok is False
    assert result.error_code == "invalid_task_id"


def test_rejects_data_directory_inside_original_repository(git_repo: Path) -> None:
    result = WorktreeManager(git_repo / ".repopilot-data").create(git_repo, "task-2")

    assert result.ok is False
    assert result.error_code == "data_dir_inside_repository"
    assert not (git_repo / ".repopilot-data").exists()


def test_rejects_symlink_escape_below_data_directory(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    data_dir.mkdir()
    outside.mkdir()
    try:
        (data_dir / "worktrees").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"The operating system denied symlink creation: {exc}")

    result = WorktreeManager(data_dir).create(git_repo, "escaped-task")

    assert result.ok is False
    assert result.error_code == "workspace_boundary_violation"
    assert not any(outside.iterdir())


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


def test_cleanup_rejects_forged_task_identity(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path / "data")
    created = manager.create(git_repo, "victim")
    assert created.ok is True
    assert created.data is not None
    info = created.data
    forged = replace(
        info,
        task_id=f"../{info.path.parent.name}/{info.task_id}",
    )

    result = manager.cleanup(forged, approved=True)

    assert result.ok is False
    assert result.error_code == "invalid_task_id"
    assert info.path.exists()
    assert manager.cleanup(info, approved=True).ok is True


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
