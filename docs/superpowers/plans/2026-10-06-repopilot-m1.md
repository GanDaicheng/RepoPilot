# RepoPilot M1 Safe Tooling and Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify RepoPilot's eight safe repository tools, Git Worktree isolation, command policy, and Docker-based test runner without introducing any model or orchestration code.

**Architecture:** M1 exposes small, typed Python services that all accept an explicit Worktree root and return one shared `ToolResult` envelope. Filesystem and command policy are checked before subprocesses run; Git and ripgrep commands are fixed argument arrays; Docker is the only test execution backend. Later LangGraph nodes and MCP adapters will call these services rather than reimplement them.

**Tech Stack:** Python 3.12 baseline, standard library, `unidiff`, pytest, Git CLI, ripgrep, Docker Engine

**Spec:** `docs/superpowers/specs/2026-10-06-repopilot-design.md`

## Global Constraints

- Runtime baseline is Python 3.12; the current Python 3.14 host may run tests but is not a claimed compatibility target.
- Product code uses a `src/repopilot` package layout and type annotations on every public interface.
- No repository tool may call `shell=True` or construct a command string for a shell.
- No tool may write outside its explicit Worktree root or mutate the original repository checkout.
- `git push`, target-repository commits, destructive Git resets, privileged containers, Docker Socket mounts, and host-root mounts are permanently forbidden.
- Full test logs are returned as bounded output in M1; later milestones persist the unabridged logs as artifacts.
- Docker tests run as a non-root user with network disabled, a read-only root filesystem, dropped capabilities, `no-new-privileges`, CPU/memory/PID limits, and a wall-clock timeout.
- M1 does not contain LangGraph, FastAPI, MCP, model, Agent, approval-database, or reporting implementations.
- Every task follows red-green-refactor and ends with a focused commit.

## File Map

- `pyproject.toml`: package metadata, Python floor, dependencies, pytest configuration, and markers.
- `.gitignore`: Python, test, build, and RepoPilot runtime data exclusions.
- `src/repopilot/domain/results.py`: shared typed tool result envelope.
- `src/repopilot/security/paths.py`: Worktree path normalization and containment.
- `src/repopilot/security/commands.py`: test-command parsing and policy decisions.
- `src/repopilot/tools/filesystem.py`: file listing and bounded text reads.
- `src/repopilot/tools/search.py`: fixed-argument ripgrep JSON adapter.
- `src/repopilot/tools/git.py`: read-only status and complete diff, including untracked files.
- `src/repopilot/tools/patch.py`: validated, atomic Git patch application.
- `src/repopilot/tools/tests.py`: deterministic error extraction and Docker test execution.
- `src/repopilot/workspace/worktree.py`: managed Worktree creation, snapshots, and approved cleanup.
- `Dockerfile`: M1 Python 3.12 test-runner image.
- `tests/`: unit, integration, Docker integration, and fixture repositories.
- `README.md`: M1 setup, security contract, and verification commands.

## Review Focus

1. **Dirty original repository:** Worktree creation must use committed `HEAD`, preserve pre-existing staged/unstaged/untracked state byte-for-byte, and report that original snapshot unchanged; Task 8 adds this integration test.
2. **Untracked files in Diff:** A newly created file must appear in `git_diff()` without staging or mutating the index; Task 4 adds this test.
3. **Symlink escape through a missing descendant:** Resolving `link/new/file.py` must fail when `link` points outside the Worktree, even though the final file does not exist; Task 2 adds this test (mandatory in Linux CI, conditionally skipped only when local Windows cannot create symlinks).
4. **Partially valid patch:** If one file in a multi-file patch is invalid or escapes the Worktree, no file may change; Task 5 adds this atomicity test.
5. **Unavailable or hung Docker:** Missing daemon, image failure, and timeout must return distinct structured errors and never be mistaken for a test assertion failure; Task 7 adds unit tests and Task 9 requires one real Docker pass.

---

