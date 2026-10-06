"""A closed, read-only adapter over RepoPilot repository inspection tools."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Mapping

from repopilot.domain.results import ToolResult
from repopilot.models.types import ModelToolCall, ToolDefinition
from repopilot.tools.filesystem import list_files, read_file
from repopilot.tools.git import git_diff, git_status
from repopilot.tools.search import search_code
from repopilot.tools.tests import inspect_error


MAX_READ_BYTES = 200_000
MAX_DIFF_CHARS = 200_000
MAX_STATUS_CHARS = 50_000


def _object_schema(
    properties: Mapping[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


class ReadOnlyToolbox:
    def __init__(self) -> None:
        integer = {"type": "integer", "minimum": 1}
        text = {"type": "string", "minLength": 1}
        self.definitions = (
            ToolDefinition(
                "list_files",
                "List repository files within fixed bounds.",
                _object_schema({"max_depth": integer, "max_entries": integer}),
            ),
            ToolDefinition(
                "search_code",
                "Search repository text using a fixed string.",
                _object_schema(
                    {"query": text, "file_glob": text, "max_results": integer},
                    required=("query",),
                ),
            ),
            ToolDefinition(
                "read_file",
                "Read a bounded range from one repository text file.",
                _object_schema(
                    {
                        "path": text,
                        "start_line": integer,
                        "end_line": integer,
                        "max_bytes": integer,
                    },
                    required=("path",),
                ),
            ),
            ToolDefinition("git_status", "Read Git worktree status.", _object_schema({})),
            ToolDefinition("git_diff", "Read the current Git diff.", _object_schema({})),
            ToolDefinition(
                "inspect_error",
                "Extract bounded failure details from test output.",
                _object_schema(
                    {"output": {"type": "string"}, "max_chars": integer, "max_items": integer},
                    required=("output",),
                ),
            ),
        )

    def execute(
        self,
        worktree_root: Path,
        call: ModelToolCall,
    ) -> ToolResult[Mapping[str, object]]:
        dispatch = {
            "list_files": self._list_files,
            "search_code": self._search_code,
            "read_file": self._read_file,
            "git_status": self._git_status,
            "git_diff": self._git_diff,
            "inspect_error": self._inspect_error,
        }
        handler = dispatch.get(call.name)
        if handler is None:
            return ToolResult.failure(
                "tool_not_allowed",
                "The requested tool is not in the read-only allowlist.",
            )
        try:
            return handler(Path(worktree_root), dict(call.arguments))
        except (TypeError, ValueError):
            return ToolResult.failure(
                "tool_arguments_invalid",
                "The tool arguments do not match the allowed schema.",
            )

    @staticmethod
    def _check(arguments: dict[str, object], allowed: set[str], required: set[str]) -> None:
        if not required <= arguments.keys() or not arguments.keys() <= allowed:
            raise ValueError("invalid arguments")

    def _list_files(self, root: Path, arguments: dict[str, object]) -> ToolResult:
        self._check(arguments, {"max_depth", "max_entries"}, set())
        max_depth = min(self._positive_int(arguments.get("max_depth", 4)), 8)
        max_entries = min(self._positive_int(arguments.get("max_entries", 500)), 500)
        result = list_files(root, max_depth=max_depth, max_entries=max_entries)
        if not result.ok:
            return result
        return ToolResult.success(
            {"files": list(result.data or ()), "truncated": bool(result.metadata.get("truncated"))},
            duration_ms=result.duration_ms,
        )

    def _search_code(self, root: Path, arguments: dict[str, object]) -> ToolResult:
        self._check(arguments, {"query", "file_glob", "max_results"}, {"query"})
        query = self._string(arguments["query"])
        file_glob = arguments.get("file_glob")
        if file_glob is not None:
            file_glob = self._string(file_glob)
        max_results = min(self._positive_int(arguments.get("max_results", 100)), 100)
        result = search_code(root, query, file_glob=file_glob, max_results=max_results)
        if not result.ok:
            return result
        return ToolResult.success(
            {
                "matches": [self._json_record(item) for item in result.data or ()],
                "truncated": bool(result.metadata.get("truncated")),
            },
            duration_ms=result.duration_ms,
        )

    def _read_file(self, root: Path, arguments: dict[str, object]) -> ToolResult:
        self._check(arguments, {"path", "start_line", "end_line", "max_bytes"}, {"path"})
        kwargs: dict[str, object] = {
            "max_bytes": min(self._positive_int(arguments.get("max_bytes", MAX_READ_BYTES)), MAX_READ_BYTES)
        }
        for name in ("start_line", "end_line"):
            if name in arguments:
                kwargs[name] = self._positive_int(arguments[name])
        result = read_file(root, self._string(arguments["path"]), **kwargs)
        normalized = self._record_result(result)
        if normalized.ok and normalized.data is not None:
            data = dict(normalized.data)
            data["text"] = str(data["text"]).replace("\r\n", "\n").replace("\r", "\n")
            return ToolResult.success(data, duration_ms=normalized.duration_ms)
        return normalized

    def _git_status(self, root: Path, arguments: dict[str, object]) -> ToolResult:
        self._check(arguments, set(), set())
        result = git_status(root)
        if not result.ok:
            return result
        data = self._json_record(result.data)
        raw = str(data["raw_porcelain_v2"])
        data["raw_porcelain_v2"] = raw[:MAX_STATUS_CHARS]
        data["truncated"] = len(raw) > MAX_STATUS_CHARS
        return ToolResult.success(data, duration_ms=result.duration_ms)

    def _git_diff(self, root: Path, arguments: dict[str, object]) -> ToolResult:
        self._check(arguments, set(), set())
        result = git_diff(root)
        if not result.ok:
            return result
        data = self._json_record(result.data)
        patch = str(data["patch"])
        data["patch"] = patch[:MAX_DIFF_CHARS]
        data["truncated"] = len(patch) > MAX_DIFF_CHARS
        return ToolResult.success(data, duration_ms=result.duration_ms)

    def _inspect_error(self, root: Path, arguments: dict[str, object]) -> ToolResult:
        del root
        self._check(arguments, {"output", "max_chars", "max_items"}, {"output"})
        output = arguments["output"]
        if not isinstance(output, str):
            raise ValueError("output must be a string")
        max_chars = min(self._positive_int(arguments.get("max_chars", 20_000)), 20_000)
        max_items = min(self._positive_int(arguments.get("max_items", 20)), 20)
        return self._record_result(inspect_error(output, max_chars=max_chars, max_items=max_items))

    @classmethod
    def _record_result(cls, result: ToolResult) -> ToolResult:
        if not result.ok:
            return result
        return ToolResult.success(cls._json_record(result.data), duration_ms=result.duration_ms)

    @staticmethod
    def _json_record(value: object) -> dict[str, object]:
        if not is_dataclass(value):
            raise TypeError("tool data must be a dataclass")
        data = asdict(value)
        return {key: list(item) if isinstance(item, tuple) else item for key, item in data.items()}

    @staticmethod
    def _positive_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expected a positive integer")
        return value

    @staticmethod
    def _string(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("expected a non-empty string")
        return value
