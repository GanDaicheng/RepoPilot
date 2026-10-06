# RepoPilot M2 设计：单 Agent 修复闭环、持久化任务 API 与国产模型接入

**日期：** 2026-10-06

**状态：** 待用户审阅

**依赖：** M1 安全工具、Git Worktree 管理器与 Docker 测试执行器

**目标里程碑：** M2

## 1. 目标

M2 把 M1 的受控仓库工具组合成一个可运行、可恢复、可观察的单 Agent 代码修复服务。调用方提交真实 Git 仓库、自然语言需求和测试命令后，服务在独立 Worktree 中完成：

```text
需求校验
→ 仓库检索与摘要
→ 结构化方案
→ 补丁生成与应用
→ Docker 测试
→ 失败分析与自动修复
→ 只读审查
→ 结果报告
```

本里程碑提前接入 DeepSeek、千问及通用 OpenAI-compatible 模型，但仍保留确定性的 Fake Model，使默认测试和 CI 不消耗真实 Token。

本文件细化并覆盖总设计中“国产模型统一到 M4 接入”的时间安排；OpenAI Agents SDK、多 Agent、Handoff 与 Tracing 仍保留在 M4。

## 2. M2 范围

### 2.1 纳入范围

- FastAPI 任务创建、查询、取消、健康检查与 SSE 事件接口；
- SQLite 业务表、单进程后台 Worker 与 LangGraph checkpoint；
- 单 `RepoAgent` 的规划、补丁、失败分析和审查阶段；
- `ModelGateway` 对 Fake、DeepSeek、千问和自定义 OpenAI-compatible 端点的统一访问；
- Pydantic 结构化模型输出与一次格式修复；
- 测试失败后的有界自动修复循环；
- 取消、进程重启恢复、事件断线续传和敏感信息脱敏；
- Fake Model 端到端测试及显式开启的真实模型冒烟测试。

### 2.2 不纳入范围

- OpenAI Agents SDK、多 Agent、主管/开发/测试/审查 Handoff；
- MCP Server 与远程 MCP 传输；
- 审批决定提交和恢复接口；
- 评测数据集、聚合指标和完整产物报告；
- 自动 commit、merge、push 或修改原始工作目录；
- 多进程、多主机或分布式任务队列。

审批流程在 M2 只做到安全暂停：一旦 M1 工具返回 `approval_required`，任务进入 `awaiting_approval`，不执行危险操作。审批决定、`Command(resume=...)` 的 HTTP 接口和完整生命周期在 M3 实现。

## 3. 核心原则

1. **模型负责推理，应用负责权限与事实。** 模型可以提出方案和补丁，不能决定路径边界、测试是否通过、审批结果或任务状态。
2. **所有改动只发生在托管 Worktree。** 原仓库工作区、索引和当前分支不得改变。
3. **测试结论是确定性的。** 只有 Docker 测试进程退出码 `0` 表示通过。
4. **重试有界且分类。** 模型网络重试与代码修复重试分别计数。
5. **恢复不重复副作用。** 可恢复节点必须幂等；副作用节点执行前再次检查取消状态和既有结果。
6. **敏感信息不落库。** API Key、完整环境变量和未脱敏提示不得进入状态、事件或日志。

## 4. 总体架构

```text
FastAPI
   │
   ▼
TaskService ─────────────── SQLite tasks / events / model_calls
   │                                      │
   ▼                                      ▼
single-process Worker              LangGraph checkpointer
   │
   ▼
LangGraph state machine
   │
   ▼
RepoAgent ───── ModelGateway ───── Fake / DeepSeek / Qwen / Custom
   │
   ▼
M1 Tool Services ───── WorktreeManager ───── DockerTestRunner
```

职责边界：

- `FastAPI` 只处理协议、验证、错误映射与流式响应；
- `TaskService` 是任务和事件的业务入口；
- `Worker` 串行领取持久化任务并驱动 Graph；
- `LangGraph` 负责节点顺序、路由、checkpoint 和有界重试；
- `RepoAgent` 将各推理阶段映射为结构化模型调用；
- `ModelGateway` 隔离供应商差异、结构化解析、usage 与瞬时错误重试；
- M1 工具继续负责真实文件、Git、补丁、命令策略和 Docker 执行。

M2 不引入第二套文件或 Git 实现。

## 5. 建议目录

