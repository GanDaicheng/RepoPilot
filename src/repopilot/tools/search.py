"""Bounded fixed-string code search backed by ripgrep JSON output."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from repopilot.domain.results import ToolResult
from repopilot.security.paths import WorkspaceBoundaryError, resolve_workspace_path


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line: int
    column: int
    text: str


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _character_column(line: str, byte_offset: int) -> int:
    prefix = line.encode("utf-8")[:byte_offset]
    return len(prefix.decode("utf-8")) + 1


def search_code(
    worktree_root: Path,
    query: str,
    *,
    file_glob: str | None = None,
    max_results: int = 100,
    timeout_seconds: float = 10.0,
    rg_executable: str = "rg",
) -> ToolResult[tuple[SearchMatch, ...]]:
    """Search repository text using a fixed, non-shell ripgrep invocation."""

    started = perf_counter()
    if not query or "\x00" in query:
        return ToolResult.failure(
            "invalid_query",
            "The search query must be non-empty and contain no NUL bytes.",
            duration_ms=_elapsed_ms(started),
        )
    if max_results < 1 or timeout_seconds <= 0:
        return ToolResult.failure(
            "invalid_limit",
            "max_results and timeout_seconds must both be positive.",
            duration_ms=_elapsed_ms(started),
        )

    try:
        root = resolve_workspace_path(Path(worktree_root), ".")
    except (WorkspaceBoundaryError, FileNotFoundError):
        return ToolResult.failure(
            "workspace_boundary_violation",
            "The search workspace cannot be resolved safely.",
            duration_ms=_elapsed_ms(started),
        )

    command = [
        rg_executable,
        "--json",
        "--line-number",
        "--column",
        "--fixed-strings",
    ]
    if file_glob is not None:
        command.extend(["--glob", file_glob])
    command.extend(["--", query, "."])

    try:
        completed = subprocess.run(
            command,
            cwd=root,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult.failure(
            "search_timeout",
            "The code search exceeded its time limit.",
            duration_ms=_elapsed_ms(started),
        )
    except FileNotFoundError:
        return ToolResult.failure(
            "tool_unavailable",
            "The ripgrep executable is not available.",
            duration_ms=_elapsed_ms(started),
        )
    except (OSError, UnicodeError, ValueError):
        return ToolResult.failure(
            "search_failed",
            "The code search could not be executed.",
            duration_ms=_elapsed_ms(started),
        )

    if completed.returncode == 1:
        return ToolResult.success((), duration_ms=_elapsed_ms(started))
    if completed.returncode != 0:
        return ToolResult.failure(
            "search_failed",
            "Ripgrep reported a search error.",
            duration_ms=_elapsed_ms(started),
            metadata={"exit_code": completed.returncode},
        )

    matches: list[SearchMatch] = []
    truncated = False
    try:
        for raw_event in completed.stdout.splitlines():
            event = json.loads(raw_event)
            if event.get("type") != "match":
                continue
            data = event["data"]
            path_text = data["path"]["text"]
            line_text = data["lines"]["text"]
            line_number = int(data["line_number"])
            for submatch in data["submatches"]:
                if len(matches) == max_results:
                    truncated = True
                    break
                normalized_path = Path(path_text).as_posix()
                if normalized_path.startswith("./"):
                    normalized_path = normalized_path[2:]
                matches.append(
                    SearchMatch(
                        path=normalized_path,
                        line=line_number,
                        column=_character_column(line_text, int(submatch["start"])),
                        text=line_text.rstrip("\r\n"),
                    )
                )
            if truncated:
                break
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return ToolResult.failure(
            "search_failed",
            "Ripgrep returned malformed search output.",
            duration_ms=_elapsed_ms(started),
        )

    return ToolResult.success(
        tuple(matches),
        duration_ms=_elapsed_ms(started),
        metadata={"truncated": truncated},
    )
