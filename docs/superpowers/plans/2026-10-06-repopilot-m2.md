# RepoPilot M2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent single-Agent repository repair service that plans, patches, tests, fixes, reviews, resumes, and streams progress while supporting Fake, DeepSeek, Qwen, and custom OpenAI-compatible models.

**Architecture:** FastAPI calls a `TaskService` backed by SQLite; one in-process `TaskWorker` drives a checkpointed LangGraph workflow. `RepoAgent` performs bounded read-only discovery and structured model stages through `ModelGateway`, while all writes, tests, worktree operations, state transitions, and cancellation checks remain deterministic application code built on M1 services.

**Tech Stack:** Python 3.12, Pydantic 2, FastAPI, aiosqlite, LangGraph 1.2, LangGraph SQLite checkpoint, OpenAI Python SDK 3, Docker, Git, pytest

**Spec:** `docs/superpowers/specs/2026-10-06-repopilot-m2-design.md`

## Global Constraints

- Preserve M1's path, command, patch, Worktree, Docker, and immutable `ToolResult` security boundaries.
- Only a Docker exit code of `0` means tests passed; timeouts and Docker failures are failures with distinct error types.
- Model code may use only `list_files`, `search_code`, `read_file`, `git_status`, `git_diff`, and `inspect_error`; it must never invoke writes, tests, cleanup, shell, approval, cancellation, or status mutation.
- Every graph node checks persisted cancellation; `create_worktree`, `apply_patch`, and `run_tests` check again immediately before their side effect.
- `retry_count` starts at `0` and increments only when scheduling `propose_fix`; `max_retries=2` allows one initial patch and two repair patches.
- Provider retry and one JSON-format repair do not increment `retry_count`.
- Store bounded business input, state, summaries, and usage only; never persist API keys, authorization headers, assembled prompts, raw provider responses, or unredacted environment variables.
- Use stable UUID `thread_id` values and reuse the original value for every resume.
- Set `LANGGRAPH_STRICT_MSGPACK=true` before constructing the SQLite checkpointer.
- M2 never commits, merges, pushes, modifies the original checkout, or cleans a Worktree automatically.
- Default tests and CI use `fake`; real provider smoke tests run only with an explicit pytest marker and the required environment variables.
- Keep Python at `>=3.12`; add `fastapi>=0.142,<1`, `uvicorn>=0.54,<1`, `pydantic>=2.13,<3`, `aiosqlite>=0.22,<1`, `langgraph>=1.2.12,<2`, `langgraph-checkpoint-sqlite>=3.1,<4`, and `openai>=3.24,<4`.
- Event types are exactly `task_created`, `task_started`, `stage_started`, `tool_completed`, `model_completed`, `patch_applied`, `test_completed`, `retry_scheduled`, `approval_required`, `task_succeeded`, `task_failed`, and `task_cancelled`.

## Review Focus

- A request containing a likely API key or private-key header must return validation failure and leave no task, event, or model-call row; pin this in Task 1 and Task 9.
- A provider that requests an unknown read-only tool or never produces a final answer must stop safely after the four-round limit; pin this in Task 5.
- A restart after a patch was applied but before the next checkpoint must reconcile the existing Diff instead of applying the same patch twice; pin this in Task 7 and Task 11.
- Cancellation requested while Docker is already running may wait for that run, but must prevent every later model, patch, test, or review side effect; pin this in Task 8 and Task 11.
- A malformed, negative, or cross-task `Last-Event-ID` must not leak another task's events or crash the stream; pin this in Task 10.

---

### Task 1: M2 dependencies, task contracts, and secret rejection

**Files:**
- Modify: `pyproject.toml`
- Create: `src/repopilot/domain/tasks.py`
- Create: `src/repopilot/security/secrets.py`
- Create: `tests/unit/domain/test_tasks.py`
- Create: `tests/unit/security/test_secrets.py`

**Interfaces:**
- Consumes: existing Python 3.12 package and pytest configuration.
- Produces: `TaskStatus`, `EventType`, `TERMINAL_STATUSES`, `TaskInput`, `TaskRecord`, `EventRecord`, `ModelCallRecord`, `can_transition(current, target) -> bool`, and `find_secret_kind(value: str) -> str | None`.