```text
src/repopilot/
├── api/
│   ├── app.py
│   ├── dependencies.py
│   ├── errors.py
│   ├── routes.py
│   └── schemas.py
├── agent/
│   ├── repo_agent.py
│   └── schemas.py
├── graph/
│   ├── builder.py
│   ├── nodes.py
│   ├── routing.py
│   └── state.py
├── models/
│   ├── gateway.py
│   ├── profiles.py
│   └── fake.py
├── persistence/
│   ├── database.py
│   ├── repositories.py
│   └── migrations.py
├── services/
│   ├── task_service.py
│   └── worker.py
└── ... M1 modules

tests/
├── unit/
├── integration/
├── e2e/
└── smoke/
```

模块名称可在实施计划中依据 M1 现有布局微调，但职责边界不变。

## 6. 任务状态与 Graph 状态

### 6.1 业务任务状态

```text
queued
running
awaiting_approval
succeeded
failed
cancelled
```

阶段由 Graph 状态和事件表示，不扩张业务状态枚举。允许的主要转换为：

```text
queued → running
queued → cancelled
running → succeeded | failed | cancelled | awaiting_approval
awaiting_approval → cancelled
```

所有终态不可再次执行。`awaiting_approval` 在 M2 中保持等待，不由 Worker 自动领取。

### 6.2 Graph 状态

```python
class RepoPilotState(TypedDict):
    task_id: str
    thread_id: str
    repo_path: str
    user_request: str
    test_command: str
    base_commit: str | None
    worktree_path: str | None
    repo_summary: str | None
    change_plan: dict[str, object] | None
    patch_text: str | None
    changed_files: list[str]
    test_exit_code: int | None
    test_output: str
    tests_passed: bool
    failure_analysis: dict[str, object] | None
    review_decision: dict[str, object] | None
    retry_count: int
    max_retries: int
    cancel_requested: bool
    pending_approval: dict[str, object] | None
    current_stage: str
    error_type: str | None
    error_message: str | None
```

约束：

- `thread_id` 使用稳定 UUID，并与 `task_id` 一一对应；恢复必须复用原值；
- `test_output`、仓库摘要和 Diff 都有字节上限；
- checkpoint 不保存 API Key、完整环境变量或未脱敏 provider 响应；
- SQLite 业务代码不读取或修改 checkpointer 的内部表。

## 7. Graph 流程

```text
validate_request
→ capture_base_commit
→ create_worktree
→ summarize_repository
→ plan_change
→ propose_patch
→ apply_patch
→ run_tests
   ├── passed → review_change
   │              ├── approved → success_report
   │              └── critical 且可重试 → propose_fix
   └── failed → inspect_failure
                  ├── 可修复且未达上限 → propose_fix
                  └── 不可修复或达到上限 → failed_report

propose_fix → apply_patch → run_tests
```

各节点只承担一个可测试职责：

- `validate_request`：校验仓库、需求、测试命令、模型 profile 和重试上限；
- `capture_base_commit`：固定起始提交，后续不跟随原仓库变化；
- `create_worktree`：复用 M1 `WorktreeManager` 创建或识别任务 Worktree；
- `summarize_repository`：通过只读工具构造有界上下文；
- `plan_change`：获取并校验 `ChangePlan`；
- `propose_patch` / `propose_fix`：获取并校验 `PatchProposal`；
- `apply_patch`：由应用调用 M1 补丁工具，不把写权限交给模型；
- `run_tests`：由应用调用 M1 Docker runner；
- `inspect_failure`：先用确定性错误提取，再获取 `FailureAnalysis`；
- `review_change`：基于 Diff、Git 状态和测试摘要获取 `ReviewDecision`；
- 报告节点：写入最终状态和最终事件。

每个节点开始时检查持久化取消标志；`create_worktree`、`apply_patch`、`run_tests` 等副作用节点在调用工具前再次检查。取消已请求时直接路由到 `cancelled`，不再产生外部副作用。

`retry_count` 表示首次补丁之后已经安排的修复轮数。初始值为 `0`；只有确定要进入 `propose_fix` 时才加一。`max_retries=2` 表示最多允许初始补丁加两次修复，共三次补丁尝试。模型传输退避和格式修复均不改变此计数。

## 8. RepoAgent 与结构化输出

`RepoAgent` 是单 Agent 的应用层门面，不拥有任务生命周期。它提供四个阶段方法：

```text
plan(request, repo_context) -> ChangePlan
propose_patch(plan, repo_context) -> PatchProposal
analyze_failure(plan, diff, test_summary) -> FailureAnalysis
review(plan, diff, test_summary) -> ReviewDecision
```

