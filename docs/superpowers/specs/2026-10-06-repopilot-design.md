# RepoPilot 代码仓库智能开发 Agent 设计规格

**日期：** 2026-10-06  
**状态：** 已完成对话设计，等待书面规格复核  
**目标读者：** 项目实现者、代码审查者与面试评估者

## 1. 项目目标

RepoPilot 是一个能够对真实 Git 代码仓库执行受控开发任务的 Agent 系统。用户提供仓库路径、自然语言需求和测试命令后，系统在独立 Git Worktree 中完成代码检索、方案制定、代码修改、测试、失败分析、自动修复、代码审查和报告生成。

系统不自动合并、不修改原始工作目录、不执行 `git push`。第一版先证明单 Agent 的“修改—测试—修复”闭环，后续再加入 MCP 和 OpenAI Agents SDK 多 Agent Handoff。最终实现必须足以真实支撑以下简历技术栈：

> Python、OpenAI Agents SDK、LangGraph、MCP、FastAPI、Docker、Git

最终版本同时支持千问、DeepSeek 等 OpenAI-compatible 模型端点，不把 OpenAI 模型或 OpenAI API 写死。

## 2. 设计原则

1. **确定性程序控制流程。** 测试是否通过、是否超过重试上限、是否需要审批等关键判断由程序完成，而不是由模型自行声称。
2. **模型只负责需要推理的部分。** 需求理解、规划、代码生成、错误分析和审查使用模型；测试执行、Git状态、路径校验和审批策略使用普通程序。
3. **先隔离，再操作。** 从代码检索开始，所有任务操作均发生在固定基础提交对应的 Worktree 中。
4. **安全默认拒绝。** 路径逃逸、任意 Shell、宿主机敏感挂载和推送远端永久禁止；危险但合理的操作必须暂停审批。
5. **可恢复、可审计。** LangGraph checkpoint 保存恢复位置，SQLite 业务表保存状态、事件、审批、指标与产物索引。
6. **分阶段交付。** 每个里程碑都必须产生可运行、可测试、可演示的功能，不提前搭建空壳多 Agent。

## 3. 范围

### 3.1 包含

- 八个核心代码仓库工具；
- Git Worktree 生命周期；
- Docker 隔离测试；
- 单 Agent 修改与自动修复循环；
- LangGraph 状态机、SQLite checkpoint 与人工中断；
- FastAPI 任务、状态、审批、取消、产物和 SSE 接口；
- MCP stdio 与 Streamable HTTP 工具服务器；
- 千问、DeepSeek、通用 OpenAI-compatible 和 Fake Model；
- 主管、开发、测试分析、审查 Agent 及 Handoff；
- 本地事件追踪、Agents SDK Tracing 接口和评测系统；
- Markdown 与 JSON 最终报告。

### 3.2 不包含

- 自动合并或修改目标仓库当前分支；
- 自动 `git commit` 目标仓库改动；
- 自动 `git push` 或创建 Pull Request；
- 通用任意 Shell；
- Web 前端；
- 多租户、集群调度或生产级分布式队列；
- 任意语言依赖环境的自动安装。未知依赖安装必须审批。

## 4. 分阶段架构

项目按四个里程碑实施：

### M1：安全工具、Worktree 与 Docker 测试

实现八个普通 Python 工具、路径边界、安全策略、Git Worktree 和 Docker 测试执行器，并提供单元与集成测试。

### M2：单 Agent、LangGraph、SQLite 与 FastAPI

实现一个 `RepoAgent`，由 LangGraph 驱动规划、修改、测试、失败分析、修复、审查和报告。加入 SQLite 持久化、任务 API 和 SSE。

### M3：审批、MCP、评测与报告

完善 Human-in-the-loop、安全审批、取消与恢复；将成熟工具包装为 MCP Server；加入标准任务集、指标聚合和可复现实验报告。

### M4：Agents SDK 多 Agent、Handoff 与 Tracing

把需要推理的节点拆为主管、开发、测试分析和审查 Agent，通过 OpenAI Agents SDK Handoff 分派任务。LangGraph继续负责持久化生命周期和确定性路由。

## 5. 总体组件

```text
FastAPI / SSE
      │
      ▼
Task Service ───── SQLite 业务表与产物索引
      │
      ▼
LangGraph：生命周期、路由、重试、暂停、恢复
      │
      ├── M2：单 RepoAgent
      └── M4：Agents SDK 主管与专职 Agent Handoff
                    │
                    ▼
               ModelGateway
          ┌─────────┼─────────┐
          ▼         ▼         ▼
        千问      DeepSeek    Fake
                    │
                    ▼
       Tool Services / MCP Adapters
                    │
                    ▼
         Git Worktree + Docker Test
```

