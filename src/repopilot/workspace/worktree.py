"""Isolated Git worktree creation, snapshotting, and approved cleanup."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from repopilot.domain.results import ToolResult


TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
FULL_OBJECT_ID_RE = re.compile(r"(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class RepoSnapshot:
    head: str
    branch: str | None
    status_porcelain_v2: str
    index_sha256: str


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    task_id: str
    original_repo: Path
    base_commit: str
    branch: str
    path: Path
    original_snapshot: RepoSnapshot


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )


def _repository_root(repo_path: Path) -> tuple[Path | None, str | None]:
    try:
        requested = Path(repo_path).resolve(strict=True)
        completed = _run_git(requested, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        return None, "tool_unavailable"
    except (NotADirectoryError, OSError, UnicodeError, RuntimeError):
        return None, "not_git_repository"
    if completed.returncode != 0:
        return None, "not_git_repository"
    try:
        return Path(completed.stdout.strip()).resolve(strict=True), None
    except (OSError, RuntimeError):
        return None, "not_git_repository"


def resolve_repository_root(repo_path: Path) -> ToolResult[Path]:
    """Resolve an accessible path to its canonical Git top-level directory."""

    root, error_code = _repository_root(repo_path)
    if root is None:
        return ToolResult.failure(
            error_code or "not_git_repository",
            "The requested path is not an accessible Git repository.",
        )
    return ToolResult.success(root)


def capture_repo_snapshot(repo_path: Path) -> ToolResult[RepoSnapshot]:
    """Capture original checkout state needed to prove it was not changed."""

    started = perf_counter()
    repo, error_code = _repository_root(repo_path)
    if repo is None:
        return ToolResult.failure(
            error_code or "not_git_repository",
            "The requested path is not an accessible Git repository.",
            duration_ms=_elapsed_ms(started),
        )
    try:
        head = _run_git(repo, "rev-parse", "HEAD")
        branch = _run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        status = _run_git(
            repo,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
        )
        index_query = _run_git(repo, "rev-parse", "--git-path", "index")
    except FileNotFoundError:
        return ToolResult.failure(
            "tool_unavailable",
            "The Git executable is not available.",
            duration_ms=_elapsed_ms(started),
        )
    except (OSError, UnicodeError):
        return ToolResult.failure(
            "git_failed",
            "The repository snapshot could not be captured.",
            duration_ms=_elapsed_ms(started),
        )
    if (
        head.returncode != 0
        or status.returncode != 0
        or index_query.returncode != 0
        or branch.returncode not in (0, 1)
    ):
        return ToolResult.failure(
            "git_failed",
            "Git returned an error while capturing the repository snapshot.",
            duration_ms=_elapsed_ms(started),
        )

    index_path = Path(index_query.stdout.strip())
    if not index_path.is_absolute():
        index_path = repo / index_path
    try:
        index_digest = hashlib.sha256(index_path.read_bytes()).hexdigest()
    except OSError:
        return ToolResult.failure(
            "git_failed",
            "The repository index could not be hashed.",
            duration_ms=_elapsed_ms(started),
        )

    snapshot = RepoSnapshot(
        head=head.stdout.strip(),
        branch=branch.stdout.strip() if branch.returncode == 0 else None,
        status_porcelain_v2=status.stdout,
        index_sha256=index_digest,
    )
    return ToolResult.success(snapshot, duration_ms=_elapsed_ms(started))


def _registered_worktrees(repo: Path) -> ToolResult[dict[Path, dict[str, str]]]:
    started = perf_counter()
    try:
        completed = _run_git(repo, "worktree", "list", "--porcelain")
    except FileNotFoundError:
        return ToolResult.failure(
            "tool_unavailable",
            "The Git executable is not available.",
            duration_ms=_elapsed_ms(started),
        )
    except (OSError, UnicodeError):
        return ToolResult.failure(
            "git_failed",
            "Registered worktrees could not be inspected.",
            duration_ms=_elapsed_ms(started),
        )
    if completed.returncode != 0:
        return ToolResult.failure(
            "git_failed",
            "Git returned an error while listing worktrees.",
            duration_ms=_elapsed_ms(started),
        )

    worktrees: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in (*completed.stdout.splitlines(), ""):
        if not line:
            if "worktree" in current:
                worktrees[Path(current["worktree"]).resolve()] = dict(current)
            current.clear()
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return ToolResult.success(worktrees, duration_ms=_elapsed_ms(started))


class WorktreeManager:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def _expected_path(self, repo: Path, task_id: str) -> Path:
        repo_hash = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
        return (self.data_dir / "worktrees" / repo_hash / task_id).resolve()

    def create(
        self,
        repo_path: Path,
        task_id: str,
        *,
        base_commit: str | None = None,
    ) -> ToolResult[WorktreeInfo]:
        started = perf_counter()
        if TASK_ID_RE.fullmatch(task_id) is None:
            return ToolResult.failure(
                "invalid_task_id",
                "Task IDs must contain only letters, digits, underscores, and hyphens.",
                duration_ms=_elapsed_ms(started),
            )
        repo, error_code = _repository_root(repo_path)
        if repo is None:
            return ToolResult.failure(
                error_code or "not_git_repository",
                "The original repository is not accessible.",
                duration_ms=_elapsed_ms(started),
            )
        data_root = self.data_dir.resolve()
        if data_root == repo or data_root.is_relative_to(repo):
            return ToolResult.failure(
                "data_dir_inside_repository",
                "The RepoPilot data directory must be outside the original repository.",
                duration_ms=_elapsed_ms(started),
            )
        try:
            data_root.mkdir(parents=True, exist_ok=True)
            data_root = data_root.resolve(strict=True)
        except (OSError, RuntimeError):
            return ToolResult.failure(
                "workspace_boundary_violation",
                "The RepoPilot data directory cannot be resolved safely.",
                duration_ms=_elapsed_ms(started),
            )

        snapshot_result = capture_repo_snapshot(repo)
        if not snapshot_result.ok or snapshot_result.data is None:
            return ToolResult.failure(
                snapshot_result.error_code or "git_failed",
                snapshot_result.message,
                duration_ms=_elapsed_ms(started),
            )
        snapshot = snapshot_result.data
        target_base = snapshot.head
        if base_commit is not None:
            if FULL_OBJECT_ID_RE.fullmatch(base_commit) is None:
                return ToolResult.failure(
                    "invalid_base_commit",
                    "The base commit must be a full hexadecimal object ID.",
                    duration_ms=_elapsed_ms(started),
                )
            try:
                resolved = _run_git(repo, "rev-parse", "--verify", f"{base_commit}^{{commit}}")
            except (FileNotFoundError, OSError, UnicodeError):
                return ToolResult.failure(
                    "git_failed",
                    "The requested base commit could not be resolved.",
                    duration_ms=_elapsed_ms(started),
                )
            resolved_commit = resolved.stdout.strip()
            if (
                resolved.returncode != 0
                or FULL_OBJECT_ID_RE.fullmatch(resolved_commit) is None
                or resolved_commit.lower() != base_commit.lower()
            ):
                return ToolResult.failure(
                    "invalid_base_commit",
                    "The requested base is not an existing full commit object ID.",
                    duration_ms=_elapsed_ms(started),
                )
            target_base = resolved_commit
        branch = f"repopilot/{task_id}"
        expected_path = self._expected_path(repo, task_id)
        if not expected_path.is_relative_to(data_root):
            return ToolResult.failure(
                "workspace_boundary_violation",
                "The managed worktree path resolves outside the data directory.",
                duration_ms=_elapsed_ms(started),
            )
        registered_result = _registered_worktrees(repo)
        if not registered_result.ok or registered_result.data is None:
            return ToolResult.failure(
                registered_result.error_code or "git_failed",
                registered_result.message,
                duration_ms=_elapsed_ms(started),
            )
        registered = registered_result.data
        existing = registered.get(expected_path)
        if existing is not None:
            if (
                existing.get("HEAD") == target_base
                and existing.get("branch") == f"refs/heads/{branch}"
            ):
                info = WorktreeInfo(
                    task_id=task_id,
                    original_repo=repo,
                    base_commit=target_base,
                    branch=branch,
                    path=expected_path,
                    original_snapshot=snapshot,
                )
                return ToolResult.success(info, duration_ms=_elapsed_ms(started))
            return ToolResult.failure(
                "workspace_conflict",
                "The expected task path is registered to different Git state.",
                duration_ms=_elapsed_ms(started),
            )
        if expected_path.exists():
            return ToolResult.failure(
                "workspace_conflict",
                "The expected task path exists but is not a registered worktree.",
                duration_ms=_elapsed_ms(started),
            )
        try:
            branch_probe = _run_git(
                repo,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            )
        except (FileNotFoundError, OSError, UnicodeError):
            return ToolResult.failure(
                "git_failed",
                "The task branch could not be checked.",
                duration_ms=_elapsed_ms(started),
            )
        if branch_probe.returncode == 0:
            return ToolResult.failure(
                "workspace_conflict",
                "The task branch already exists without its managed worktree.",
                duration_ms=_elapsed_ms(started),
            )
        if branch_probe.returncode not in (0, 1):
            return ToolResult.failure(
                "git_failed",
                "Git returned an error while checking the task branch.",
                duration_ms=_elapsed_ms(started),
            )

        expected_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            created = _run_git(
                repo,
                "worktree",
                "add",
                "-b",
                branch,
                str(expected_path),
                target_base,
            )
        except (FileNotFoundError, OSError, UnicodeError):
            return ToolResult.failure(
                "git_failed",
                "The managed worktree could not be created.",
                duration_ms=_elapsed_ms(started),
            )
        if created.returncode != 0:
            return ToolResult.failure(
                "workspace_conflict",
                "Git rejected the managed worktree creation request.",
                duration_ms=_elapsed_ms(started),
                metadata={"exit_code": created.returncode},
            )
        info = WorktreeInfo(
            task_id=task_id,
            original_repo=repo,
            base_commit=target_base,
            branch=branch,
            path=expected_path,
            original_snapshot=snapshot,
        )
        return ToolResult.success(info, duration_ms=_elapsed_ms(started))

    def cleanup(
        self,
        info: WorktreeInfo,
        *,
        approved: bool = False,
    ) -> ToolResult[None]:
        started = perf_counter()
        if not approved:
            return ToolResult.failure(
                "approval_required",
                "Removing a managed worktree requires explicit approval.",
                duration_ms=_elapsed_ms(started),
            )
        if TASK_ID_RE.fullmatch(info.task_id) is None:
            return ToolResult.failure(
                "invalid_task_id",
                "The managed worktree carries an invalid task identity.",
                duration_ms=_elapsed_ms(started),
            )
        if info.branch != f"repopilot/{info.task_id}":
            return ToolResult.failure(
                "workspace_conflict",
                "The managed worktree branch does not match its task identity.",
                duration_ms=_elapsed_ms(started),
            )
        repo, error_code = _repository_root(info.original_repo)
        if repo is None:
            return ToolResult.failure(
                error_code or "not_git_repository",
                "The original repository is not accessible.",
                duration_ms=_elapsed_ms(started),
            )
        expected_path = self._expected_path(repo, info.task_id)
        try:
            actual_path = info.path.resolve(strict=True)
            data_root = self.data_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            return ToolResult.failure(
                "workspace_conflict",
                "The managed worktree path no longer resolves as expected.",
                duration_ms=_elapsed_ms(started),
            )
        if (
            actual_path != expected_path
            or not actual_path.is_relative_to(data_root)
            or info.original_repo.resolve() != repo
        ):
            return ToolResult.failure(
                "workspace_conflict",
                "Cleanup was refused for a path outside this manager's exact scope.",
                duration_ms=_elapsed_ms(started),
            )

        registered_result = _registered_worktrees(repo)
        if (
            not registered_result.ok
            or registered_result.data is None
            or actual_path not in registered_result.data
        ):
            return ToolResult.failure(
                "workspace_conflict",
                "The path is not a registered worktree of the original repository.",
                duration_ms=_elapsed_ms(started),
            )
        registered = registered_result.data[actual_path]
        if registered.get("branch") != f"refs/heads/{info.branch}":
            return ToolResult.failure(
                "workspace_conflict",
                "The registered worktree branch does not match the cleanup request.",
                duration_ms=_elapsed_ms(started),
            )
        try:
            removed = _run_git(repo, "worktree", "remove", "--force", str(actual_path))
        except (FileNotFoundError, OSError, UnicodeError):
            return ToolResult.failure(
                "git_failed",
                "The managed worktree could not be removed.",
                duration_ms=_elapsed_ms(started),
            )
        if removed.returncode != 0:
            return ToolResult.failure(
                "git_failed",
                "Git returned an error while removing the managed worktree.",
                duration_ms=_elapsed_ms(started),
                metadata={"exit_code": removed.returncode},
            )
        return ToolResult.success(None, duration_ms=_elapsed_ms(started))
