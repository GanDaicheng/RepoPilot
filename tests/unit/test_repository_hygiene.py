from pathlib import Path


def test_local_secret_and_runtime_files_are_gitignored() -> None:
    root = Path(__file__).parents[2]
    rules = (root / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in rules
    assert ".env.*" in rules
    assert "!.env.example" in rules
    assert ".repopilot-data/" in rules