### Task 1: Package Scaffold and Shared Result Contract

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/repopilot/__init__.py`
- Create: `src/repopilot/domain/__init__.py`
- Create: `src/repopilot/domain/results.py`
- Create: `tests/unit/test_results.py`

**Interfaces:**
- Consumes: no project interfaces.
- Produces: `ToolResult[T]`, `ToolResult.success(...)`, and `ToolResult.failure(...)` for every later tool.

- [ ] **Step 1: Create package/test configuration and write failing result-contract tests**

`pyproject.toml` must declare `requires-python = ">=3.12"`, runtime dependency `unidiff>=0.7.5,<1`, dev dependency group containing `pytest>=8.3,<10` and `pytest-cov>=5,<8`, `pythonpath = ["src"]`, and markers `integration` and `docker`.

Write these tests in `tests/unit/test_results.py`:

```python
def test_success_result_contains_data_and_timing(): ...
def test_failure_result_contains_stable_error_code(): ...
def test_result_metadata_is_read_only_mapping(): ...
```

Assert success has `ok is True`, no error code, and preserves data; failure has `ok is False`, `data is None`, and preserves the provided code/message; metadata exposed by both constructors cannot be mutated through the result.

- [ ] **Step 2: Install the editable package and verify the tests fail**

Run: `python -m pip install -e ".[dev]"`
Run: `python -m pytest tests/unit/test_results.py -q`
Expected: FAIL during import because `repopilot.domain.results` does not exist yet.

- [ ] **Step 3: Implement the shared result envelope**

In `src/repopilot/domain/results.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class ToolResult(Generic[T]):
    ok: bool
    data: T | None
    error_code: str | None
    message: str
    duration_ms: int
    metadata: Mapping[str, object]

    @classmethod
    def success(cls, data: T, *, message: str = "", duration_ms: int = 0,
                metadata: Mapping[str, object] | None = None) -> "ToolResult[T]": ...

    @classmethod
    def failure(cls, error_code: str, message: str, *, duration_ms: int = 0,
                metadata: Mapping[str, object] | None = None) -> "ToolResult[T]": ...
```

Copy metadata into `types.MappingProxyType` so callers cannot mutate the recorded audit values.

- [ ] **Step 4: Run the focused tests**

Run: `python -m pytest tests/unit/test_results.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the scaffold**

```bash
git add pyproject.toml .gitignore src/repopilot tests/unit/test_results.py
git commit -m "chore: scaffold RepoPilot package"
```

### Task 2: Worktree Boundary and Filesystem Tools

**Files:**
- Create: `src/repopilot/security/__init__.py`
- Create: `src/repopilot/security/paths.py`
- Create: `src/repopilot/tools/__init__.py`
- Create: `src/repopilot/tools/filesystem.py`
- Create: `tests/unit/security/test_paths.py`
- Create: `tests/unit/tools/test_filesystem.py`

**Interfaces:**
- Consumes: `ToolResult[T]` from Task 1.
- Produces: `WorkspaceBoundaryError`, `resolve_workspace_path()`, `FileContent`, `list_files()`, and `read_file()`.

- [ ] **Step 1: Write failing path-boundary tests**

In `tests/unit/security/test_paths.py`, add:

```python
def test_resolve_accepts_existing_child(tmp_path): ...
def test_resolve_rejects_absolute_path(tmp_path): ...
def test_resolve_rejects_parent_traversal(tmp_path): ...
def test_resolve_rejects_symlink_to_outside(tmp_path): ...
def test_resolve_rejects_missing_descendant_below_external_symlink(tmp_path): ...
```

The last two tests must create a real symlink. Mark them skipped only when the local OS denies symlink creation; do not skip them in Linux CI.

- [ ] **Step 2: Run boundary tests and verify they fail**

Run: `python -m pytest tests/unit/security/test_paths.py -q`
Expected: FAIL during import because `repopilot.security.paths` is absent.

- [ ] **Step 3: Implement canonical containment**

In `src/repopilot/security/paths.py`, implement:

```python
class WorkspaceBoundaryError(ValueError): ...

def resolve_workspace_path(
    worktree_root: Path,
    relative_path: str | Path,
    *,
    must_exist: bool = True,
) -> Path: ...
```

Reject absolute inputs before joining. Resolve the Worktree root strictly; resolve the candidate with existing parents/symlinks accounted for; require `candidate.is_relative_to(resolved_root)`. For `must_exist=False`, a missing leaf is allowed only when its resolved existing parent remains inside the root.

- [ ] **Step 4: Run boundary tests**