- [ ] **Step 1: Add failing contract tests**

Test that the six statuses are exactly `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, and `cancelled`; event names are exactly the 12 names in the M2 spec; terminal states reject all transitions; `awaiting_approval → cancelled` is allowed; frozen records cannot be mutated; likely `sk-...`, bearer-token, and private-key header inputs are detected while ordinary repository requirements are accepted.

- [ ] **Step 2: Run the focused tests and verify collection/import failure**

Run: `python -m pytest tests/unit/domain/test_tasks.py tests/unit/security/test_secrets.py -q`
Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Add dependencies, markers, and minimal contracts**

Add the Global Constraints dependency ranges plus `httpx>=0.28,<1` and `pytest-asyncio>=1.2,<2` to `dev`; add `deepseek_smoke` and `qwen_smoke` markers. Implement immutable dataclasses with timezone-aware ISO timestamps and exact transition rules. Implement bounded pattern detection without ever returning the matched secret value.

- [ ] **Step 4: Run focused and M1 tests**

Run: `python -m pytest tests/unit/domain/test_tasks.py tests/unit/security/test_secrets.py tests/unit/test_results.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/repopilot/domain/tasks.py src/repopilot/security/secrets.py tests/unit/domain/test_tasks.py tests/unit/security/test_secrets.py
git commit -m "feat: add M2 task domain contracts"
```

### Task 2: SQLite business persistence and atomic events

**Files:**
- Create: `src/repopilot/persistence/__init__.py`
- Create: `src/repopilot/persistence/database.py`
- Create: `src/repopilot/persistence/migrations.py`
- Create: `src/repopilot/persistence/repositories.py`
- Create: `tests/unit/persistence/test_repositories.py`

**Interfaces:**
- Consumes: Task 1 domain records and transitions.
- Produces: `SqliteDatabase(path: Path)`, `TaskRepository`, `EventRepository`, and `ModelCallRepository`.
- `TaskRepository.create(task_input: TaskInput, *, task_id: str, thread_id: str) -> TaskRecord`
- `TaskRepository.get(task_id: str) -> TaskRecord | None`
- `TaskRepository.claim_next() -> TaskRecord | None`
- `TaskRepository.transition(task_id: str, target: TaskStatus, *, stage: str, event_type: EventType, payload: Mapping[str, object], dedupe_key: str | None = None, error_type: str | None = None, error_message: str | None = None) -> TaskRecord`
- `TaskRepository.request_cancel(task_id: str) -> TaskRecord`
- `TaskRepository.update_execution(task_id: str, **bounded_fields: object) -> TaskRecord`
- `EventRepository.list_after(task_id: str, event_id: int, *, limit: int = 100) -> tuple[EventRecord, ...]`
- `ModelCallRepository.add(record: ModelCallRecord) -> None`

- [ ] **Step 1: Write failing async repository tests**

Cover schema version creation, WAL/foreign-key/busy-timeout pragmas, create plus `task_created` in one transaction, recovery claiming pre-existing `running` tasks before the oldest `queued` task without duplicating `task_started`, atomic status/event transition, invalid transition rollback, nullable event dedupe keys with a per-task unique partial index, monotonic event IDs, `list_after`, cancellation of queued/running/awaiting tasks, terminal conflicts, and model-call usage persistence without prompt columns.

- [ ] **Step 2: Run the tests and verify missing persistence modules**

Run: `python -m pytest tests/unit/persistence/test_repositories.py -q`
Expected: FAIL during import.

- [ ] **Step 3: Implement versioned SQLite schema and repositories**

Use short-lived `aiosqlite` connections configured with `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, and a 5-second busy timeout. Create only `schema_version`, `tasks`, `events`, and `model_calls`; do not create M3 approvals/artifacts tables. Make transition and its event a single transaction, make repeated non-null `dedupe_key` writes idempotent, serialize event payloads with deterministic JSON, and whitelist every field accepted by `update_execution`.

