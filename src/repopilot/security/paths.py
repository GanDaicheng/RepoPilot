"""Canonical path containment for repository worktrees."""

from __future__ import annotations

from pathlib import Path


class WorkspaceBoundaryError(ValueError):
    """Raised when a requested path escapes the configured worktree."""


def resolve_workspace_path(
    worktree_root: Path,
    relative_path: str | Path,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a user-supplied path while keeping it inside ``worktree_root``."""

    requested = Path(relative_path)
    if requested.is_absolute():
        raise WorkspaceBoundaryError("Workspace paths must be relative.")
    if ".." in requested.parts:
        raise WorkspaceBoundaryError("The requested path points outside the workspace.")

    try:
        resolved_root = Path(worktree_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceBoundaryError("The workspace root cannot be resolved.") from exc
    if not resolved_root.is_dir():
        raise WorkspaceBoundaryError("The workspace root must be a directory.")

    candidate = resolved_root.joinpath(requested)
    try:
        resolved_candidate = candidate.resolve(strict=must_exist)
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError) as exc:
        raise WorkspaceBoundaryError("The requested path cannot be resolved safely.") from exc

    if not resolved_candidate.is_relative_to(resolved_root):
        raise WorkspaceBoundaryError("The requested path points outside the workspace.")
    return resolved_candidate

