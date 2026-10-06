from __future__ import annotations

from pathlib import Path

from repopilot.tools.filesystem import list_files, read_file


def test_list_files_returns_sorted_posix_paths_and_ignores_git(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "nested" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / ".git" / "secret").write_text("hidden", encoding="utf-8")

    result = list_files(tmp_path)

    assert result.ok is True
    assert result.data == ("nested/a.py", "z.txt")


def test_list_files_stops_at_entry_limit(tmp_path: Path) -> None:
    for name in ("c.py", "a.py", "b.py"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    result = list_files(tmp_path, max_entries=2)

    assert result.ok is True
    assert result.data == ("a.py", "b.py")
    assert result.metadata["truncated"] is True


def test_read_file_returns_requested_one_based_line_range(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_bytes(b"one\ntwo\nthree\nfour\n")

    result = read_file(tmp_path, "sample.txt", start_line=2, end_line=3)

    assert result.ok is True
    assert result.data is not None
    assert result.data.path == "sample.txt"
    assert result.data.text == "two\nthree\n"
    assert result.data.start_line == 2
    assert result.data.end_line == 3
    assert result.data.truncated is False


def test_read_file_marks_truncated_content(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("abcdefgh", encoding="utf-8")

    result = read_file(tmp_path, "large.txt", max_bytes=5)

    assert result.ok is True
    assert result.data is not None
    assert result.data.text == "abcde"
    assert result.data.truncated is True


def test_read_file_rejects_binary_content(tmp_path: Path) -> None:
    (tmp_path / "image.bin").write_bytes(b"header\x00payload")

    result = read_file(tmp_path, "image.bin")

    assert result.ok is False
    assert result.error_code == "binary_file"


def test_file_tools_report_boundary_violation(tmp_path: Path) -> None:
    result = read_file(tmp_path, "../secret.txt")

    assert result.ok is False
    assert result.error_code == "workspace_boundary_violation"
