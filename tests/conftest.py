from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.git import init_repo


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo", {"app.py": "value = 1\n"})

