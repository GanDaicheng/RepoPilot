from __future__ import annotations

import pytest

from repopilot.security.commands import evaluate_test_command


def test_allows_pytest_with_arguments() -> None:
    decision = evaluate_test_command('pytest -q "tests/unit tools"')

    assert decision.outcome == "allow"
    assert decision.argv == ("pytest", "-q", "tests/unit tools")
    assert isinstance(decision.argv, tuple)


def test_allows_python_module_pytest() -> None:
    decision = evaluate_test_command("python -m pytest tests -q")

    assert decision.outcome == "allow"
    assert decision.argv == ("python", "-m", "pytest", "tests", "-q")


def test_unknown_executable_requires_approval() -> None:
    decision = evaluate_test_command("ruff check src")

    assert decision.outcome == "approval_required"
    assert decision.argv == ("ruff", "check", "src")


def test_empty_command_is_denied() -> None:
    decision = evaluate_test_command("   ")

    assert decision.outcome == "deny"
    assert decision.argv == ()


def test_malformed_quoting_is_denied() -> None:
    decision = evaluate_test_command('pytest "unterminated')

    assert decision.outcome == "deny"
    assert decision.argv == ()


@pytest.mark.parametrize("token", ["&&", "|", ";", ">", "<", "`", "$("])
def test_shell_syntax_is_permanently_denied(token: str) -> None:
    decision = evaluate_test_command(
        f"pytest -q {token} echo unsafe",
        extra_allowed_prefixes=(("pytest",),),
    )

    assert decision.outcome == "deny"

