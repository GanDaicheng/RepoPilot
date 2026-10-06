from __future__ import annotations

import pytest

from repopilot.domain.results import ToolResult


def test_success_result_contains_data_and_timing() -> None:
    result = ToolResult.success(
        {"changed": 2},
        message="completed",
        duration_ms=17,
        metadata={"tool": "filesystem"},
    )

    assert result.ok is True
    assert result.data == {"changed": 2}
    assert result.error_code is None
    assert result.message == "completed"
    assert result.duration_ms == 17
    assert result.metadata == {"tool": "filesystem"}


def test_failure_result_contains_stable_error_code() -> None:
    result = ToolResult.failure(
        "PATH_OUTSIDE_WORKSPACE",
        "The requested path is outside the workspace.",
        duration_ms=3,
    )

    assert result.ok is False
    assert result.data is None
    assert result.error_code == "PATH_OUTSIDE_WORKSPACE"
    assert result.message == "The requested path is outside the workspace."
    assert result.duration_ms == 3


def test_result_metadata_is_read_only_mapping() -> None:
    source = {"attempt": 1}
    success = ToolResult.success("ok", metadata=source)
    failure = ToolResult.failure("FAILED", "failed", metadata=source)
    source["attempt"] = 2

    assert success.metadata["attempt"] == 1
    assert failure.metadata["attempt"] == 1
    with pytest.raises(TypeError):
        success.metadata["attempt"] = 3  # type: ignore[index]
    with pytest.raises(TypeError):
        failure.metadata["attempt"] = 3  # type: ignore[index]