Run: `python -m pytest tests/unit/security/test_paths.py -q`
Expected: PASS, with symlink tests either passing or explicitly skipped only for local privilege limitations.

- [ ] **Step 5: Write failing filesystem-tool tests**

In `tests/unit/tools/test_filesystem.py`, add:

```python
def test_list_files_returns_sorted_posix_paths_and_ignores_git(tmp_path): ...
def test_list_files_stops_at_entry_limit(tmp_path): ...
def test_read_file_returns_requested_one_based_line_range(tmp_path): ...
def test_read_file_marks_truncated_content(tmp_path): ...
def test_read_file_rejects_binary_content(tmp_path): ...
def test_file_tools_report_boundary_violation(tmp_path): ...
```

- [ ] **Step 6: Run filesystem tests and verify they fail**

Run: `python -m pytest tests/unit/tools/test_filesystem.py -q`
Expected: FAIL during import because `repopilot.tools.filesystem` is absent.

- [ ] **Step 7: Implement bounded file listing and reads**

In `src/repopilot/tools/filesystem.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class FileContent:
    path: str
    text: str
    start_line: int
    end_line: int
    truncated: bool

def list_files(
    worktree_root: Path,
    *,
    max_depth: int = 4,
    max_entries: int = 500,
    ignored_names: AbstractSet[str] = DEFAULT_IGNORED_NAMES,
) -> ToolResult[tuple[str, ...]]: ...

def read_file(
    worktree_root: Path,
    relative_path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    max_bytes: int = 1_000_000,
) -> ToolResult[FileContent]: ...
```

Use UTF-8 strict decoding, classify NUL-containing data as `binary_file`, validate positive one-based line ranges, return stable error codes, and never expose absolute paths in normal results.
Set `DEFAULT_IGNORED_NAMES = frozenset({".git", ".venv", "__pycache__", "node_modules"})`.

- [ ] **Step 8: Run Task 2 tests**

Run: `python -m pytest tests/unit/security/test_paths.py tests/unit/tools/test_filesystem.py -q`
Expected: PASS.

- [ ] **Step 9: Commit path and filesystem safety**

```bash
git add src/repopilot/security src/repopilot/tools tests/unit/security tests/unit/tools/test_filesystem.py
git commit -m "feat: add worktree-safe filesystem tools"
```

### Task 3: Ripgrep Code Search

**Files:**
- Create: `src/repopilot/tools/search.py`
- Create: `tests/unit/tools/test_search.py`

**Interfaces:**
- Consumes: `resolve_workspace_path()` and `ToolResult[T]`.
- Produces: `SearchMatch` and `search_code()`.

- [ ] **Step 1: Write failing search tests**

In `tests/unit/tools/test_search.py`, add:

```python
def test_search_returns_structured_unicode_match(tmp_path): ...
def test_search_treats_dash_prefixed_query_as_text(tmp_path): ...
def test_search_honors_file_glob_and_result_limit(tmp_path): ...
def test_search_returns_success_for_no_matches(tmp_path): ...
def test_search_rejects_empty_query(tmp_path): ...
def test_search_reports_timeout(monkeypatch, tmp_path): ...
def test_search_reports_missing_rg(monkeypatch, tmp_path): ...
```

Assert paths are relative POSIX strings, line/column are one-based, no-match exit code `1` produces an empty successful tuple, and rg failures use distinct `search_timeout` and `tool_unavailable` codes.

- [ ] **Step 2: Run search tests and verify they fail**

Run: `python -m pytest tests/unit/tools/test_search.py -q`
Expected: FAIL during import because `repopilot.tools.search` is absent.

- [ ] **Step 3: Implement the fixed-argument rg adapter**

In `src/repopilot/tools/search.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line: int
    column: int
    text: str

def search_code(
    worktree_root: Path,
    query: str,
    *,
    file_glob: str | None = None,
    max_results: int = 100,
    timeout_seconds: float = 10.0,
    rg_executable: str = "rg",
) -> ToolResult[tuple[SearchMatch, ...]]: ...
```

Call `rg` with a list containing `--json`, `--line-number`, `--column`, `--fixed-strings`, `--`, the query, and `.`. Set `cwd` to the resolved Worktree and `shell=False`; parse only `match` JSON events; cap returned results even if rg emits more. Convert rg's byte offset to a one-based Unicode character column before populating `SearchMatch.column`.