- [ ] **Step 4: Run repository tests**

Run: `python -m pytest tests/unit/persistence/test_repositories.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repopilot/persistence tests/unit/persistence
git commit -m "feat: persist tasks events and model usage"
```

### Task 3: Provider profiles and protected credentials

**Files:**
- Create: `src/repopilot/models/__init__.py`
- Create: `src/repopilot/models/profiles.py`
- Create: `tests/unit/models/test_profiles.py`

**Interfaces:**
- Consumes: process environment only at registry construction time.
- Produces: `ModelProvider`, `ModelProfile`, `ResolvedModelProfile`, `ModelProfileRegistry.from_env(environ: Mapping[str, str])`, and `registry.resolve(name: str) -> ResolvedModelProfile`.

- [ ] **Step 1: Write failing profile tests**

Assert `fake` needs no key; DeepSeek reads only `DEEPSEEK_API_KEY`; Qwen reads only `DASHSCOPE_API_KEY`; custom reads only `MODEL_API_KEY`; model names are required; Qwen/custom require explicit HTTPS base URLs; DeepSeek defaults to `https://api.deepseek.com`; unknown profiles and missing keys fail with stable codes; `repr()` and serialization never reveal a key.

- [ ] **Step 2: Run the tests and verify missing module failure**

Run: `python -m pytest tests/unit/models/test_profiles.py -q`
Expected: FAIL during import.

- [ ] **Step 3: Implement immutable profile registry**

Use `pydantic.SecretStr` in resolved profiles, reject URL credentials/query/fragment and non-HTTPS remote endpoints, and expose only an allowlisted profile name to HTTP callers. Do not accept an environment-variable name or raw key from task input.

- [ ] **Step 4: Run profile tests**

Run: `python -m pytest tests/unit/models/test_profiles.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repopilot/models tests/unit/models/test_profiles.py
git commit -m "feat: configure protected model profiles"
```

### Task 4: OpenAI-compatible gateway, Fake transport, retry, and usage

**Files:**
- Create: `src/repopilot/models/types.py`
- Create: `src/repopilot/models/fake.py`
- Create: `src/repopilot/models/gateway.py`
- Create: `tests/unit/models/test_gateway.py`

**Interfaces:**
- Consumes: Task 3 `ResolvedModelProfile`; Task 2 `ModelCallRepository` through a `ModelCallSink` protocol.
- Produces: `ChatMessage`, `ToolDefinition`, `ModelToolCall`, `ModelUsage`, `ModelTurn`, `ModelTransport` protocol, `OpenAIChatTransport`, `ScriptedFakeTransport`, and `ModelGateway.complete(task_id: str, stage: str, profile: ResolvedModelProfile, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition] = ()) -> ModelTurn`.
- `ModelGateway.repair_json(..., schema: Mapping[str, object], invalid_output: str, validation_error: str) -> ModelTurn` performs exactly one separate repair call when requested by `RepoAgent`.

- [ ] **Step 1: Write failing gateway tests**

Cover scripted Fake turns, `AsyncOpenAI(base_url=..., api_key=...)`, Chat Completions message/tool translation, bounded timeout/connection/429/5xx exponential retry, no retry on authentication/invalid request, aggregation of usage, one `model_calls` row per logical call, redacted errors, and no prompt/response persistence.

- [ ] **Step 2: Run the tests and verify missing gateway failure**

Run: `python -m pytest tests/unit/models/test_gateway.py -q`
Expected: FAIL during import.

- [ ] **Step 3: Implement transports and gateway**

Use `openai.AsyncOpenAI.chat.completions.create`, local dataclass conversion, an injected async sleeper for deterministic retry tests, at most three transport attempts, and capped response text. Do not use SDK strict parsing because compatibility endpoints differ; Pydantic validation belongs to Task 5.

- [ ] **Step 4: Run gateway tests**

Run: `python -m pytest tests/unit/models/test_gateway.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repopilot/models tests/unit/models/test_gateway.py
git commit -m "feat: add compatible model gateway"
```

### Task 5: Structured RepoAgent with bounded read-only tools