LangGraph和Agents SDK不承担相同职责：LangGraph是外层持久化状态机，Agents SDK是内层模型协作运行时。测试工具和策略引擎始终独立于模型。

## 6. 目录结构

采用 `src` 布局，避免顶层 `tools`、`mcp` 等名称与第三方包冲突：

```text
repopilot/
├── src/repopilot/
│   ├── api/
│   │   ├── app.py
│   │   ├── routes.py
│   │   ├── schemas.py
│   │   └── sse.py
│   ├── graph/
│   │   ├── state.py
│   │   ├── workflow.py
│   │   ├── nodes.py
│   │   └── routing.py
│   ├── agents/
│   │   ├── single.py
│   │   ├── supervisor.py
│   │   ├── developer.py
│   │   ├── test_analyst.py
│   │   └── reviewer.py
│   ├── models/
│   │   ├── gateway.py
│   │   ├── profiles.py
│   │   └── fake.py
│   ├── tools/
│   │   ├── filesystem.py
│   │   ├── search.py
│   │   ├── git.py
│   │   ├── patch.py
│   │   └── tests.py
│   ├── security/
│   │   ├── paths.py
│   │   ├── commands.py
│   │   ├── policy.py
│   │   └── approvals.py
│   ├── workspace/
│   │   └── worktree.py
│   ├── persistence/
│   │   ├── database.py
│   │   ├── repositories.py
│   │   └── events.py
│   ├── reporting/
│   │   ├── artifacts.py
│   │   └── report.py
│   ├── mcp_server/
│   │   └── server.py
│   ├── evals/
│   │   └── evaluator.py
│   └── config.py
├── evals/
│   ├── tasks.json
│   └── fixtures/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── docs/superpowers/
├── Dockerfile
├── compose.yaml
└── pyproject.toml
```

运行时数据保存在可配置的 `REPOPILOT_DATA_DIR` 中，该目录不属于任何目标仓库。

## 7. 核心状态与状态机

```python
class RepoPilotState(TypedDict):
    task_id: str
    repo_path: str
    base_commit: str
    worktree_path: str
    user_request: str
    test_command: str
    repo_summary: str
    implementation_plan: list[str]
    changed_files: list[str]
    test_output: str
    failure_summary: str
    tests_passed: bool
    review_findings: list[dict[str, object]]
    retry_count: int
    max_retries: int
    pending_approval: dict[str, object] | None
    tool_metrics: dict[str, int]
    token_usage: dict[str, int]
    status: str
    error: str | None
```

`test_output`只保存大小受限的最近输出，完整日志作为产物保存。状态中不得保存API密钥或未经脱敏的环境变量。

状态枚举为：

```text
queued
preparing
planning
coding
testing
fixing
reviewing
awaiting_approval
succeeded
failed
cancelled
```

主流程：

```text
validate_request
→ capture_base_commit
→ create_worktree
→ summarize_repository
→ plan_change
→ apply_change
→ run_tests
   ├── exit_code == 0 → review_change
   └── exit_code != 0
       ├── retry_count < max_retries → inspect_error → fix_change → run_tests
       └── retry_count >= max_retries → failed_report
→ review_change
   ├── critical 且仍可重试 → fix_change
   ├── critical 且不可重试 → failed_report
   └── 无 critical → success_report
```

测试通过仅由退出码 `0` 表示。测试超时、容器启动失败和信号终止均视为失败，并记录不同错误类型。

任意节点在策略要求时调用 LangGraph `interrupt()`，同一 `thread_id` 通过 `Command(resume=...)` 恢复。取消请求在每个有副作用的节点前检查，并转入 `cancelled`。

## 8. 八个核心工具

1. `list_files`：列出Worktree内受限制深度和数量的目录结构，忽略 `.git` 与配置中的大目录。
2. `search_code`：使用 `rg` 的固定参数模板搜索文本，限制结果数量和单条长度。
3. `read_file`：读取Worktree内文本文件，支持行范围并限制最大字节数。
4. `apply_patch`：解析统一Diff，只允许相对路径；应用前验证全部目标，文件删除触发审批。
5. `git_diff`：使用固定Git参数输出未提交Diff，可选统计摘要。
6. `git_status`：使用 `--porcelain=v2` 获取机器可解析状态。
7. `run_tests`：在Docker中以参数数组执行已批准测试命令，返回退出码、输出、耗时和超时标记。
8. `inspect_error`：确定性提取失败测试名、异常类型、文件位置和尾部日志，供Agent进一步分析。

