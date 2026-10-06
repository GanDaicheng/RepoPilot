from __future__ import annotations

from pathlib import Path

from repopilot.tools.patch import apply_patch


MODIFY_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

CREATE_PATCH = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+answer = 42
"""

DELETE_PATCH = """diff --git a/app.py b/app.py
deleted file mode 100644
--- a/app.py
+++ /dev/null
@@ -1 +0,0 @@
-value = 1
"""

TRAVERSAL_PATCH = """diff --git a/../escape.py b/../escape.py
new file mode 100644
--- /dev/null
+++ b/../escape.py
@@ -0,0 +1 @@
+escaped = True
"""

MULTIFILE_INVALID_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 9
diff --git a/../escape.py b/../escape.py
new file mode 100644
--- /dev/null
+++ b/../escape.py
@@ -0,0 +1 @@
+escaped = True
"""


def _snapshot(repo: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def test_apply_patch_modifies_existing_file(git_repo: Path) -> None:
    result = apply_patch(git_repo, MODIFY_PATCH)

    assert result.ok is True
    assert result.data is not None
    assert result.data.changed_files == ("app.py",)
    assert (git_repo / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_apply_patch_creates_new_file(git_repo: Path) -> None:
    result = apply_patch(git_repo, CREATE_PATCH)

    assert result.ok is True
    assert result.data is not None
    assert result.data.changed_files == ("new.py",)
    assert (git_repo / "new.py").read_text(encoding="utf-8") == "answer = 42\n"


def test_delete_patch_requires_approval(git_repo: Path) -> None:
    before = _snapshot(git_repo)

    result = apply_patch(git_repo, DELETE_PATCH)

    assert result.ok is False
    assert result.error_code == "approval_required"
    assert _snapshot(git_repo) == before


def test_approved_delete_patch_removes_file(git_repo: Path) -> None:
    result = apply_patch(git_repo, DELETE_PATCH, deletion_approved=True)

    assert result.ok is True
    assert result.data is not None
    assert result.data.deletes_files is True
    assert not (git_repo / "app.py").exists()


def test_patch_rejects_parent_traversal_before_write(git_repo: Path) -> None:
    before = _snapshot(git_repo)

    result = apply_patch(git_repo, TRAVERSAL_PATCH)

    assert result.ok is False
    assert result.error_code == "workspace_boundary_violation"
    assert _snapshot(git_repo) == before
    assert not (git_repo.parent / "escape.py").exists()


def test_malformed_patch_changes_nothing(git_repo: Path) -> None:
    before = _snapshot(git_repo)

    result = apply_patch(git_repo, "this is not a unified diff\n")

    assert result.ok is False
    assert result.error_code == "invalid_patch"
    assert _snapshot(git_repo) == before


def test_multifile_patch_is_atomic_when_one_target_is_invalid(git_repo: Path) -> None:
    before = _snapshot(git_repo)

    result = apply_patch(git_repo, MULTIFILE_INVALID_PATCH)

    assert result.ok is False
    assert result.error_code == "workspace_boundary_violation"
    assert _snapshot(git_repo) == before
    assert not (git_repo.parent / "escape.py").exists()