- [ ] **Step 4: Run search tests**

Run: `python -m pytest tests/unit/tools/test_search.py -q`
Expected: PASS.

- [ ] **Step 5: Commit search tooling**

```bash
git add src/repopilot/tools/search.py tests/unit/tools/test_search.py
git commit -m "feat: add bounded ripgrep search"
```

### Task 4: Read-only Git Status and Complete Diff

**Files:**
- Create: `src/repopilot/tools/git.py`
- Create: `tests/helpers/git.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/tools/test_git.py`

**Interfaces:**
- Consumes: `resolve_workspace_path()` and `ToolResult[T]`.
- Produces: `GitStatusData`, `GitDiffData`, `git_status()`, and `git_diff()`.

- [ ] **Step 1: Add a deterministic temporary-repository helper and failing Git tests**

`tests/helpers/git.py` must expose `init_repo(path: Path, files: Mapping[str, str]) -> Path`, using command argument arrays and per-command `-c user.name=RepoPilotTest -c user.email=test@repopilot.invalid` for fixture commits.

In `tests/unit/tools/test_git.py`, add:

```python
def test_git_status_reports_modified_and_untracked_paths(git_repo): ...
def test_git_status_clean_repository(git_repo): ...
def test_git_diff_contains_tracked_modification(git_repo): ...
def test_git_diff_contains_untracked_new_file_without_staging(git_repo): ...
def test_git_diff_does_not_change_index(git_repo): ...
def test_git_tools_reject_non_repository(tmp_path): ...
```

- [ ] **Step 2: Run Git tests and verify they fail**

Run: `python -m pytest tests/unit/tools/test_git.py -q`
Expected: FAIL during import because `repopilot.tools.git` is absent.

- [ ] **Step 3: Implement status and full diff**

In `src/repopilot/tools/git.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class GitStatusData:
    clean: bool
    changed_files: tuple[str, ...]
    raw_porcelain_v2: str

@dataclass(frozen=True, slots=True)
class GitDiffData:
    patch: str
    changed_files: tuple[str, ...]

def git_status(worktree_root: Path) -> ToolResult[GitStatusData]: ...
def git_diff(worktree_root: Path) -> ToolResult[GitDiffData]: ...
```

Use `git status --porcelain=v2 -z --untracked-files=all` for status and `git diff --no-ext-diff --binary --` for tracked changes. Append each untracked file through `git diff --no-index --binary -- /dev/null <relative-path>`; accept its expected exit code `1`. The implementation must never invoke `git add`; the test snapshots `git diff --cached --binary` before and after to prove the index is unchanged.

- [ ] **Step 4: Run Git tests**

Run: `python -m pytest tests/unit/tools/test_git.py -q`
Expected: PASS.

- [ ] **Step 5: Commit Git read tools**

```bash
git add src/repopilot/tools/git.py tests/helpers tests/unit/tools/test_git.py tests/conftest.py
git commit -m "feat: add safe Git status and diff tools"
```

### Task 5: Validated Atomic Patch Application

**Files:**
- Create: `src/repopilot/tools/patch.py`
- Create: `tests/unit/tools/test_patch.py`

**Interfaces:**
- Consumes: `resolve_workspace_path()`, `ToolResult[T]`, and `unidiff.PatchSet`.
- Produces: `PatchInspection`, `PatchApplyData`, `inspect_patch()`, and `apply_patch()`.

- [ ] **Step 1: Write failing patch tests**

In `tests/unit/tools/test_patch.py`, add:

```python
def test_apply_patch_modifies_existing_file(git_repo): ...
def test_apply_patch_creates_new_file(git_repo): ...
def test_delete_patch_requires_approval(git_repo): ...
def test_approved_delete_patch_removes_file(git_repo): ...
def test_patch_rejects_parent_traversal_before_write(git_repo): ...
def test_malformed_patch_changes_nothing(git_repo): ...
def test_multifile_patch_is_atomic_when_one_target_is_invalid(git_repo): ...
```

For every failure case, hash or read all original files before the call and assert they remain unchanged afterward.

- [ ] **Step 2: Run patch tests and verify they fail**