**Files:**
- Create: `src/repopilot/agent/__init__.py`
- Create: `src/repopilot/agent/schemas.py`
- Create: `src/repopilot/agent/toolbox.py`
- Create: `src/repopilot/agent/repo_agent.py`
- Create: `tests/unit/agent/test_schemas.py`
- Create: `tests/unit/agent/test_toolbox.py`
- Create: `tests/unit/agent/test_repo_agent.py`

**Interfaces:**
- Consumes: Task 4 `ModelGateway`; M1 read-only tools.
- Produces: strict Pydantic `ChangePlan`, `PatchProposal`, `FailureAnalysis`, and `ReviewDecision`; `ReadOnlyToolbox.execute(worktree_root: Path, call: ModelToolCall) -> ToolResult[Mapping[str, object]]`; `RepoAgent.plan`, `propose_patch`, `analyze_failure`, and `review` returning those four schemas.

- [ ] **Step 1: Write failing schema and toolbox tests**

Assert extra fields are forbidden, patches are non-empty bounded unified Diff text, expected files are relative, review severity is constrained, all six allowed tools serialize bounded data, unknown/write/shell tools are denied before execution, and caller-supplied paths cannot replace the injected Worktree root.

- [ ] **Step 2: Write failing Agent-loop tests**

Test direct structured output, read-only tool call followed by final output, four-round maximum, unknown tool, repeated tool requests, malformed JSON repaired once, second malformed output failure, and stage-specific prompts that omit keys and host environment.

- [ ] **Step 3: Run tests and verify missing modules**

Run: `python -m pytest tests/unit/agent -q`
Expected: FAIL during import.

- [ ] **Step 4: Implement schemas, toolbox, and Agent loop**

Parse final text with `model_validate_json`; on the first validation error call `repair_json` with capped invalid text and validation details, then validate once more. Preserve provider tool-call IDs in returned tool messages. Require final structured output by the end of round four and return stable `model_output_invalid` after the second invalid result.

- [ ] **Step 5: Run Agent tests**

Run: `python -m pytest tests/unit/agent -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/repopilot/agent tests/unit/agent
git commit -m "feat: add bounded structured repository agent"
```

### Task 6: Fixed-base Worktree creation

**Files:**
- Modify: `src/repopilot/workspace/worktree.py`
- Modify: `tests/integration/test_worktree.py`

**Interfaces:**
- Consumes: existing `WorktreeManager` and `capture_repo_snapshot`.
- Produces: backward-compatible `WorktreeManager.create(repo_path: Path, task_id: str, *, base_commit: str | None = None) -> ToolResult[WorktreeInfo]`.

- [ ] **Step 1: Add failing race and validation tests**

Capture commit A, advance the original checkout to commit B, create with `base_commit=A`, and assert the managed Worktree is at A while the original remains at B. Also reject non-commit refs, missing objects, a forged hash, and an existing task Worktree registered at a different base.

- [ ] **Step 2: Run focused tests and verify signature/behavior failure**

Run: `python -m pytest tests/integration/test_worktree.py -q`
Expected: FAIL on the new fixed-base test.

- [ ] **Step 3: Implement exact-commit creation**

Resolve an optional base through `git rev-parse --verify <value>^{commit}`, require the caller value to be a full 40/64-character hexadecimal object ID equal to the resolved commit, use it for idempotence and `git worktree add`, and keep the original snapshot solely for later invariance proof.

- [ ] **Step 4: Run all Worktree and M1 integration tests**

Run: `python -m pytest tests/integration/test_worktree.py tests/integration/test_m1_toolchain.py -q`
Expected: PASS, with the Docker test passing when the daemon/image is available or explicitly skipped only for local unavailability.

- [ ] **Step 5: Commit**

```bash
git add src/repopilot/workspace/worktree.py tests/integration/test_worktree.py
git commit -m "feat: create worktrees from fixed commits"
```

### Task 7: LangGraph state, deterministic nodes, routing, and checkpointing

