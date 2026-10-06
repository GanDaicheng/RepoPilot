from __future__ import annotations

import pytest

from repopilot.security.secrets import find_secret_kind


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("use " + "sk-" + "1234567890abcdef1234567890abcdef", "api_key"),
        ("use " + "sk-" + "proj-1234567890abcdef", "api_key"),
        ("use " + "sk-" + "live_key-1234567890abcdef", "api_key"),
        (
            "Authorization: " + "Bearer " + "abcdefghijklmnopqrstuvwxyz.123",
            "bearer_token",
        ),
        ("-----" + "BEGIN " + "PRIVATE KEY" + "-----", "private_key"),
        ("-----" + "BEGIN RSA " + "PRIVATE KEY" + "-----", "private_key"),
        ("-----" + "BEGIN OPENSSH " + "PRIVATE KEY" + "-----", "private_key"),
    ],
)
def test_reports_secret_kind_without_returning_secret(
    value: str,
    expected: str,
) -> None:
    result = find_secret_kind(value)

    assert result == expected
    assert value not in result


@pytest.mark.parametrize(
    "value",
    [
        "Fix the parser for strings beginning with sk-.",
        "Document Bearer authentication without including a token.",
        "Reject text that says BEGIN PUBLIC KEY.",
        "Add a division-by-zero check and run pytest.",
    ],
)
def test_accepts_ordinary_repository_requirements(value: str) -> None:
    assert find_secret_kind(value) is None


def test_bounds_scanning_of_oversized_input() -> None:
    assert find_secret_kind("x" * 100_001) == "oversized_input"
