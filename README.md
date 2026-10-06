# RepoPilot

RepoPilot 是一个面向真实 Git 仓库的受控代码修复 Agent。它接收本地仓库路径、自然语言需求和测试命令，在独立 Git Worktree 中完成仓库检索、方案生成、补丁应用、Docker 测试、失败分析、自动修复和只读审查，并通过 FastAPI 与持久化 SSE 对外提供任务状态。

当前仓库已完成 M1 安全工具层与 M2 单 Agent 修复闭环。DeepSeek、千问和自定义 OpenAI-compatible 模型通过统一网关接入；默认测试与 CI 使用确定性 Fake Model，不会调用付费模型。

## 它解决什么

- 将一次代码修改组织成“理解需求 → 检索代码 → 制定方案 → 修改 → 测试 → 修复 → 审查”的可恢复流程，而不是一次性生成不可追踪的代码片段。
- 所有写入只发生在固定 commit 创建的托管 Worktree 中，不修改开发者正在使用的原始 checkout、索引或分支。
- 测试在无网络、只读根文件系统、受 CPU/内存/进程限制的 Docker 容器中运行。
- 模型只能调用六种有界只读工具，不能直接执行 shell、应用补丁、创建/清理 Worktree 或改变审批结果。
- SQLite 同时保存业务任务、事件、模型用量和 LangGraph checkpoint；进程重启后沿用原 `thread_id` 恢复，并对补丁副作用做幂等对账。
- 任务事件支持 `Last-Event-ID` 断线续传；错误、响应和事件不会保存完整提示、完整 Diff、完整测试日志或密钥。

## 架构

```text
FastAPI / SSE
    ↓
TaskService → SQLite tasks / events / model_calls
    ↓
单进程 Worker → LangGraph checkpoint
    ↓
RepoAgent → ModelGateway → Fake / DeepSeek / Qwen / Custom
    ↓
只读工具 + Patch 工具 + Git Worktree + DockerTestRunner
```

M2 的 `RepoAgent` 是阶段化单 Agent。OpenAI Agents SDK、多 Agent 主管/开发/测试/审查角色及 Handoff 不在当前版本中，计划在 M4 接入。

## 环境要求

- Python 3.12 或更高版本
- Git
- ripgrep（`rg`）
- Docker Desktop 或 Docker Engine，且 daemon 已启动

安装开发依赖并构建测试镜像：

```bash
python -m pip install -e ".[dev]"
docker build -t repopilot-runner:m1 .
```

运行数据目录必须位于目标仓库之外。默认使用用户目录下的 `.repopilot`；也可以先设置 `REPOPILOT_DATA_DIR`。项目不会自动读取 `.env` 文件，`.env.example` 只是安全的变量清单，请通过终端、进程管理器或密钥管理服务注入配置。

## 启动 API

```bash
uvicorn repopilot.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

默认数据文件：

```text
$REPOPILOT_DATA_DIR/repopilot.sqlite3
$REPOPILOT_DATA_DIR/checkpoints.sqlite3
$REPOPILOT_DATA_DIR/managed/worktrees/...
```

创建任务：

```bash
curl -X POST http://127.0.0.1:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "repo_path": "C:/code/example",
    "user_request": "为除零场景返回明确的领域错误",
    "test_command": "pytest -q",
    "model_profile": "deepseek",
    "max_retries": 2
  }'
```

查询、取消和订阅事件：

```bash
curl http://127.0.0.1:8000/tasks/<task-id>
curl -X POST http://127.0.0.1:8000/tasks/<task-id>/cancel
curl -N http://127.0.0.1:8000/tasks/<task-id>/events
curl -N -H "Last-Event-ID: 42" http://127.0.0.1:8000/tasks/<task-id>/events
```

`POST /tasks` 立即返回 `202`。后台只有一个 Worker 串行领取数据库任务。`max_retries=2` 表示初始补丁之外最多再安排两次代码修复；模型连接重试与 JSON 格式修复不增加该计数。

取消是安全边界取消，不会强杀已经启动的 Docker 进程。正在执行的调用完成后，Graph 会重新读取持久化取消标志，不再启动后续模型、补丁、测试或审查步骤。

文件删除或非默认测试执行器会进入 `awaiting_approval`，并产生 `approval_required` 事件。M2 没有批准/恢复接口，因此不会自动执行这些操作；完整的持久化 Human-in-the-loop 审批属于 M3。

## 模型配置

任务的 `model_profile` 只能是 `fake`、`deepseek`、`qwen` 或 `custom`。调用方不能指定任意密钥环境变量。

DeepSeek：

```text
DEEPSEEK_MODEL
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL（可选，默认 https://api.deepseek.com）
```

千问：

```text
QWEN_MODEL
QWEN_BASE_URL（必须显式配置对应地域的 HTTPS OpenAI-compatible 端点）
DASHSCOPE_API_KEY
```

自定义 OpenAI-compatible 服务：

```text
MODEL_NAME
MODEL_BASE_URL
MODEL_API_KEY
```

`fake` 不需要密钥，用于测试和 CI。应用默认不会给 Fake Model 注入具体修复脚本，因此普通运行应选择已配置的真实 profile；端到端测试会显式注入确定性脚本，确保不消耗 Token。

## 验证

完整本地验收：

```bash
python -m compileall -q src tests
docker build -t repopilot-runner:m1 .
python -m pytest -q
```

只运行 M2 Fake Model 端到端场景：

```bash
python -m pytest tests/e2e -q
```

真实模型冒烟测试默认明确跳过。只有同时设置 opt-in 开关及对应配置时才会产生真实请求：

```bash
RUN_DEEPSEEK_SMOKE=1 python -m pytest -m deepseek_smoke tests/smoke/test_deepseek.py -q
RUN_QWEN_SMOKE=1 python -m pytest -m qwen_smoke tests/smoke/test_qwen.py -q
```

不要把密钥写入命令历史、测试参数、仓库文件或任务需求。若密钥曾出现在聊天或日志中，应先在供应商控制台撤销并生成新密钥，再通过受控环境变量注入。

## 安全边界

- 文件路径经过规范化和 Worktree 边界校验；读取、搜索、Diff、模型输出和事件均有大小上限。
- 搜索和 Git 工具使用参数数组，不经过 shell；测试命令策略永久拒绝管道、重定向、拼接和多行输入。
- 统一 Diff 在写入前解析所有路径并执行 `git apply --check`；重启重放仅在反向校验证明确切补丁已存在时视为成功。
- Docker 禁用网络，使用非 root 用户、只读根文件系统、`no-new-privileges`、capability 清空和资源限制；不挂载用户目录、SSH 配置、密钥或 Docker socket。
- 原仓库不会被自动 commit、merge、push 或清理；托管 Worktree 清理仍需显式批准。
- API Key 使用 Pydantic `SecretStr`，只从固定环境变量读取；业务表、checkpoint、事件和 HTTP 响应不保存密钥或完整 provider 对话。

## 当前限制与路线

- Docker 镜像只预装 pytest，不会自动安装目标仓库依赖。
- M2 是单 Worker、单 Agent，适合个人项目和可审计演示，不是分布式执行平台。
- M3：持久化审批决定与恢复接口、MCP Server、任务产物和评测指标。
- M4：OpenAI Agents SDK，多 Agent 主管/开发/测试/审查角色，Handoff 与 tracing。
