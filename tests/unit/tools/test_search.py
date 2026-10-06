from __future__ import annotations

import subprocess
from pathlib import Path

from repopilot.tools import search
from repopilot.tools.search import search_code


def test_search_returns_structured_unicode_match(tmp_path: Path) -> None:
    (tmp_path / "unicode.py").write_bytes("前缀 needle 后缀\n".encode())

    result = search_code(tmp_path, "needle")

    assert result.ok is True
    assert result.data is not None
    assert len(result.data) == 1
    match = result.data[0]
    assert match.path == "unicode.py"
    assert match.line == 1
    assert match.column == 4
    assert match.text == "前缀 needle 后缀"


def test_search_treats_dash_prefixed_query_as_text(tmp_path: Path) -> None:
    (tmp_path / "options.txt").write_text("use --hidden here", encoding="utf-8")

    result = search_code(tmp_path, "--hidden")

    assert result.ok is True
    assert result.data is not None
    assert [match.path for match in result.data] == ["options.txt"]


def test_search_honors_file_glob_and_result_limit(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("target\n", encoding="utf-8")

    result = search_code(tmp_path, "target", file_glob="*.py", max_results=1)

    assert result.ok is True
    assert result.data is not None
    assert len(result.data) == 1
    assert result.data[0].path.endswith(".py")
    assert result.metadata["truncated"] is True


def test_search_returns_success_for_no_matches(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("present", encoding="utf-8")

    result = search_code(tmp_path, "absent")

    assert result.ok is True
    assert result.data == ()


def test_search_rejects_empty_query(tmp_path: Path) -> None:
    result = search_code(tmp_path, "")

    assert result.ok is False
    assert result.error_code == "invalid_query"


def test_search_reports_timeout(monkeypatch, tmp_path: Path) -> None:
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="rg", timeout=0.01)

    monkeypatch.setattr(search.subprocess, "run", raise_timeout)

    result = search_code(tmp_path, "needle", timeout_seconds=0.01)

    assert result.ok is False
    assert result.error_code == "search_timeout"


def test_search_reports_missing_rg(monkeypatch, tmp_path: Path) -> None:
    def raise_missing(*args, **kwargs):
        raise FileNotFoundError("rg is missing")

    monkeypatch.setattr(search.subprocess, "run", raise_missing)

    result = search_code(tmp_path, "needle")

    assert result.ok is False
    assert result.error_code == "tool_unavailable"

