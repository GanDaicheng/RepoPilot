from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.agent.toolbox import ReadOnlyToolbox
from repopilot.models.types import ModelToolCall


def call(name: str, **arguments: object) -> ModelToolCall:
    return ModelToolCall(id=f"call-{name}", name=name, arguments=arguments)


def test_tool_definitions_expose_exact_read_only_allowlist() -> None:
    toolbox = ReadOnlyToolbox()

    assert [definition.name for definition in toolbox.definitions] == [
        "list_files",
        "search_code",
        "read_file",
        "git_status",
        "git_diff",
        "inspect_error",
    ]


def test_executes_all_six_read_only_tools_with_structured_bounded_data(
    git_repo: Path,
) -> None:
    (git_repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    toolbox = ReadOnlyToolbox()

    listed = toolbox.execute(git_repo, call("list_files", max_entries=10))
    searched = toolbox.execute(git_repo, call("search_code", query="value = 2"))
    read = toolbox.execute(git_repo, call("read_file", path="app.py"))
    status = toolbox.execute(git_repo, call("git_status"))
    diff = toolbox.execute(git_repo, call("git_diff"))
    inspected = toolbox.execute(
        git_repo,
        call(
            "inspect_error",
            output="FAILED tests/test_app.py::test_value\nE ValueError: bad\n",
        ),
    )

    assert listed.ok and listed.data == {"files": ["app.py"], "truncated": False}
    assert searched.ok and searched.data is not None
    assert searched.data["matches"][0]["path"] == "app.py"
    assert read.ok and read.data is not None and read.data["text"] == "value = 2\n"
    assert status.ok and status.data is not None and status.data["clean"] is False
    assert diff.ok and diff.data is not None and "value = 2" in diff.data["patch"]
    assert inspected.ok and inspected.data is not None
    assert inspected.data["failed_tests"] == ["tests/test_app.py::test_value"]


@pytest.mark.parametrize(
    "name",
    ["apply_patch", "run_tests", "cleanup_worktree", "shell", "unknown"],
)
def test_denies_every_non_allowlisted_tool_before_side_effect(
    git_repo: Path,
    name: str,
) -> None:
    before = (git_repo / "app.py").read_bytes()

    result = ReadOnlyToolbox().execute(
        git_repo,
        call(name, command="remove everything", patch_text="malicious"),
    )

    assert result.ok is False
    assert result.error_code == "tool_not_allowed"
    assert (git_repo / "app.py").read_bytes() == before


def test_rejects_attempt_to_replace_injected_worktree_root(git_repo: Path) -> None:
    result = ReadOnlyToolbox().execute(
        git_repo,
        call("read_file", worktree_root="C:/outside", path="app.py"),
    )

    assert result.ok is False
    assert result.error_code == "tool_arguments_invalid"


def test_forces_requested_limits_down_to_toolbox_caps(git_repo: Path) -> None:
    large = "x" * 250_000
    (git_repo / "large.txt").write_text(large, encoding="utf-8")

    result = ReadOnlyToolbox().execute(
        git_repo,
        call("read_file", path="large.txt", max_bytes=9_999_999),
    )

    assert result.ok is True
    assert result.data is not None
    assert len(result.data["text"]) == 200_000
    assert result.data["truncated"] is True
