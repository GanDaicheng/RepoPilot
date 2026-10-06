from __future__ import annotations

import os
from pathlib import Path

import pytest

from repopilot.security.paths import WorkspaceBoundaryError, resolve_workspace_path


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"The operating system denied symlink creation: {exc}")


def test_resolve_accepts_existing_child(tmp_path: Path) -> None:
    child = tmp_path / "src" / "main.py"
    child.parent.mkdir()
    child.write_text("print('ok')\n", encoding="utf-8")

    assert resolve_workspace_path(tmp_path, "src/main.py") == child.resolve()


def test_resolve_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceBoundaryError, match="relative"):
        resolve_workspace_path(tmp_path, tmp_path / "file.py", must_exist=False)


def test_resolve_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        resolve_workspace_path(tmp_path, Path("..") / "outside.py", must_exist=False)


def test_resolve_rejects_symlink_to_outside(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    _symlink_or_skip(workspace / "escape", outside)

    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        resolve_workspace_path(workspace, "escape/secret.txt")


def test_resolve_rejects_missing_descendant_below_external_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _symlink_or_skip(workspace / "escape", outside)

    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        resolve_workspace_path(
            workspace,
            os.path.join("escape", "missing", "file.txt"),
            must_exist=False,
        )
