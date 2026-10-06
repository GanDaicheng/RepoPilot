"""Read-only Git status and complete working-tree diff tools."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from repopilot.domain.results import ToolResult
from repopilot.security.paths import WorkspaceBoundaryError, resolve_workspace_path


@dataclass(frozen=True, slots=True)
class GitStatusData:
    clean: bool
    changed_files: tuple[str, ...]
    raw_porcelain_v2: str


@dataclass(frozen=True, slots=True)
class GitDiffData:
    patch: str
    changed_files: tuple[str, ...]


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )


def _resolve_repository(worktree_root: Path) -> tuple[Path | None, str | None]:
    try:
        root = resolve_workspace_path(Path(worktree_root), ".")
    except (WorkspaceBoundaryError, FileNotFoundError):
        return None, "workspace_boundary_violation"
    try:
        probe = _run_git(root, "rev-parse", "--is-inside-work-tree")
    except FileNotFoundError:
        return None, "tool_unavailable"
    except (OSError, UnicodeError):
        return None, "git_failed"
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None, "not_git_repository"
    return root, None


def _status_paths(raw: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    changed: set[str] = set()
    untracked: set[str] = set()
    records = raw.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        if not record:
            index += 1
            continue
        if record.startswith("1 "):
            changed.add(record.split(" ", 8)[-1])
        elif record.startswith("2 "):
            changed.add(record.split(" ", 9)[-1])
            index += 1  # Rename/copy records carry the original path next.
        elif record.startswith("u "):
            changed.add(record.split(" ", 10)[-1])
        elif record.startswith("? "):
            path = record[2:]
            changed.add(path)
            untracked.add(path)
        index += 1
    return tuple(sorted(changed)), tuple(sorted(untracked))


def git_status(worktree_root: Path) -> ToolResult[GitStatusData]:
    """Return machine-readable Git status without mutating the repository."""

    started = perf_counter()
    root, error_code = _resolve_repository(worktree_root)
    if root is None:
        return ToolResult.failure(
            error_code or "git_failed",
            "The requested workspace is not an accessible Git repository.",
            duration_ms=_elapsed_ms(started),
        )
    try:
        completed = _run_git(
            root,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
        )
    except FileNotFoundError:
        return ToolResult.failure(
            "tool_unavailable",
            "The Git executable is not available.",
            duration_ms=_elapsed_ms(started),
        )
    except (OSError, UnicodeError):
        return ToolResult.failure(
            "git_failed",
            "Git status could not be read.",
            duration_ms=_elapsed_ms(started),
        )
    if completed.returncode != 0:
        return ToolResult.failure(
            "git_failed",
            "Git status returned an error.",
            duration_ms=_elapsed_ms(started),
            metadata={"exit_code": completed.returncode},
        )

    changed_files, _ = _status_paths(completed.stdout)
    data = GitStatusData(
        clean=not changed_files,
        changed_files=changed_files,
        raw_porcelain_v2=completed.stdout,
    )
    return ToolResult.success(data, duration_ms=_elapsed_ms(started))


def git_diff(worktree_root: Path) -> ToolResult[GitDiffData]:
    """Return tracked and untracked working-tree changes without staging files."""

    started = perf_counter()
    root, error_code = _resolve_repository(worktree_root)
    if root is None:
        return ToolResult.failure(
            error_code or "git_failed",
            "The requested workspace is not an accessible Git repository.",
            duration_ms=_elapsed_ms(started),
        )

    try:
        status_process = _run_git(
            root,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
        )
        tracked_process = _run_git(root, "diff", "--no-ext-diff", "--binary", "--")
    except FileNotFoundError:
        return ToolResult.failure(
            "tool_unavailable",
            "The Git executable is not available.",
            duration_ms=_elapsed_ms(started),
        )
    except (OSError, UnicodeError):
        return ToolResult.failure(
            "git_failed",
            "Git diff could not be read.",
            duration_ms=_elapsed_ms(started),
        )
    if status_process.returncode != 0 or tracked_process.returncode != 0:
        return ToolResult.failure(
            "git_failed",
            "Git returned an error while building the diff.",
            duration_ms=_elapsed_ms(started),
        )

    changed_files, untracked_files = _status_paths(status_process.stdout)
    patch_parts = [tracked_process.stdout]
    for relative_path in untracked_files:
        try:
            resolve_workspace_path(root, relative_path)
            untracked_process = _run_git(
                root,
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                relative_path,
            )
        except WorkspaceBoundaryError:
            return ToolResult.failure(
                "workspace_boundary_violation",
                "An untracked path resolves outside the workspace.",
                duration_ms=_elapsed_ms(started),
            )
        except (FileNotFoundError, OSError, UnicodeError):
            return ToolResult.failure(
                "git_failed",
                "Git could not inspect an untracked file.",
                duration_ms=_elapsed_ms(started),
            )
        if untracked_process.returncode not in (0, 1):
            return ToolResult.failure(
                "git_failed",
                "Git returned an error while inspecting an untracked file.",
                duration_ms=_elapsed_ms(started),
                metadata={"exit_code": untracked_process.returncode},
            )
        patch_parts.append(untracked_process.stdout)

    data = GitDiffData(patch="".join(patch_parts), changed_files=changed_files)
    return ToolResult.success(data, duration_ms=_elapsed_ms(started))