**Files:**
- Create: `src/repopilot/graph/__init__.py`
- Create: `src/repopilot/graph/state.py`
- Create: `src/repopilot/graph/routing.py`
- Create: `src/repopilot/graph/nodes.py`
- Create: `src/repopilot/graph/builder.py`
- Modify: `src/repopilot/tools/patch.py`
- Create: `tests/unit/graph/test_routing.py`
- Create: `tests/unit/graph/test_nodes.py`
- Modify: `tests/unit/tools/test_patch.py`
- Create: `tests/integration/test_graph_checkpoint.py`

**Interfaces:**
- Consumes: Tasks 2, 5, and 6; M1 patch/Git/Docker tools.
- Produces: backward-compatible `apply_patch(..., already_applied_ok: bool = False)`, whose `PatchApplyData.already_applied` distinguishes a new write from a verified prior application; `RepoPilotState`; `GraphDependencies`; `build_graph(deps: GraphDependencies, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph`; and `graph_config(thread_id: str) -> RunnableConfig`.
- `RepoPilotState` fields are exactly `task_id`, `thread_id`, `repo_path`, `user_request`, `test_command`, `base_commit`, `worktree_path`, `repo_summary`, `change_plan`, `patch_text`, `changed_files`, `test_exit_code`, `test_output`, `tests_passed`, `failure_analysis`, `review_decision`, `retry_count`, `max_retries`, `cancel_requested`, `pending_approval`, `current_stage`, `error_type`, and `error_message`, with the types fixed by the M2 spec.

- [ ] **Step 1: Write failing pure routing tests**

Cover initial patch success, test failure to fix, review critical to fix, exact `max_retries` exhaustion, infrastructure failure without code retry, approval routing, cancellation from every nonterminal stage, and terminal routes.

- [ ] **Step 2: Write failing node tests with injected fakes**

Assert each node updates only its owned fields; base commit is captured before Worktree creation; model cannot supply task/path/commit/config; patch/test results determine truth; model/network retry does not alter code retry; outputs are capped; every node rereads persisted cancellation and every side-effect node checks it immediately before its call; node boundaries emit only the exact M2 `EventType` values with bounded payloads.

- [ ] **Step 3: Write failing SQLite checkpoint test**

Compile with `AsyncSqliteSaver`, run a stable UUID thread to an injected mid-graph stop, recreate the graph/checkpointer, resume the same thread, and assert completed nodes are not repeated. Add the Review Focus crash case: forward `git apply --check` fails after a prior write, reverse check proves the exact patch is already present, `already_applied_ok=True` returns success without writing, and the dedupe key prevents a second `patch_applied` event; a patch that passes neither check remains a conflict.

- [ ] **Step 4: Run graph tests and verify missing modules**

Run: `python -m pytest tests/unit/graph tests/integration/test_graph_checkpoint.py -q`
Expected: FAIL during import.

- [ ] **Step 5: Implement graph state, nodes, and builder**

Wrap synchronous M1/model-independent calls with `asyncio.to_thread`; use `interrupt()` only for normalized `approval_required` payloads; compile with the supplied checkpointer; form the event/operation dedupe key as `task_id:stage:retry_count`; and use forward then reverse `git apply --check` to reconcile a patch-node replay. Do not add an M2 resume endpoint.

- [ ] **Step 6: Run graph tests**

Run: `python -m pytest tests/unit/graph tests/integration/test_graph_checkpoint.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/repopilot/graph src/repopilot/tools/patch.py tests/unit/graph tests/unit/tools/test_patch.py tests/integration/test_graph_checkpoint.py
git commit -m "feat: orchestrate repairs with persistent LangGraph"
```

### Task 8: Task service, single Worker, restart recovery, and cancellation

**Files:**
- Create: `src/repopilot/services/__init__.py`
- Create: `src/repopilot/services/task_service.py`
- Create: `src/repopilot/services/worker.py`
- Create: `tests/unit/services/test_task_service.py`
- Create: `tests/integration/test_worker.py`

**Interfaces:**
- Consumes: Task 2 repositories, Task 3 profile registry, Task 7 compiled graph, M1 repository/command validation.
- Produces: `TaskService.create_task(input: TaskInput) -> TaskRecord`, `get_task`, `cancel_task`; `TaskWorker.start()`, `stop()`, `run_once() -> bool`, and `run_forever()`.