所有工具返回结构化结果，包含 `ok`、`data`、`error_code`、`message`、`duration_ms` 和审计元数据。MCP包装器仅做协议转换，不复制工具实现。

## 9. Worktree与原仓库保护

任务创建时解析目标仓库根目录并读取当前 `HEAD` 作为 `base_commit`。Worktree创建在 `REPOPILOT_DATA_DIR/worktrees/<repo-hash>/<task-id>`，不嵌套在目标仓库内。任务分支使用内部名称 `repopilot/<task-id>`，但不切换原仓库当前分支。

Agent从不获得原仓库写权限。所有文件工具只接收相对于Worktree的路径。任务报告记录开始和结束时原仓库的提交与状态摘要，用于证明原始工作目录未被RepoPilot修改。

默认保留Worktree供用户检查。清理Worktree属于文件删除操作，必须由用户显式请求并批准；清理前保存Diff、日志和报告。

## 10. 安全策略

操作分为三类：

### 10.1 自动允许

- Worktree内的读取、列举和搜索；
- 固定模板的 `git status` 与 `git diff`；
- Worktree内非删除补丁；
- 白名单内测试命令；
- 读取任务自身的日志和产物。

### 10.2 必须审批

- 删除文件；
- 安装或升级依赖；
- 临时开放测试容器网络；
- 不在白名单中的测试可执行程序；
- 清理Worktree；
- 策略明确标记的其他高风险操作。

### 10.3 永久拒绝

- `git push`；
- 写入原仓库或Worktree外部；
- `git reset --hard`、强制切换或覆盖用户数据；
- 未解析的Shell脚本、管道、重定向和命令拼接；
- Docker `--privileged`；
- 挂载宿主机根目录、用户敏感目录或Docker Socket；
- 绕过审批后替换已批准参数。

路径验证先规范化路径，再解析已有父目录与符号链接，最后验证结果仍位于Worktree根目录。`apply_patch`在产生任何写入前验证补丁中的全部路径，避免部分应用后才发现逃逸。

审批记录包含审批ID、任务ID、动作类型、规范化参数、风险说明、创建时间、过期时间和一次性决策。恢复时再次比较实际参数与审批记录，不一致则拒绝。

## 11. Docker测试执行

默认测试容器：

- 使用Python 3.12基础镜像；
- 非root用户；
- Worktree是唯一可写宿主机挂载；
- 网络默认关闭；
- 根文件系统只读，`/tmp`使用临时文件系统；
- 删除额外Linux capabilities并启用 `no-new-privileges`；
- 配置CPU、内存、进程数和墙钟超时；
- 不挂载SSH、云凭据、用户目录或Docker Socket。

测试命令用参数数组执行，不使用 `shell=True`。首版默认允许 `pytest` 与 `python -m pytest`；额外命令可由项目配置扩展或进入审批。包含 `&&`、`|`、`;`、重定向或命令替换的输入直接拒绝。

需要依赖安装时先中断审批。批准后使用单独、受限的准备步骤构建或缓存镜像；正式测试阶段重新关闭网络。API密钥不传入测试容器。

## 12. 模型接入

`ModelGateway`统一屏蔽供应商差异。配置包含：

```text
MODEL_PROVIDER=qwen | deepseek | custom | fake
MODEL_NAME=<provider model id>
MODEL_BASE_URL=<OpenAI-compatible endpoint>
MODEL_API_KEY_ENV=<environment variable name>
MODEL_API_MODE=chat_completions | responses
```

支持：

- 千问：阿里云百炼OpenAI-compatible端点；
- DeepSeek：DeepSeek OpenAI-compatible端点；
- Custom：任意通过能力测试的兼容端点；
- Fake：离线、确定性的测试模型。

模型名不硬编码为某个“最新”版本。启动时根据profile构建客户端，密钥只从指定环境变量读取。

系统提供能力探测，验证工具调用、流式输出、JSON结构化输出和usage数据。若端点不支持Responses API则使用Chat Completions。若不支持严格JSON Schema，输出必须经Pydantic校验；格式错误允许一次仅针对格式的修复，仍失败则明确终止当前模型步骤。

M4通过Agents SDK的自定义OpenAI-compatible客户端或模型提供器接入。模型供应商与追踪导出器解耦：没有OpenAI追踪凭据时仍记录本地trace，不影响千问或DeepSeek执行。

## 13. Agent职责与Handoff

### 13.1 M2单Agent

`RepoAgent`完成规划、改动生成、失败分析和审查。LangGraph仍将这些行为拆成不同节点与结构化输出，便于M4替换，而不是让Agent运行一个不可观察的无限循环。

### 13.2 M4专职Agent

