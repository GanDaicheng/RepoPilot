"""Bounded, worktree-safe filesystem inspection tools."""

from __future__ import annotations

import codecs
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import AbstractSet

from repopilot.domain.results import ToolResult
from repopilot.security.paths import WorkspaceBoundaryError, resolve_workspace_path


DEFAULT_IGNORED_NAMES = frozenset({".git", ".venv", "__pycache__", "node_modules"})


@dataclass(frozen=True, slots=True)
class FileContent:
    path: str
    text: str
    start_line: int
    end_line: int
    truncated: bool


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _walk_files(
    root: Path,
    current: Path,
    *,
    depth: int,
    max_depth: int,
    ignored_names: AbstractSet[str],
) -> Iterator[str]:
    if depth >= max_depth:
        return
    for entry in sorted(current.iterdir(), key=lambda path: path.name):
        if entry.name in ignored_names:
            continue
        safe_entry = resolve_workspace_path(root, entry.relative_to(root))
        if safe_entry.is_dir():
            yield from _walk_files(
                root,
                safe_entry,
                depth=depth + 1,
                max_depth=max_depth,
                ignored_names=ignored_names,
            )
        elif safe_entry.is_file():
            yield entry.relative_to(root).as_posix()


def list_files(
    worktree_root: Path,
    *,
    max_depth: int = 4,
    max_entries: int = 500,
    ignored_names: AbstractSet[str] = DEFAULT_IGNORED_NAMES,
) -> ToolResult[tuple[str, ...]]:
    """List a deterministic, bounded set of files below a worktree root."""

    started = perf_counter()
    if max_depth < 1 or max_entries < 1:
        return ToolResult.failure(
            "invalid_limit",
            "max_depth and max_entries must both be positive.",
            duration_ms=_elapsed_ms(started),
        )
    try:
        root = resolve_workspace_path(Path(worktree_root), ".")
        iterator = _walk_files(
            root,
            root,
            depth=0,
            max_depth=max_depth,
            ignored_names=ignored_names,
        )
        files: list[str] = []
        truncated = False
        for path in iterator:
            if len(files) == max_entries:
                truncated = True
                break
            files.append(path)
    except WorkspaceBoundaryError:
        return ToolResult.failure(
            "workspace_boundary_violation",
            "A filesystem entry resolves outside the workspace.",
            duration_ms=_elapsed_ms(started),
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return ToolResult.failure(
            "filesystem_error",
            "The workspace files could not be listed.",
            duration_ms=_elapsed_ms(started),
        )

    return ToolResult.success(
        tuple(files),
        duration_ms=_elapsed_ms(started),
        metadata={"truncated": truncated},
    )


def read_file(
    worktree_root: Path,
    relative_path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    max_bytes: int = 1_000_000,
) -> ToolResult[FileContent]:
    """Read a UTF-8 text file without leaving the worktree or exceeding bounds."""

    started = perf_counter()
    first_line = 1 if start_line is None else start_line
    if (
        first_line < 1
        or (end_line is not None and end_line < first_line)
        or max_bytes < 1
    ):
        return ToolResult.failure(
            "invalid_range",
            "Line numbers and max_bytes must define a positive range.",
            duration_ms=_elapsed_ms(started),
        )

    try:
        path = resolve_workspace_path(Path(worktree_root), relative_path)
    except WorkspaceBoundaryError:
        return ToolResult.failure(
            "workspace_boundary_violation",
            "The requested file is outside the workspace.",
            duration_ms=_elapsed_ms(started),
        )
    except FileNotFoundError:
        return ToolResult.failure(
            "file_not_found",
            "The requested file does not exist.",
            duration_ms=_elapsed_ms(started),
        )

    if not path.is_file():
        return ToolResult.failure(
            "not_a_file",
            "The requested path is not a regular file.",
            duration_ms=_elapsed_ms(started),
        )

    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except (PermissionError, OSError):
        return ToolResult.failure(
            "filesystem_error",
            "The requested file could not be read.",
            duration_ms=_elapsed_ms(started),
        )

    if b"\x00" in raw:
        return ToolResult.failure(
            "binary_file",
            "The requested file contains binary data.",
            duration_ms=_elapsed_ms(started),
        )

    byte_truncated = len(raw) > max_bytes
    payload = raw[:max_bytes]
    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        text = decoder.decode(payload, final=not byte_truncated)
    except UnicodeDecodeError:
        return ToolResult.failure(
            "invalid_utf8",
            "The requested file is not valid UTF-8 text.",
            duration_ms=_elapsed_ms(started),
        )

    lines = text.splitlines(keepends=True)
    requested_end = len(lines) if end_line is None else min(end_line, len(lines))
    selected = lines[first_line - 1 : requested_end]
    actual_end = first_line + len(selected) - 1
    content = FileContent(
        path=Path(relative_path).as_posix(),
        text="".join(selected),
        start_line=first_line,
        end_line=actual_end,
        truncated=byte_truncated,
    )
    return ToolResult.success(content, duration_ms=_elapsed_ms(started))