- [ ] **Step 1: Write failing service validation tests**

Cover real Git repository validation, bounded non-empty request, secret rejection before persistence, permanent command denial before persistence, approval-required commands accepted for later safe interruption, profile allowlist, `max_retries` range `0..5`, UUID task/thread IDs, missing task, duplicate cancel, and terminal conflict.

- [ ] **Step 2: Write failing Worker/recovery tests**

Assert one task at a time, queued claim to running, original thread reuse for pre-existing running tasks, awaiting/terminal tasks ignored, graph success/failure/interrupt mapping, top-level exception to redacted `internal_error`, and clean stop. Simulate cancellation during an awaited Docker node and assert no later graph side effect executes.

- [ ] **Step 3: Run focused tests and verify missing services**

Run: `python -m pytest tests/unit/services tests/integration/test_worker.py -q`
Expected: FAIL during import.

- [ ] **Step 4: Implement service and Worker**

Use a single async loop and repository-backed claiming; do not create an in-memory-only queue. Initial Graph invocation passes full state, recovery invokes the original checkpoint using the same `thread_id`, and interrupts persist `awaiting_approval` plus one event. Stop uses an `asyncio.Event` and never abandons a database transaction.

- [ ] **Step 5: Run service and Worker tests**

Run: `python -m pytest tests/unit/services tests/integration/test_worker.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/repopilot/services tests/unit/services tests/integration/test_worker.py
git commit -m "feat: run and recover persistent tasks"
```

### Task 9: FastAPI lifecycle and REST endpoints

**Files:**
- Create: `src/repopilot/api/__init__.py`
- Create: `src/repopilot/api/schemas.py`
- Create: `src/repopilot/api/errors.py`
- Create: `src/repopilot/api/dependencies.py`
- Create: `src/repopilot/api/routes.py`
- Create: `src/repopilot/api/app.py`
- Create: `tests/integration/api/test_tasks.py`
- Create: `tests/integration/api/test_lifespan.py`

**Interfaces:**
- Consumes: Task 8 service/Worker and Task 2 database.
- Produces: `create_app(settings: AppSettings | None = None, *, overrides: AppOverrides | None = None) -> FastAPI` with `POST /tasks`, `GET /tasks/{task_id}`, `POST /tasks/{task_id}/cancel`, and `GET /health`.

- [ ] **Step 1: Write failing REST contract tests**

Assert `POST /tasks` returns `202` plus task snapshot; `GET` returns bounded fields and no assembled prompt/API key; cancel semantics map to `200`, `404`, and `409`; invalid repository, permanently denied command, profile, retry value, and a likely secret return `422`; an unallowlisted executable returns `202` and later becomes `awaiting_approval`; secret rejection leaves every business table empty; health reports service/database/Worker without probing a paid model.

- [ ] **Step 2: Write failing lifespan tests**

Assert app startup initializes migrations/checkpointer and starts exactly one Worker; shutdown stops it and closes the checkpointer connection; `LANGGRAPH_STRICT_MSGPACK` is set before saver construction.

- [ ] **Step 3: Run API tests and verify missing modules**

Run: `python -m pytest tests/integration/api/test_tasks.py tests/integration/api/test_lifespan.py -q`
Expected: FAIL during import.

- [ ] **Step 4: Implement dependency assembly, lifespan, and routes**

Keep construction injectable for tests. Map typed domain errors centrally, redact unknown exceptions, return background failures only through task state/events, and exclude complete Diff, logs, prompts, provider responses, and secrets from response models.

- [ ] **Step 5: Run REST tests**

Run: `python -m pytest tests/integration/api/test_tasks.py tests/integration/api/test_lifespan.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/repopilot/api tests/integration/api/test_tasks.py tests/integration/api/test_lifespan.py
git commit -m "feat: expose persistent task API"
```

### Task 10: Durable SSE event stream and reconnection

**Files:**
- Modify: `src/repopilot/api/routes.py`
- Create: `src/repopilot/api/sse.py`
- Create: `tests/integration/api/test_events.py`

