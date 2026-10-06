from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def init_repo(path: Path, files: Mapping[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "--quiet")
    for relative_path, content in files.items():
        target = path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    _run_git(path, "add", "--all")
    _run_git(
        path,
        "-c",
        "user.name=RepoPilotTest",
        "-c",
        "user.email=test@repopilot.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    return path

