from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.tools.tests import DockerTestRunner


@pytest.mark.integration
@pytest.mark.docker
def test_real_container_runs_fixture_tests_without_network(tmp_path: Path) -> None:
    runner = DockerTestRunner()
    availability = runner.is_available()
    if not availability.ok and availability.error_code == "docker_unavailable":
        pytest.skip(f"Docker daemon unavailable: {availability.message}")
    assert availability.ok, availability.message

    fixture = tmp_path / "test_fixture.py"
    fixture.write_text("def test_value():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    passing = runner.run(tmp_path, "pytest -q")

    assert passing.ok is True
    assert passing.data is not None
    assert passing.data.exit_code == 0

    fixture.write_text("def test_value():\n    assert 2 + 2 == 5\n", encoding="utf-8")
    failing = runner.run(tmp_path, "pytest -q")

    assert failing.ok is True
    assert failing.data is not None
    assert failing.data.exit_code != 0