核心 Pydantic 输出：

- `ChangePlan`：目标、相关文件、实施步骤、风险、建议测试；
- `PatchProposal`：统一 Diff、变更说明、预期修改文件；
- `FailureAnalysis`：根因、证据、是否可修复、修复策略、建议读取文件；
- `ReviewDecision`：`approved`、`critical_findings`、`warnings`、结论。

所有模型输出都必须通过 Pydantic 校验。解析失败时，`ModelGateway` 只携带校验错误和原输出进行一次格式修复，不追加仓库工具权限；第二次仍失败则该模型步骤以 `model_output_invalid` 失败。

### 8.1 工具权限

模型仅能请求或消费以下只读工具结果：

```text
list_files
search_code
read_file
git_status
git_diff
inspect_error
```

每个推理阶段最多允许四轮只读工具请求；每轮由应用校验参数、调用 M1 工具并返回有界结果。达到上限后模型必须提交该阶段的结构化最终输出，否则以 `model_output_invalid` 结束。

模型不得直接调用：

```text
apply_patch
run_tests
Worktree 创建或清理
任意 shell 命令
审批、取消或状态修改
```

任务 ID、真实路径、基础提交、审批结果和 Docker 配置由应用注入，不能采用模型生成值。

## 9. ModelGateway

### 9.1 Provider profile

```text
MODEL_PROVIDER=fake | deepseek | qwen | custom
MODEL_NAME=<provider model id>
MODEL_BASE_URL=<optional OpenAI-compatible endpoint>
```

固定密钥环境变量：

```text
DeepSeek → DEEPSEEK_API_KEY
Qwen    → DASHSCOPE_API_KEY
Custom  → MODEL_API_KEY
```

`MODEL_API_KEY_ENV` 不由 HTTP 请求提供，防止调用方指向任意服务端环境变量。默认 profile 在进程启动时加载；任务只引用允许列表中的 profile 名称。

### 9.2 实现策略

M2 使用 OpenAI Python SDK 的兼容客户端作为传输层，以 `base_url`、`model` 和受控环境变量切换供应商。首版使用 Chat Completions 兼容路径并在本地做 Pydantic 校验，避免假设所有兼容端点完整支持 Responses API 或严格 JSON Schema。

- `fake`：脚本化、确定性输出，可模拟首次成功、失败后修复、连续失败和格式错误；
- `deepseek`：读取 `DEEPSEEK_API_KEY`，默认端点由内置 profile 提供，可显式覆盖；
- `qwen`：读取 `DASHSCOPE_API_KEY`，端点按百炼地域配置显式提供；
- `custom`：必须显式提供 HTTPS `base_url`、模型名和受控 `MODEL_API_KEY`。

模型 ID 不写死为“最新模型”，由运行配置指定并记录在 `model_calls` 中。

业务表保存经过长度限制的 `user_request`，因为重启恢复需要原始任务意图；发送给 provider 的完整系统提示、工具对话和格式修复提示不落库。创建任务时若检测到常见 API Key、私钥头或令牌格式，直接返回验证错误，避免把凭据当成需求保存。

### 9.3 重试与 usage

- 超时、连接失败、429 和可恢复 5xx 使用有界指数退避；
- provider 瞬时重试不增加 `retry_count`；
- 补丁无法应用、测试失败或关键审查问题才增加代码修复次数；
- 每次调用记录 provider、model、阶段、耗时、输入/输出 Token、结果类型和脱敏错误；
- 不保存完整提示、完整响应或授权请求头。

## 10. SQLite 持久化

### 10.1 业务表

`tasks`：

```text
id, thread_id, repo_path, user_request, test_command, model_profile,
status, current_stage, base_commit, worktree_path,
retry_count, max_retries, cancel_requested,
error_type, error_message, created_at, started_at, finished_at, updated_at
```

`events`：

```text
id INTEGER PRIMARY KEY AUTOINCREMENT,
task_id, event_type, stage, payload_json, created_at
```

`model_calls`：

```text
id, task_id, stage, provider, model,
input_tokens, output_tokens, total_tokens,
duration_ms, attempt_count, outcome, error_type, created_at
```

迁移采用项目内显式 schema version。连接启用外键、busy timeout 和 WAL；同一进程中的写操作通过短事务完成，不在事务内等待模型或 Docker。

### 10.2 Checkpoint

LangGraph 使用文件型 SQLite checkpointer，并通过同一持久化配置创建。业务表和 checkpoint 可以位于同一数据库文件，但连接与表由各自适配器管理。