Run: `python -m pytest tests/unit/tools/test_patch.py -q`
Expected: FAIL during import because `repopilot.tools.patch` is absent.

- [ ] **Step 3: Implement inspection and atomic application**

In `src/repopilot/tools/patch.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class PatchInspection:
    paths: tuple[str, ...]
    deletes_files: bool

@dataclass(frozen=True, slots=True)
class PatchApplyData:
    changed_files: tuple[str, ...]
    deletes_files: bool

def inspect_patch(
    worktree_root: Path,
    patch_text: str,
    *,
    max_bytes: int = 1_000_000,
) -> ToolResult[PatchInspection]: ...

def apply_patch(
    worktree_root: Path,
    patch_text: str,
    *,
    deletion_approved: bool = False,
    max_bytes: int = 1_000_000,
) -> ToolResult[PatchApplyData]: ...
```

Parse with `PatchSet`, normalize source/target paths by removing only the standard `a/` or `b/` prefix, validate every non-`/dev/null` target before any subprocess, and return `approval_required` for deletions without approval. Run `git apply --check --whitespace=error-all -` first, then `git apply --whitespace=nowarn -`, sending the patch on stdin with `shell=False`.

- [ ] **Step 4: Run patch tests**

Run: `python -m pytest tests/unit/tools/test_patch.py -q`
Expected: PASS.

- [ ] **Step 5: Commit patch tooling**

```bash
git add src/repopilot/tools/patch.py tests/unit/tools/test_patch.py
git commit -m "feat: add validated atomic patch tool"
```

### Task 6: Test Command Policy and Error Inspection

**Files:**
- Create: `src/repopilot/security/commands.py`
- Create: `src/repopilot/tools/tests.py`
- Create: `tests/unit/security/test_commands.py`
- Create: `tests/unit/tools/test_error_inspection.py`

**Interfaces:**
- Consumes: `ToolResult[T]`.
- Produces: `CommandDecision`, `evaluate_test_command()`, `ErrorSummary`, and `inspect_error()`.

- [ ] **Step 1: Write failing command-policy tests**

In `tests/unit/security/test_commands.py`, add:

```python
def test_allows_pytest_with_arguments(): ...
def test_allows_python_module_pytest(): ...
def test_unknown_executable_requires_approval(): ...
def test_empty_command_is_denied(): ...
def test_malformed_quoting_is_denied(): ...
@pytest.mark.parametrize("token", ["&&", "|", ";", ">", "<", "`", "$("])
def test_shell_syntax_is_permanently_denied(token): ...
```

Assert approved commands return normalized immutable argv, unknown executables return `approval_required`, and shell syntax returns `deny` even if approval was requested.

- [ ] **Step 2: Run policy tests and verify they fail**

Run: `python -m pytest tests/unit/security/test_commands.py -q`
Expected: FAIL during import because `repopilot.security.commands` is absent.

- [ ] **Step 3: Implement command evaluation**

In `src/repopilot/security/commands.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class CommandDecision:
    outcome: Literal["allow", "approval_required", "deny"]
    argv: tuple[str, ...]
    reason: str

def evaluate_test_command(
    command: str,
    *,
    extra_allowed_prefixes: tuple[tuple[str, ...], ...] = (),
) -> CommandDecision: ...
```

Parse with `shlex.split(..., posix=True)`. Default allowed prefixes are `("pytest",)` and `("python", "-m", "pytest")`. Reject newline and the listed shell operators before parsing.

- [ ] **Step 4: Run policy tests**

Run: `python -m pytest tests/unit/security/test_commands.py -q`
Expected: PASS.

- [ ] **Step 5: Write failing deterministic error-inspection tests**

In `tests/unit/tools/test_error_inspection.py`, add:

```python
def test_extracts_pytest_failed_node_ids(): ...
def test_extracts_exception_types_and_locations(): ...
def test_limits_items_and_tail_size(): ...
def test_empty_output_returns_empty_summary(): ...
```

- [ ] **Step 6: Run inspection tests and verify they fail**

Run: `python -m pytest tests/unit/tools/test_error_inspection.py -q`
Expected: FAIL because `inspect_error` is absent.

- [ ] **Step 7: Implement bounded failure extraction**

In `src/repopilot/tools/tests.py`, implement:

```python
@dataclass(frozen=True, slots=True)
class ErrorSummary:
    failed_tests: tuple[str, ...]
    exception_types: tuple[str, ...]
    locations: tuple[str, ...]
    tail: str
    truncated: bool

def inspect_error(
    output: str,
    *,
    max_chars: int = 20_000,
    max_items: int = 20,
) -> ToolResult[ErrorSummary]: ...
```

Recognize pytest `FAILED <node-id>`, traceback `path.py:<line>`, and `E   <ExceptionName>:` forms. Preserve only bounded unique items and the bounded output tail.

- [ ] **Step 8: Run Task 6 tests**

Run: `python -m pytest tests/unit/security/test_commands.py tests/unit/tools/test_error_inspection.py -q`
Expected: PASS.

- [ ] **Step 9: Commit command policy and error inspection**

```bash
git add src/repopilot/security/commands.py src/repopilot/tools/tests.py tests/unit/security/test_commands.py tests/unit/tools/test_error_inspection.py
git commit -m "feat: add test policy and error inspection"
```

### Task 7: Hardened Docker Test Runner

**Files:**
- Create: `Dockerfile`
- Modify: `src/repopilot/tools/tests.py`
- Create: `tests/unit/tools/test_docker_runner.py`
- Create: `tests/integration/test_docker_runner.py`

**Interfaces:**
- Consumes: `evaluate_test_command()`, `ToolResult[T]`, and Worktree path validation.
- Produces: `DockerRunnerConfig`, `DockerAvailability`, `TestRunResult`, `DockerTestRunner.is_available()`, and `DockerTestRunner.run()`.

- [ ] **Step 1: Write failing Docker command-construction tests**

In `tests/unit/tools/test_docker_runner.py`, mock `subprocess.run` and add:

```python
def test_runner_uses_all_required_hardening_flags(monkeypatch, tmp_path): ...
def test_runner_passes_test_as_argv_not_shell(monkeypatch, tmp_path): ...
def test_runner_rejects_denied_command_before_docker(monkeypatch, tmp_path): ...
def test_runner_returns_approval_required_for_unknown_command(monkeypatch, tmp_path): ...
def test_runner_distinguishes_test_failure_from_docker_failure(monkeypatch, tmp_path): ...
def test_runner_maps_timeout_to_test_timeout(monkeypatch, tmp_path): ...
def test_unavailable_daemon_returns_docker_unavailable(monkeypatch): ...
```

Assert the `docker run` argv contains `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`, `--user`, a single Worktree bind mount, and no `-e` secret forwarding.

- [ ] **Step 2: Run Docker unit tests and verify they fail**

Run: `python -m pytest tests/unit/tools/test_docker_runner.py -q`
Expected: FAIL because Docker runner interfaces are absent.

- [ ] **Step 3: Implement the runner image and Docker adapter**

`Dockerfile` must start from `python:3.12-slim`, create an unprivileged `repopilot` user, install only `pytest>=8.3,<10`, set `/workspace` as working directory, and define no network-dependent entrypoint.

Add to `src/repopilot/tools/tests.py`:

```python
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

class DockerTestRunner:
    def is_available(self) -> ToolResult[DockerAvailability]: ...
    def run(self, worktree_root: Path, test_command: str,
            *, config: DockerRunnerConfig = DockerRunnerConfig(),
            command_approved: bool = False) -> ToolResult[TestRunResult]: ...
```

Use `docker version --format` for availability. A completed container with exit code `1` is a successful tool call whose `TestRunResult.exit_code` is `1`; daemon/image/process failures are failed tool results with specific codes. Bound captured stdout/stderr to 1 MiB each and mark truncation in metadata.

- [ ] **Step 4: Run Docker unit tests**

Run: `python -m pytest tests/unit/tools/test_docker_runner.py -q`
Expected: PASS without a running daemon because subprocesses are mocked.

- [ ] **Step 5: Write the real Docker integration test**

In `tests/integration/test_docker_runner.py`, mark tests `@pytest.mark.integration` and `@pytest.mark.docker`. Add:

```python
def test_real_container_runs_fixture_tests_without_network(tmp_path): ...
```

The test must call `DockerTestRunner.is_available()` and skip with its explicit error only when the daemon is unavailable. When available, it must prove a passing fixture returns exit code `0` and a failing fixture returns nonzero without classifying Docker itself as failed.

- [ ] **Step 6: Build the runner image and run real Docker integration**

Run: `docker build -t repopilot-runner:m1 .`
Run: `python -m pytest tests/integration/test_docker_runner.py -m docker -q`
Expected: PASS. If the daemon is not running, record this step as blocked rather than passed; M1 cannot be accepted until rerun successfully.

- [ ] **Step 7: Commit Docker test execution**

```bash
git add Dockerfile src/repopilot/tools/tests.py tests/unit/tools/test_docker_runner.py tests/integration/test_docker_runner.py
git commit -m "feat: run tests in hardened Docker sandbox"
```

### Task 8: Managed Git Worktree Lifecycle

**Files:**
- Create: `src/repopilot/workspace/__init__.py`
- Create: `src/repopilot/workspace/worktree.py`
- Create: `tests/integration/test_worktree.py`

**Interfaces:**
- Consumes: `ToolResult[T]`, Git CLI, and canonical paths.
- Produces: `RepoSnapshot`, `WorktreeInfo`, `capture_repo_snapshot()`, and `WorktreeManager`.

- [ ] **Step 1: Write failing Worktree integration tests**

In `tests/integration/test_worktree.py`, mark tests `@pytest.mark.integration` and add:

```python
def test_create_worktree_at_committed_head_without_touching_original(git_repo, tmp_path): ...
def test_dirty_original_state_is_byte_for_byte_unchanged(git_repo, tmp_path): ...
def test_create_is_idempotent_for_registered_task_worktree(git_repo, tmp_path): ...
def test_rejects_invalid_task_id(git_repo, tmp_path): ...
def test_rejects_data_directory_inside_original_repository(git_repo): ...
def test_cleanup_requires_explicit_approval(git_repo, tmp_path): ...
def test_approved_cleanup_removes_only_dirty_managed_worktree(git_repo, tmp_path): ...
```

The dirty-state test must include staged, unstaged, and untracked changes; capture `HEAD`, branch name, porcelain-v2 status, index hash, and file hashes before/after.

- [ ] **Step 2: Run Worktree tests and verify they fail**

Run: `python -m pytest tests/integration/test_worktree.py -q`
Expected: FAIL during import because `repopilot.workspace.worktree` is absent.

- [ ] **Step 3: Implement snapshots and managed lifecycle**

In `src/repopilot/workspace/worktree.py`, implement:

```python
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

def capture_repo_snapshot(repo_path: Path) -> ToolResult[RepoSnapshot]: ...

class WorktreeManager:
    def __init__(self, data_dir: Path): ...
    def create(self, repo_path: Path, task_id: str) -> ToolResult[WorktreeInfo]: ...
    def cleanup(self, info: WorktreeInfo, *, approved: bool = False) -> ToolResult[None]: ...
```

Validate task IDs against `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. Use `sha256(resolved_repo_path)[:12]` for the data directory, branch `repopilot/<task-id>`, and `git worktree add -b <branch> <path> <base_commit>`. If the expected path is already registered at the same base commit, return the existing `WorktreeInfo`; a branch/path conflict returns `workspace_conflict`. Before cleanup, verify the path is registered by `git worktree list --porcelain` and lies inside the manager's resolved data directory; after explicit approval call `git worktree remove --force` for that exact managed path so a task with expected edits can be removed.

- [ ] **Step 4: Run Worktree integration tests**

Run: `python -m pytest tests/integration/test_worktree.py -q`
Expected: PASS.

- [ ] **Step 5: Commit Worktree management**

```bash
git add src/repopilot/workspace tests/integration/test_worktree.py
git commit -m "feat: add isolated Git worktree lifecycle"
```

### Task 9: M1 End-to-End Verification and Documentation

**Files:**
- Create: `tests/fixtures/calculator/calculator.py`
- Create: `tests/fixtures/calculator/test_calculator.py`
- Create: `tests/integration/test_m1_toolchain.py`
- Create: `README.md`

**Interfaces:**
- Consumes: all M1 public interfaces.
- Produces: one repeatable M1 proof that creates an isolated Worktree, searches/reads/patches code, generates a complete Diff, runs Docker tests, and proves the original checkout unchanged.

- [ ] **Step 1: Add the failing calculator fixture and write the M1 workflow test**

The fixture must contain a deterministic defect in `divide(a, b)` and a pytest asserting the required zero-division behavior. In `tests/integration/test_m1_toolchain.py`, mark the test `integration` and `docker`, initially set `REPAIR_PATCH = ""`, and add:

```python
def test_m1_tools_fix_fixture_in_isolated_worktree(tmp_path): ...
```

The test must:

1. Copy the fixture into a temporary Git repository and commit it.
2. Capture the original snapshot.
3. Create a managed Worktree.
4. Use `list_files`, `search_code`, and `read_file` to find the defect.
5. Apply a fixed, checked-in patch string with `apply_patch`.
6. Assert `git_status` and `git_diff` report only the intended file.
7. Run `pytest -q` through `DockerTestRunner` and assert exit code `0`.
8. Re-capture the original snapshot and assert it equals the initial snapshot.
9. Assert unapproved cleanup is refused, then clean up with explicit approval.

- [ ] **Step 2: Run the M1 workflow test and verify the intended initial failure**

Run: `python -m pytest tests/integration/test_m1_toolchain.py -m docker -q`
Expected: FAIL because the deliberately empty `REPAIR_PATCH` does not repair the fixture, not because of an import or infrastructure error.

- [ ] **Step 3: Complete the fixture patch and M1 documentation**

Replace `REPAIR_PATCH` with the exact unified Diff that adds the required zero-division behavior, then write `README.md` with:

- the M1 purpose and explicit non-goals;
- Python 3.12 environment setup;
- `python -m pip install -e ".[dev]"`;
- required `git`, `rg`, and Docker prerequisites;
- Docker image build command;
- unit, integration, Docker, and complete verification commands;
- security guarantees and known M1 limits;
- a note that Docker daemon must be running for acceptance.

- [ ] **Step 4: Run all non-Docker tests**

Run: `python -m pytest -m "not docker" -q`
Expected: PASS with no unexpected skips.

- [ ] **Step 5: Run static package checks**

Run: `python -m compileall -q src tests`
Expected: exit code `0`.

- [ ] **Step 6: Build and run complete Docker acceptance**

Run: `docker build -t repopilot-runner:m1 .`
Run: `python -m pytest -m docker -q`
Expected: PASS with no skipped Docker tests. A stopped daemon is a blocker, not acceptance.

- [ ] **Step 7: Verify source-control safety**

Run: `git status --short`
Expected: only the intentional M1 implementation/documentation files are present before the commit; no `.repopilot`, fixture runtime repo, Worktree, database, log, or test artifact is tracked.

- [ ] **Step 8: Commit M1 acceptance proof**

```bash
git add README.md tests/fixtures tests/integration/test_m1_toolchain.py
git commit -m "test: verify M1 isolated toolchain"
```

- [ ] **Step 9: Record final evidence**

Run: `git status --short --branch`
Expected: clean branch.
Run: `git log --oneline --decorate -10`
Expected: focused commits for Tasks 1-9 following the design commit.

## M1 Completion Gate

M1 is complete only when all of the following are true:

- Every focused test command in Tasks 1-9 passes.
- `python -m pytest -m "not docker" -q` passes without unexpected skips.
- `docker build -t repopilot-runner:m1 .` succeeds.
- `python -m pytest -m docker -q` passes with no skips.
- The end-to-end test proves the original repository snapshot is unchanged.
- The complete Diff includes both tracked and untracked changes without staging.
- Deletion and cleanup return `approval_required` until explicitly approved.
- No API, model, LangGraph, MCP, or multi-Agent placeholder is introduced in M1.
- The Git working tree is clean and the README commands match the verified commands.

## Deferred to Later Milestones

- M2: RepoAgent, LangGraph state/routing, SQLite checkpoint/business tables, FastAPI, SSE, automatic code-repair loop.
- M3: persistent approval records, MCP transports, task artifacts/reports, evaluation dataset and metrics.
- M4: Agents SDK supervisor/specialists, Handoff, model profiles for Qwen/DeepSeek, local/exported tracing.