**Interfaces:**
- Consumes: Task 2 `EventRepository` and Task 9 dependency injection.
- Produces: `GET /tasks/{task_id}/events`, `parse_last_event_id(value: str | None) -> int`, and `event_stream(task_id: str, after_id: int, ...) -> AsyncIterator[bytes]`.

- [ ] **Step 1: Write failing event serialization and reconnection tests**

Assert `text/event-stream`, event `id`/`event`/JSON `data`, strictly increasing IDs, `Last-Event-ID` sends only greater IDs, event IDs belonging to another task reveal nothing, missing task is `404`, malformed/negative/oversized header is `422`, heartbeat is an SSE comment, slow polling uses bounded batches, and a terminal event closes the stream.

- [ ] **Step 2: Write failing concurrent read/write test**

Keep a stream open, append an event through another SQLite connection, and assert it is delivered without database lock failure or unbounded buffering.

- [ ] **Step 3: Run event tests and verify endpoint absence**

Run: `python -m pytest tests/integration/api/test_events.py -q`
Expected: FAIL with 404/import error.

- [ ] **Step 4: Implement SSE streaming**

Use FastAPI `StreamingResponse`, query `task_id = ? AND id > ?` in batches of at most 100, close each read before sleeping, send bounded redacted payloads, issue periodic `: heartbeat` comments, and stop on disconnect or terminal event.

- [ ] **Step 5: Run event tests**

Run: `python -m pytest tests/integration/api/test_events.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/repopilot/api/routes.py src/repopilot/api/sse.py tests/integration/api/test_events.py
git commit -m "feat: stream durable task events"
```

### Task 11: Fake-model end-to-end repair, retries, restart, and safety

**Files:**
- Create: `tests/e2e/test_m2_fake_agent.py`
- Create: `tests/e2e/test_m2_restart.py`
- Create: `tests/e2e/test_m2_cancellation.py`
- Create: `tests/e2e/test_m2_approval.py`
- Create: `tests/helpers/m2.py`

**Interfaces:**
- Consumes: complete Tasks 1–10 stack and calculator fixture.
- Produces: reusable Fake scenario builders and M2 acceptance evidence; no production interface.

- [ ] **Step 1: Add first-patch success E2E test**

Create a temporary calculator Git repository, submit through HTTP with `fake`, wait through SSE, assert `succeeded`, Docker exit `0`, expected Diff in managed Worktree, original snapshot unchanged, and required event ordering.

- [ ] **Step 2: Add fail-then-fix and exhaustion tests**

Script Fake outputs so the first valid patch still fails tests and the repair succeeds; assert one `retry_scheduled`, `retry_count == 1`, and final success. Script persistent failure with `max_retries=2`; assert exactly three patch attempts and terminal `retry_exhausted`.

- [ ] **Step 3: Add restart and patch-reconciliation test**

Stop after the first patch has changed the Worktree but before the next checkpoint, reconstruct database/checkpointer/app, and assert the same `thread_id` resumes, the patch is not applied twice, events are not duplicated, and the task completes.

- [ ] **Step 4: Add cancellation and approval tests**

Request cancellation while a controlled Docker call is in progress, release it, and assert no subsequent model/patch/test/review call. Propose a deletion patch and an unallowlisted test command separately; assert `awaiting_approval`, `approval_required`, and no dangerous action.

- [ ] **Step 5: Run Docker E2E tests**