`thread_id` 是 checkpoint 的稳定游标。Worker 恢复任务时不构造新 Graph 输入覆盖旧状态，而是使用原 `thread_id` 查询并继续现有 checkpoint。

### 10.3 状态与事件一致性

业务状态变更和对应事件必须在同一业务事务写入。Graph checkpoint 与业务表不能跨两个独立组件实现真正的单事务，因此采用可恢复的状态协调：

1. 节点副作用使用任务 ID、阶段和尝试次数形成幂等键；
2. 节点成功后先保存可重放结果，再更新业务阶段；
3. 重启时对照 checkpoint 和业务阶段，重复进入节点时复用已完成结果；
4. 终态任务永不重新排队。

## 11. Worker 与重启恢复

M2 只有一个进程内 Worker，同一时刻串行执行任务：

1. 原子领取最早的 `queued` 任务并置为 `running`；
2. 使用任务的稳定 `thread_id` 驱动 Graph；
3. 在每个节点边界刷新任务阶段和心跳时间；
4. Graph 完成、中断或异常时写入确定状态；
5. 再领取下一任务。

服务启动恢复规则：

- `queued`：保持或重新进入队列；
- `running`：复用原 `thread_id` 和 checkpoint 恢复；
- `awaiting_approval`：保持等待，不自动执行；
- `succeeded`、`failed`、`cancelled`：不重复执行。

节点发生未预期异常时，Worker 捕获顶层异常、写入脱敏 `internal_error` 和 `task_failed`，不能遗留永久 `running`。

## 12. FastAPI 接口

### 12.1 `POST /tasks`

请求字段：

```text
repo_path
user_request
test_command
model_profile
max_retries
```

服务验证后立即返回 `202 Accepted` 和任务快照，不等待模型或测试完成。仓库路径必须是服务允许访问的本地 Git 仓库；测试命令仍由 M1 策略校验。

### 12.2 `GET /tasks/{task_id}`

返回状态、当前阶段、重试次数、时间、脱敏错误和最终摘要。默认不返回完整测试日志、提示或 Diff。

### 12.3 `POST /tasks/{task_id}/cancel`

将 `cancel_requested` 原子置为真并产生取消请求事件：

- `queued` 任务可直接进入 `cancelled`；
- `running` 任务在下一安全节点边界停止；
- `awaiting_approval` 任务可直接进入 `cancelled`；
- 对终态任务或重复取消返回 `409 Conflict`；
- M2 不强杀已经启动的 Docker 子进程，执行中的调用完成后不得再启动下一副作用。

### 12.4 `GET /tasks/{task_id}/events`

使用 `text/event-stream` 返回持久化事件。支持标准 `Last-Event-ID` 请求头：查询条件为 `task_id = ? AND id > ?`，因此重连只发送新事件。

### 12.5 `GET /health`

返回服务、数据库和 Worker 就绪状态，不探测付费模型、不暴露配置值。

### 12.6 HTTP 错误

- `404`：任务不存在；
- `409`：终态操作、重复取消或状态冲突；
- `422`：请求字段、仓库、模型 profile 或命令策略校验失败；
- `500`：请求处理层未预期错误，响应与日志必须脱敏。

后台任务错误不改变已经返回的 HTTP 响应；它们将任务置为 `failed` 并通过查询与 SSE 暴露。

## 13. SSE 事件模型

事件名：

```text
task_created
task_started
stage_started
tool_completed
model_completed
patch_applied
test_completed
retry_scheduled
approval_required
task_succeeded
task_failed
task_cancelled
```

规则：

- `events.id` 是数据库生成的全局单调整数，并作为 SSE `id`；
- 每条事件包含 `task_id`、阶段、时间和有界摘要；
- 无新事件时定期发送 SSE 注释 heartbeat，避免代理错误关闭连接；
- 终态事件发送后正常关闭流；
- 客户端过慢时从 SQLite 按批读取，不在内存无限排队；
- 事件不包含密钥、完整代码、完整 Diff、完整提示或完整测试日志。

SSE 是任务进度协议，不是 M3 的 MCP 传输。

## 14. 审批行为

如果补丁删除文件、Worktree 清理或命令策略返回 `approval_required`：

1. 节点不执行该操作；
2. Graph 保存 pending action 的规范化摘要；
3. 任务状态变为 `awaiting_approval`；
4. 写入 `approval_required` 事件；
5. Worker 停止驱动该任务。