- **主管Agent：** 理解需求、选择专职Agent、整理阶段输入；不修改文件。
- **开发Agent：** 检索代码、生成补丁、根据反馈修复；只能使用Worktree工具。
- **测试分析Agent：** 分析确定性测试工具产生的失败日志；不运行测试、不决定通过与否。
- **审查Agent：** 只读Diff、状态和日志，输出 `critical`、`warning`、`suggestion`；默认无写权限。

每个LangGraph推理阶段启动一次Agents SDK run，入口为主管Agent。主管根据当前阶段Handoff给唯一合适的专职Agent。专职Agent产生结构化最终输出，应用层将结果写回Graph状态和SQLite；下一阶段使用最新状态重新进入主管Agent。

Handoff元数据包含任务ID、阶段、原因和输入摘要。已有应用状态通过本地run context传递，不让模型伪造路径、权限或审批结果。

工具权限：

```text
主管：list_files、search_code、read_file
开发：list_files、search_code、read_file、apply_patch、git_diff、git_status
测试分析：inspect_error、read_file
审查：git_diff、git_status、read_file
```

`run_tests`只由LangGraph的确定性测试节点调用。

## 14. 持久化与事件

SQLite业务表包括：

- `tasks`：请求、状态、基础提交、重试计数、时间和最终结论；
- `events`：状态变化、节点、工具、模型、Handoff、测试和系统事件；
- `approvals`：动作、规范化参数、风险、决策和有效期；
- `artifacts`：产物类型、路径、摘要、大小和校验值；
- `metrics`：任务与评测聚合指标。

LangGraph SQLite checkpointer使用同一数据库文件或独立文件均可，但通过单一持久化配置创建。业务代码不直接依赖checkpointer内部表结构。

首版是单进程、单Worker。服务启动时扫描非终态任务：`awaiting_approval`保持等待，其余可恢复任务重新排队并用原 `thread_id` 继续。SQLite事务保证状态更新和业务事件一致写入；外部产物采用先写临时文件、再原子重命名的方式发布。

## 15. FastAPI与SSE

核心接口：

```text
POST /tasks
GET  /tasks/{task_id}
GET  /tasks/{task_id}/events
POST /tasks/{task_id}/approvals
POST /tasks/{task_id}/cancel
GET  /tasks/{task_id}/artifacts
POST /tasks/{task_id}/cleanup
```

`POST /tasks`验证仓库、请求、测试命令、重试上限和模型profile后立即返回任务ID。后台执行器从持久化队列取任务。重复取消、重复审批和对终态任务操作返回明确的冲突错误，不重复产生副作用。

SSE从SQLite事件表读取，事件有单调递增ID。客户端通过 `Last-Event-ID` 重连后继续消费。事件至少包括：

```text
task.created
task.status_changed
workspace.created
agent.started
model.completed
handoff.completed
tool.called
test.completed
approval.required
approval.resolved
task.succeeded
task.failed
task.cancelled
```

SSE不传输密钥、完整模型提示或未脱敏环境变量。超长工具输出只发送摘要和产物引用。

## 16. MCP

八个工具首先实现为普通Python服务。MCP Server通过薄适配器注册这些服务，不复制路径校验、安全策略或审计逻辑。

M3先提供stdio传输，供本地Agents SDK进程安全使用；随后提供Streamable HTTP。新实现不使用已被Streamable HTTP取代的旧MCP SSE传输。FastAPI的SSE仅用于任务进度，不是MCP传输。

MCP调用与Python内部调用产生相同的工具事件和指标。角色权限在提供工具清单和执行策略两层同时校验，不能通过直接构造MCP请求绕过。

## 17. 产物与报告

```text
artifacts/<task-id>/
├── plan.json
├── changes.patch
├── test-attempt-01.log
├── test-attempt-02.log
├── review.json
├── trace.jsonl
├── report.json
└── report.md
```

报告包含需求、计划、基础提交、Worktree、修改文件、完整Diff、每次测试的命令/退出码/摘要、修复次数、审查结果、审批记录、工具调用、模型与Token、耗时、估算成本以及最终状态和原因。

日志和trace在写入前对常见API密钥格式、Authorization头和配置中的敏感字段进行脱敏。产物路径始终位于任务产物目录。

## 18. 错误处理

错误按类型记录：

- `validation_error`：无效仓库、请求或配置；
- `policy_denied`：永久禁止操作；
- `approval_rejected`：用户拒绝；
- `workspace_error`：Worktree创建或验证失败；
- `tool_error`：工具自身失败；
- `test_failed`：测试正常运行但退出码非零；
- `test_timeout`：达到墙钟超时；
- `model_error`：供应商、限流或结构化输出失败；
- `retry_exhausted`：达到自动修复上限；
- `cancelled`：用户取消；
- `internal_error`：未预期异常。

