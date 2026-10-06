"""Validated, approval-aware unified patch inspection and application."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from repopilot.domain.results import ToolResult
from repopilot.security.paths import WorkspaceBoundaryError, resolve_workspace_path


@dataclass(frozen=True, slots=True)
class PatchInspection:
    paths: tuple[str, ...]
    deletes_files: bool


@dataclass(frozen=True, slots=True)
class PatchApplyData:
    changed_files: tuple[str, ...]
    deletes_files: bool


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _normalize_patch_path(raw_path: str) -> str:
    if raw_path in ("/dev/null", "dev/null"):
        return "/dev/null"
    if raw_path.startswith(("a/", "b/")):
        return raw_path[2:]
    return raw_path


def inspect_patch(
    worktree_root: Path,
    patch_text: str,
    *,
    max_bytes: int = 1_000_000,
) -> ToolResult[PatchInspection]:
    """Parse and validate every path in a unified diff before any write."""

    started = perf_counter()
    if max_bytes < 1 or not patch_text or len(patch_text.encode("utf-8")) > max_bytes:
        return ToolResult.failure(
            "invalid_patch",
            "The patch is empty or exceeds the configured size limit.",
            duration_ms=_elapsed_ms(started),
        )
    try:
        root = resolve_workspace_path(Path(worktree_root), ".")
        patch_set = PatchSet(patch_text.splitlines(keepends=True))
    except (UnidiffParseError, ValueError, TypeError):
        return ToolResult.failure(
            "invalid_patch",
            "The input is not a valid unified diff.",
            duration_ms=_elapsed_ms(started),
        )
    except (WorkspaceBoundaryError, FileNotFoundError):
        return ToolResult.failure(
            "workspace_boundary_violation",
            "The patch workspace cannot be resolved safely.",
            duration_ms=_elapsed_ms(started),
        )

    if not patch_set or any(len(patched_file) == 0 for patched_file in patch_set):
        return ToolResult.failure(
            "invalid_patch",
            "The patch must contain at least one file hunk.",
            duration_ms=_elapsed_ms(started),
        )

    changed_paths: set[str] = set()
    deletes_files = False
    try:
        for patched_file in patch_set:
            source = _normalize_patch_path(patched_file.source_file)
            target = _normalize_patch_path(patched_file.target_file)
            deletes_files = deletes_files or target == "/dev/null"
            for candidate in (source, target):
                if candidate == "/dev/null":
                    continue
                resolve_workspace_path(root, candidate, must_exist=False)
            changed_paths.add(target if target != "/dev/null" else source)
    except (WorkspaceBoundaryError, FileNotFoundError, OSError, RuntimeError):
        return ToolResult.failure(
            "workspace_boundary_violation",
            "A patch path resolves outside the workspace.",
            duration_ms=_elapsed_ms(started),
        )

    inspection = PatchInspection(
        paths=tuple(sorted(changed_paths)),
        deletes_files=deletes_files,
    )
    return ToolResult.success(inspection, duration_ms=_elapsed_ms(started))


def _run_git_apply(
    root: Path,
    patch_text: str,
    *,
    check_only: bool,
) -> subprocess.CompletedProcess[bytes]:
    command = ["git", "apply"]
    if check_only:
        command.extend(["--check", "--whitespace=error-all"])
    else:
        command.append("--whitespace=nowarn")
    command.append("-")
    return subprocess.run(
        command,
        cwd=root,
        shell=False,
        input=patch_text.encode("utf-8"),
        capture_output=True,
        check=False,
    )


def apply_patch(
    worktree_root: Path,
    patch_text: str,
    *,
    deletion_approved: bool = False,
    max_bytes: int = 1_000_000,
) -> ToolResult[PatchApplyData]:
    """Check and atomically apply a validated patch to the worktree."""

    started = perf_counter()
    inspection_result = inspect_patch(
        worktree_root,
        patch_text,
        max_bytes=max_bytes,
    )
    if not inspection_result.ok or inspection_result.data is None:
        return ToolResult.failure(
            inspection_result.error_code or "invalid_patch",
            inspection_result.message,
            duration_ms=_elapsed_ms(started),
            metadata=inspection_result.metadata,
        )
    inspection = inspection_result.data
    if inspection.deletes_files and not deletion_approved:
        return ToolResult.failure(
            "approval_required",
            "Deleting files requires explicit approval.",
            duration_ms=_elapsed_ms(started),
            metadata={"paths": inspection.paths},
        )

    try:
        root = resolve_workspace_path(Path(worktree_root), ".")
        checked = _run_git_apply(root, patch_text, check_only=True)
    except FileNotFoundError:
        return ToolResult.failure(
            "tool_unavailable",
            "The Git executable is not available.",
            duration_ms=_elapsed_ms(started),
        )
    except (WorkspaceBoundaryError, OSError, UnicodeError, ValueError):
        return ToolResult.failure(
            "patch_check_failed",
            "The patch could not be checked safely.",
            duration_ms=_elapsed_ms(started),
        )
    if checked.returncode != 0:
        return ToolResult.failure(
            "patch_check_failed",
            "Git rejected the patch during validation.",
            duration_ms=_elapsed_ms(started),
            metadata={"exit_code": checked.returncode},
        )

    try:
        applied = _run_git_apply(root, patch_text, check_only=False)
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return ToolResult.failure(
            "patch_apply_failed",
            "The validated patch could not be applied.",
            duration_ms=_elapsed_ms(started),
        )
    if applied.returncode != 0:
        return ToolResult.failure(
            "patch_apply_failed",
            "Git failed to apply the validated patch.",
            duration_ms=_elapsed_ms(started),
            metadata={"exit_code": applied.returncode},
        )

    data = PatchApplyData(
        changed_files=inspection.paths,
        deletes_files=inspection.deletes_files,
    )
    return ToolResult.success(data, duration_ms=_elapsed_ms(started))
