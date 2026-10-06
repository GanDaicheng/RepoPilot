from __future__ import annotations

import subprocess
from pathlib import Path

from repopilot.tools.git import git_diff, git_status


def _cached_diff(repo: Path) -> str:
    return subprocess.run(
        ["git", "diff", "--cached", "--binary", "--"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def test_git_status_reports_modified_and_untracked_paths(git_repo: Path) -> None:
    (git_repo / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    (git_repo / "new.txt").write_text("new\n", encoding="utf-8", newline="")

    result = git_status(git_repo)

    assert result.ok is True
    assert result.data is not None
    assert result.data.clean is False
    assert result.data.changed_files == ("app.py", "new.txt")
    assert "? new.txt" in result.data.raw_porcelain_v2


def test_git_status_clean_repository(git_repo: Path) -> None:
    result = git_status(git_repo)

    assert result.ok is True
    assert result.data is not None
    assert result.data.clean is True
    assert result.data.changed_files == ()


def test_git_diff_contains_tracked_modification(git_repo: Path) -> None:
    (git_repo / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")

    result = git_diff(git_repo)

    assert result.ok is True
    assert result.data is not None
    assert result.data.changed_files == ("app.py",)
    assert "-value = 1" in result.data.patch
    assert "+value = 2" in result.data.patch


def test_git_diff_contains_untracked_new_file_without_staging(git_repo: Path) -> None:
    (git_repo / "new.py").write_text("answer = 42\n", encoding="utf-8", newline="")

    result = git_diff(git_repo)

    assert result.ok is True
    assert result.data is not None
    assert result.data.changed_files == ("new.py",)
    assert "new.py" in result.data.patch
    assert "+answer = 42" in result.data.patch
    assert _cached_diff(git_repo) == ""


def test_git_diff_does_not_change_index(git_repo: Path) -> None:
    (git_repo / "app.py").write_text("value = 3\n", encoding="utf-8", newline="")
    (git_repo / "other.txt").write_text("other\n", encoding="utf-8", newline="")
    before = _cached_diff(git_repo)

    git_diff(git_repo)

    assert _cached_diff(git_repo) == before


def test_git_tools_reject_non_repository(tmp_path: Path) -> None:
    status = git_status(tmp_path)
    diff = git_diff(tmp_path)

    assert status.ok is False
    assert status.error_code == "not_git_repository"
    assert diff.ok is False
    assert diff.error_code == "not_git_repository"