瞬时模型错误可按受限指数退避重试，默认不计入代码修复次数；代码或测试失败计入 `retry_count`。任何异常都必须产生终态或可恢复的等待状态，不能让任务长期停留在模糊的“运行中”。

## 19. 测试策略

### 19.1 单元测试

覆盖路径逃逸、符号链接、命令解析、策略分类、补丁验证、错误提取、退出码判断、路由、重试、状态转换、脱敏和指标计算。

### 19.2 集成测试

使用临时Git仓库验证Worktree创建、修改、Diff、原仓库保护、清理审批、SQLite恢复、FastAPI接口、SSE重连、MCP in-process调用和报告产物。

Docker测试标记为集成测试；开发环境未启动Docker daemon时明确跳过并说明原因，项目完成验收和CI环境必须真实执行，不能用跳过结果替代通过。

### 19.3 端到端测试

Fake Model与固定fixture覆盖：

1. 首次改动后测试通过；
2. 第一次测试失败，自动修复后通过；
3. 连续失败，达到上限停止；
4. 删除文件触发暂停，批准后继续；
5. 用户拒绝审批后安全结束；
6. 服务重启后从checkpoint恢复；
7. 原始工作目录内容与状态未被RepoPilot修改。

### 19.4 真实模型冒烟测试

千问与DeepSeek冒烟测试通过环境变量和pytest标记显式开启，验证结构化输出、工具调用、usage采集以及M4的Handoff。普通测试默认不消耗真实Token。

## 20. 评测

`evals/tasks.json`中的每项包含任务ID、fixture、自然语言需求、测试命令、预期文件范围和是否必须通过。评测器在全新fixture副本上运行，记录：

- 任务成功率；
- 最终测试通过率；
- 自动修复成功率；
- 补丁有效性与修改范围；
- 工具调用、重试和审批次数；
- 总耗时、节点耗时和模型耗时；
- 输入、输出、缓存及总Token；
- 估算成本；
- 各节点与错误类型的失败分布。

模型价格不硬编码到历史结果。评测运行时记录使用的价格配置版本，无法获得价格时报告Token但将成本标记为不可用。

## 21. 验收标准

完成项目必须由自动化测试和演示证据共同证明：

1. FastAPI能接受仓库路径、自然语言需求和测试命令；
2. Agent只在独立Worktree中修改真实仓库副本；
3. 至少一个端到端场景发生一次测试失败并自动修复成功；
4. 输出可应用的Git Diff；
5. 原始仓库内容、分支、索引和工作区状态保持不变；
6. 达到最大修复次数后主动停止并报告原因；
7. 危险操作通过LangGraph中断等待审批；
8. 服务重启后可以恢复等待或执行中的任务；
9. 记录工具调用、测试结果、耗时、Token与成本；
10. MCP工具与Python内部工具行为一致；
11. 千问和DeepSeek至少各完成一次显式冒烟测试；
12. Agents SDK版本真实产生可审计Handoff和trace；
13. 系统从不自动合并、提交目标改动或推送远端。

只有M4验收通过后，简历中才加入OpenAI Agents SDK、Handoff和Tracing；M3之前的描述仅列出已真实实现的技术。

## 22. 实施约束

- 项目运行基线为Python 3.12；宿主机当前Python 3.14不作为兼容目标。
- 使用类型标注、Pydantic边界模型和结构化日志。
- 产品依赖与开发依赖分组管理。
- 所有功能以测试驱动方式实现；每个里程碑都有独立验收命令。
- M1至M4分别编写实施计划并依次交付，后续里程碑不得绕过前一里程碑的验收门槛。
- Docker Desktop当前未运行；开始Docker集成验证前需要启动daemon，但不影响纯Python单元测试和设计阶段。

## 23. 参考资料

- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph interrupts: <https://docs.langchain.com/oss/python/langgraph/interrupts>
- OpenAI Agents SDK: <https://openai.github.io/openai-agents-python/>
- OpenAI Agents SDK handoffs: <https://openai.github.io/openai-agents-python/handoffs/>
- OpenAI Agents SDK models: <https://openai.github.io/openai-agents-python/models/>
- MCP Python SDK: <https://github.com/modelcontextprotocol/python-sdk>
- Git worktree: <https://git-scm.com/docs/git-worktree.html>
- Alibaba Cloud Model Studio OpenAI compatibility: <https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope>
- DeepSeek API: <https://api-docs.deepseek.com/>