Run: `docker build -t repopilot-runner:m1 .`
Expected: image build succeeds.
Run: `python -m pytest tests/e2e -q`
Expected: PASS with Docker daemon running; no skip counts as acceptance.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e tests/helpers/m2.py
git commit -m "test: verify M2 autonomous repair lifecycle"
```

### Task 12: Real provider smoke tests, operator docs, and complete verification

**Files:**
- Create: `tests/smoke/test_deepseek.py`
- Create: `tests/smoke/test_qwen.py`
- Create: `.env.example`
- Modify: `README.md`

**Interfaces:**
- Consumes: complete M2 system.
- Produces: explicit `deepseek_smoke` and `qwen_smoke` verification commands and user-facing configuration/API documentation.

- [ ] **Step 1: Write provider smoke tests with safe skip gates**

DeepSeek runs only when `RUN_DEEPSEEK_SMOKE=1`, `DEEPSEEK_API_KEY`, and `DEEPSEEK_MODEL` exist. Qwen runs only when `RUN_QWEN_SMOKE=1`, `DASHSCOPE_API_KEY`, `QWEN_MODEL`, and `QWEN_BASE_URL` exist. Each requests one tiny `ChangePlan`, validates Pydantic output and nonnegative usage, and never prints credentials or raw headers.

- [ ] **Step 2: Run smoke tests without credentials**

Run: `python -m pytest tests/smoke -q`
Expected: both tests SKIP with a precise missing opt-in/config reason and make no network request.

- [ ] **Step 3: Document configuration and operation**

Update README from M1-only wording to truthful M1+M2 capability, architecture, startup command, REST/SSE examples, Fake/default behavior, retry semantics, cancellation delay, approval limitation, database location, DeepSeek/Qwen variable names, smoke commands, security boundaries, and M3/M4 roadmap. `.env.example` contains variable names and safe placeholders only, never a real key.

- [ ] **Step 4: Run the DeepSeek smoke test when explicitly configured**

Run: `python -m pytest -m deepseek_smoke tests/smoke/test_deepseek.py -q`
Expected: PASS when the user supplies a rotated valid key and model; otherwise leave the documented SKIP evidence and do not copy a key into any command, file, or log.

- [ ] **Step 5: Run the Qwen smoke test when explicitly configured**

Run: `python -m pytest -m qwen_smoke tests/smoke/test_qwen.py -q`
Expected: PASS when explicitly configured; otherwise SKIP because no key/config is present.

- [ ] **Step 6: Run complete security and regression verification**

Run: `python -m compileall -q src tests`
Expected: exit `0`.
Run: `python -m pytest -q`
Expected: all M1 and M2 non-provider tests PASS; provider tests may SKIP only for absent explicit opt-in/config.
Run: `git diff --check`
Expected: no output.
Run: `rg -l "sk-[A-Za-z0-9]{16,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|Bearer [A-Za-z0-9._-]{16,}" . --glob '!\.git/**'`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add tests/smoke .env.example README.md
git commit -m "docs: document M2 model operation"
```

### Task 13: Whole-branch review and acceptance record

**Files:**
- Create: `docs/verification/2026-10-06-repopilot-m2.md`
- Modify if findings require it: M2 source/tests/docs only

**Interfaces:**
- Consumes: all previous task commits and the M2 spec acceptance list.
- Produces: evidence mapping every M2 acceptance criterion to an exact command/result.

- [ ] **Step 1: Review the entire branch against the spec**

Inspect all changes from M1 commit `1d6f49b` through HEAD for security regressions, missing cancellation checks, retry off-by-one, leaked secrets, unsafe provider URLs, non-idempotent resume, API/event overexposure, and original-checkout mutation.

- [ ] **Step 2: Fix review findings with focused red-green tests**

For every accepted finding, first add a failing regression test, run it to prove the gap, implement the minimum fix, and rerun the focused suite. If there are no findings, record that explicitly without making a no-op code change.

- [ ] **Step 3: Rebuild and run final acceptance**

Run: `docker build -t repopilot-runner:m1 .`
Run: `python -m compileall -q src tests`
Run: `python -m pytest -q`
Run: `git diff --check`
Expected: Docker build and compile succeed, all required tests pass, provider smoke tests skip only when not explicitly configured, and whitespace check is empty.

- [ ] **Step 4: Write the verification record**

Record commit, platform, Docker image, exact commands, pass/skip counts, DeepSeek/Qwen smoke status, all 13 spec acceptance items, and any honest limitation. Do not include credentials, complete prompts, raw model output, or local environment dumps.

- [ ] **Step 5: Commit**

```bash
git add docs/verification/2026-10-06-repopilot-m2.md
git commit -m "docs: record RepoPilot M2 verification"
```
