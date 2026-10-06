from __future__ import annotations

import subprocess
from pathlib import Path

from repopilot.domain.results import ToolResult
from repopilot.tools import tests as tests_module
from repopilot.tools.tests import DockerAvailability, DockerTestRunner


def _available(runner: DockerTestRunner, monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "is_available",
        lambda: ToolResult.success(DockerAvailability("29.0", "29.0")),
    )


def test_runner_uses_all_required_hardening_flags(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = DockerTestRunner()
    _available(runner, monkeypatch)
    captured: list[str] = []

    def fake_run(argv, **kwargs):
        captured.extend(argv)
        return subprocess.CompletedProcess(argv, 0, "passed", "")

    monkeypatch.setattr(tests_module.subprocess, "run", fake_run)

    result = runner.run(tmp_path, "pytest -q")

    assert result.ok is True
    for flag, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pids-limit", "256"),
        ("--memory", "1g"),
        ("--cpus", "2.0"),
        ("--user", "10001:10001"),
    ):
        index = captured.index(flag)
        assert captured[index + 1] == value
    assert "--read-only" in captured
    assert "--tmpfs" in captured
    assert captured.count("--mount") == 1
    assert "target=/workspace" in captured[captured.index("--mount") + 1]
    assert "-e" not in captured
    assert "--env" not in captured


def test_runner_passes_test_as_argv_not_shell(monkeypatch, tmp_path: Path) -> None:
    runner = DockerTestRunner()
    _available(runner, monkeypatch)
    captured: list[str] = []

    def fake_run(argv, **kwargs):
        captured.extend(argv)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tests_module.subprocess, "run", fake_run)

    result = runner.run(tmp_path, 'pytest -q "tests/unit tools"')

    assert result.ok is True
    assert captured[-3:] == ["pytest", "-q", "tests/unit tools"]


def test_runner_rejects_denied_command_before_docker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tests_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Docker called")),
    )

    result = DockerTestRunner().run(
        tmp_path,
        "pytest -q && echo unsafe",
        command_approved=True,
    )

    assert result.ok is False
    assert result.error_code == "command_denied"


def test_runner_returns_approval_required_for_unknown_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tests_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Docker called")),
    )

    result = DockerTestRunner().run(tmp_path, "ruff check src")

    assert result.ok is False
    assert result.error_code == "approval_required"


def test_runner_distinguishes_test_failure_from_docker_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = DockerTestRunner()
    _available(runner, monkeypatch)
    responses = iter(
        (
            subprocess.CompletedProcess([], 1, "one failed", ""),
            subprocess.CompletedProcess([], 125, "", "daemon error"),
        )
    )
    monkeypatch.setattr(
        tests_module.subprocess,
        "run",
        lambda *args, **kwargs: next(responses),
    )

    test_failure = runner.run(tmp_path, "pytest -q")
    docker_failure = runner.run(tmp_path, "pytest -q")

    assert test_failure.ok is True
    assert test_failure.data is not None
    assert test_failure.data.exit_code == 1
    assert docker_failure.ok is False
    assert docker_failure.error_code == "docker_run_failed"


def test_runner_maps_timeout_to_test_timeout(monkeypatch, tmp_path: Path) -> None:
    runner = DockerTestRunner()
    _available(runner, monkeypatch)

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker run", timeout=1)

    monkeypatch.setattr(tests_module.subprocess, "run", raise_timeout)

    result = runner.run(tmp_path, "pytest -q")

    assert result.ok is False
    assert result.error_code == "test_timeout"


def test_runner_bounds_multibyte_output_to_one_mebibyte(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = DockerTestRunner()
    _available(runner, monkeypatch)
    oversized = "你" * 400_000
    monkeypatch.setattr(
        tests_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, oversized, ""),
    )

    result = runner.run(tmp_path, "pytest -q")

    assert result.ok is True
    assert result.data is not None
    assert len(result.data.stdout.encode("utf-8")) <= 1_048_576
    assert result.metadata["stdout_truncated"] is True


def test_unavailable_daemon_returns_docker_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        tests_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            1,
            "29.0\t",
            "Cannot connect to the Docker daemon",
        ),
    )

    result = DockerTestRunner().is_available()

    assert result.ok is False
    assert result.error_code == "docker_unavailable"


def test_available_daemon_accepts_docker_aligned_version_output(monkeypatch) -> None:
    monkeypatch.setattr(
        tests_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            "29.8.0              29.8.0\n",
            "",
        ),
    )

    result = DockerTestRunner().is_available()

    assert result.ok is True
    assert result.data == DockerAvailability("29.8.0", "29.8.0")
