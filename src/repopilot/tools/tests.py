"""Deterministic extraction of useful details from test output."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from repopilot.domain.results import ToolResult
from repopilot.security.commands import evaluate_test_command
from repopilot.security.paths import WorkspaceBoundaryError, resolve_workspace_path


FAILED_RE = re.compile(r"^FAILED\s+(\S+)")
EXCEPTION_RE = re.compile(r"^\s*E\s+([A-Za-z_][A-Za-z0-9_.]*)\s*:")
LOCATION_RE = re.compile(r"((?:[A-Za-z]:)?[^:\r\n]*?\.py:\d+)")


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    failed_tests: tuple[str, ...]
    exception_types: tuple[str, ...]
    locations: tuple[str, ...]
    tail: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class DockerRunnerConfig:
    image: str = "repopilot-runner:m1"
    timeout_seconds: int = 300
    memory: str = "1g"
    cpus: float = 2.0
    pids_limit: int = 256
    tmpfs_size: str = "128m"


@dataclass(frozen=True, slots=True)
class DockerAvailability:
    client_version: str
    server_version: str


@dataclass(frozen=True, slots=True)
class TestRunResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


MAX_CAPTURE_BYTES = 1_048_576


def _bounded_tail(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_CAPTURE_BYTES:
        return value, False
    return encoded[-MAX_CAPTURE_BYTES:].decode("utf-8", errors="ignore"), True


class DockerTestRunner:
    """Execute an approved test argv inside a hardened Docker container."""

    def is_available(self) -> ToolResult[DockerAvailability]:
        started = perf_counter()
        try:
            completed = subprocess.run(
                [
                    "docker",
                    "version",
                    "--format",
                    "{{.Client.Version}}\t{{.Server.Version}}",
                ],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            return ToolResult.failure(
                "tool_unavailable",
                "The Docker client is not installed.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        except (subprocess.TimeoutExpired, OSError):
            return ToolResult.failure(
                "docker_unavailable",
                "Docker availability could not be confirmed.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )

        parts = completed.stdout.strip().split(maxsplit=1)
        if completed.returncode != 0 or len(parts) != 2 or not all(parts):
            return ToolResult.failure(
                "docker_unavailable",
                "The Docker daemon is unavailable.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
                metadata={"exit_code": completed.returncode},
            )
        availability = DockerAvailability(
            client_version=parts[0],
            server_version=parts[1],
        )
        return ToolResult.success(
            availability,
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
        )

    def run(
        self,
        worktree_root: Path,
        test_command: str,
        *,
        config: DockerRunnerConfig = DockerRunnerConfig(),
        command_approved: bool = False,
    ) -> ToolResult[TestRunResult]:
        started = perf_counter()
        decision = evaluate_test_command(test_command)
        if decision.outcome == "deny":
            return ToolResult.failure(
                "command_denied",
                decision.reason,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        if decision.outcome == "approval_required" and not command_approved:
            return ToolResult.failure(
                "approval_required",
                decision.reason,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
                metadata={"argv": decision.argv},
            )
        if (
            not config.image
            or config.timeout_seconds < 1
            or config.cpus <= 0
            or config.pids_limit < 1
            or not config.memory
            or not config.tmpfs_size
        ):
            return ToolResult.failure(
                "invalid_config",
                "Docker runner limits must all be positive and non-empty.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )

        try:
            root = resolve_workspace_path(Path(worktree_root), ".")
        except (WorkspaceBoundaryError, FileNotFoundError):
            return ToolResult.failure(
                "workspace_boundary_violation",
                "The test workspace cannot be resolved safely.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )

        availability = self.is_available()
        if not availability.ok:
            return ToolResult.failure(
                availability.error_code or "docker_unavailable",
                availability.message,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
                metadata=availability.metadata,
            )

        mount = f"type=bind,source={root},target=/workspace"
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(config.pids_limit),
            "--memory",
            config.memory,
            "--cpus",
            str(config.cpus),
            "--user",
            "10001:10001",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={config.tmpfs_size}",
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            config.image,
            *decision.argv,
        ]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(
                "test_timeout",
                "The test container exceeded its wall-clock timeout.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
                metadata={"timeout_seconds": config.timeout_seconds},
            )
        except FileNotFoundError:
            return ToolResult.failure(
                "tool_unavailable",
                "The Docker client is not available.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        except OSError:
            return ToolResult.failure(
                "docker_run_failed",
                "The Docker process could not be started.",
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )

        stdout, stdout_truncated = _bounded_tail(completed.stdout)
        stderr, stderr_truncated = _bounded_tail(completed.stderr)
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        if completed.returncode in (125, 126, 127):
            return ToolResult.failure(
                "docker_run_failed",
                "Docker could not start or execute the test container.",
                duration_ms=duration_ms,
                metadata={
                    "exit_code": completed.returncode,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                },
            )

        data = TestRunResult(
            argv=decision.argv,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            duration_ms=duration_ms,
        )
        return ToolResult.success(
            data,
            duration_ms=duration_ms,
            metadata={
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
        )


def _append_unique(items: list[str], value: str, limit: int) -> None:
    if value not in items and len(items) < limit:
        items.append(value)


def inspect_error(
    output: str,
    *,
    max_chars: int = 20_000,
    max_items: int = 20,
) -> ToolResult[ErrorSummary]:
    """Extract bounded pytest failures, exception types, locations, and tail."""

    started = perf_counter()
    if max_chars < 1 or max_items < 1:
        return ToolResult.failure(
            "invalid_limit",
            "max_chars and max_items must both be positive.",
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
        )

    failed_tests: list[str] = []
    exception_types: list[str] = []
    locations: list[str] = []
    for raw_line in output.splitlines():
        failed_match = FAILED_RE.match(raw_line)
        if failed_match:
            _append_unique(failed_tests, failed_match.group(1), max_items)
        exception_match = EXCEPTION_RE.match(raw_line)
        if exception_match:
            _append_unique(exception_types, exception_match.group(1), max_items)
        location_match = LOCATION_RE.search(raw_line.strip())
        if location_match:
            _append_unique(locations, location_match.group(1), max_items)

    summary = ErrorSummary(
        failed_tests=tuple(failed_tests),
        exception_types=tuple(exception_types),
        locations=tuple(locations),
        tail=output[-max_chars:],
        truncated=len(output) > max_chars,
    )
    duration_ms = max(0, int((perf_counter() - started) * 1000))
    return ToolResult.success(summary, duration_ms=duration_ms)
