"""Deterministic policy evaluation for proposed test commands."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal


DEFAULT_ALLOWED_PREFIXES = (("pytest",), ("python", "-m", "pytest"))
DENIED_SYNTAX = ("&&", "|", ";", ">", "<", "`", "$(")


@dataclass(frozen=True, slots=True)
class CommandDecision:
    outcome: Literal["allow", "approval_required", "deny"]
    argv: tuple[str, ...]
    reason: str


def _has_prefix(argv: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return bool(prefix) and argv[: len(prefix)] == prefix


def evaluate_test_command(
    command: str,
    *,
    extra_allowed_prefixes: tuple[tuple[str, ...], ...] = (),
) -> CommandDecision:
    """Classify a command without executing it or invoking a shell."""

    if "\n" in command or "\r" in command or any(
        token in command for token in DENIED_SYNTAX
    ):
        return CommandDecision(
            outcome="deny",
            argv=(),
            reason="Shell syntax and multi-line commands are not permitted.",
        )
    try:
        argv = tuple(shlex.split(command, posix=True))
    except ValueError:
        return CommandDecision(
            outcome="deny",
            argv=(),
            reason="The command contains malformed quoting.",
        )
    if not argv:
        return CommandDecision(
            outcome="deny",
            argv=(),
            reason="The command is empty.",
        )

    allowed_prefixes = DEFAULT_ALLOWED_PREFIXES + extra_allowed_prefixes
    if any(_has_prefix(argv, prefix) for prefix in allowed_prefixes):
        return CommandDecision(
            outcome="allow",
            argv=argv,
            reason="The command matches an allowed test runner.",
        )
    return CommandDecision(
        outcome="approval_required",
        argv=argv,
        reason="The executable is not on the default test-runner allowlist.",
    )