pending action 不包含密钥或任意 shell 字符串。M2 没有批准接口，因此不会绕过 M1 安全策略；M3 再增加审批记录、过期机制和带同一 `thread_id` 的恢复。

## 15. 错误分类

```text
validation_error
policy_denied
approval_required
workspace_error
tool_error
patch_invalid
patch_apply_failed
test_failed
test_timeout
model_transport_error
model_output_invalid
retry_exhausted
cancelled
internal_error
```

测试输出、provider 响应和异常栈在进入数据库与事件前统一脱敏。用户可见错误包含类型、阶段和短消息，不回显环境变量、请求头或完整本地路径之外的系统信息。

## 16. 测试策略

### 16.1 单元测试

- 任务状态转换和终态保护；
- Graph 路由、重试计数和取消检查；
- 四种 Pydantic 输出校验及一次格式修复；
- provider profile、环境变量映射和错误分类；
- 事件 ID、`Last-Event-ID` 过滤和脱敏；
- SQLite repository 的事务与幂等写入。

### 16.2 集成测试

- FastAPI 创建、查询、取消、健康检查；
- SQLite Worker 领取和服务重启恢复；
- LangGraph SQLite checkpoint 使用原 `thread_id` 恢复；
- SSE 断线重连不重复、不遗漏已提交事件；
- M1 Worktree、补丁和 Docker runner 组合调用。

### 16.3 Fake Model 端到端测试

1. 首个补丁测试通过并成功；
2. 首次测试失败，分析后生成修复补丁并成功；
3. 连续失败，达到 `max_retries` 后以 `retry_exhausted` 结束；
4. 运行中取消，后续副作用不再发生；
5. Worker 重启后使用原 checkpoint 继续；
6. SSE 使用 `Last-Event-ID` 恢复且不重复；
7. 危险操作进入 `awaiting_approval` 且未执行；
8. 原仓库内容、索引和当前分支保持不变。

### 16.4 真实模型冒烟测试

- `pytest -m deepseek_smoke` 仅在显式启用且存在 `DEEPSEEK_API_KEY` 时运行；
- `pytest -m qwen_smoke` 仅在显式启用且存在 `DASHSCOPE_API_KEY` 时运行；
- 缺少密钥时明确 skip，而不是失败或使用 Fake 冒充；
- 冒烟测试使用极小结构化任务，验证连通性、Pydantic 输出和 usage；
- 普通测试、默认 CI 和本地全量测试不会调用付费模型。

## 17. M2 验收标准

M2 完成必须同时满足：

1. Fake Model 首次补丁成功场景通过；
2. Fake Model 首次失败、自动修复后成功场景通过；
3. 连续失败在重试上限停止；
4. 取消请求阻止后续副作用；
5. 服务重启后从原 `thread_id` checkpoint 恢复；
6. SSE 断线续传无重复事件；
7. 危险操作安全进入 `awaiting_approval`；
8. DeepSeek 真实冒烟测试在提供环境变量时通过；
9. 千问真实冒烟测试在提供环境变量时通过，未提供时明确跳过；
10. M1 的 70 个测试继续通过；
11. 新增测试全部通过，Docker 端到端测试在验收环境真实运行；
12. 原仓库未被修改，系统未自动 commit、merge 或 push；
13. 数据库、事件、日志和代码库中均无 API Key。

真实模型冒烟测试属于本地显式验收，不作为公开 CI 的必需步骤。

## 18. 已知取舍与后续

- 单 Worker 简化 SQLite 并发与恢复语义，但吞吐量有限；分布式队列不属于个人项目首版目标。
- Graph checkpoint 和业务事件无法共享跨组件原子提交，M2 用幂等键与重放协调保证最终一致。
- M2 只能暂停危险操作，不能在线批准；M3 补齐审批表、恢复接口和 MCP。
- M2 单 Agent 保持阶段化结构，M4 可将各推理阶段替换为 Agents SDK 专职 Agent 与 Handoff，而不改动外层任务生命周期。
- 真实 provider 的兼容程度可能不同，因此 M2 依赖本地 Pydantic 校验，不把严格 JSON Schema 当作共同能力。

## 19. 官方参考

- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph interrupts: <https://docs.langchain.com/oss/python/langgraph/interrupts>
- FastAPI custom/streaming responses: <https://fastapi.tiangolo.com/advanced/custom-response/>
- DeepSeek API: <https://api-docs.deepseek.com/>
- 阿里云百炼千问首次调用: <https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen>
