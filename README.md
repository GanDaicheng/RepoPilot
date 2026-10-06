# RepoPilot

RepoPilot 是一个面向真实 Git 代码仓库的受控开发 Agent。当前 M1 先把最容易造成数据损坏或命令越权的底层能力做实：它在独立 Git Worktree 中检索和读取代码、校验并原子应用补丁、生成完整 Git diff，并在无网络的加固 Docker 容器中运行测试。所有工具都返回统一、可审计的结构化结果。

## M1 能解决什么

- 防止 Agent 直接污染开发者正在使用的原始 checkout；即使原目录同时存在 staged、unstaged 和 untracked 改动，也从固定提交创建隔离 Worktree。
- 防止路径穿越、绝对路径和符号链接逃逸；所有文件操作都限制在 Worktree 内。
- 防止模型把测试字符串当成任意 shell 使用；默认只允许 `pytest` 与 `python -m pytest`，管道、重定向和命令拼接永久拒绝。
- 在写入前解析并校验整份统一 diff；删除文件与清理 Worktree 必须显式审批。
- 让测试失败和 Docker 基础设施失败可区分，便于后续 Agent 根据失败类型决定修复还是停止。

M1 不包含模型调用、自动规划循环、LangGraph、FastAPI、MCP、多 Agent Handoff、持久化审批或评测系统。这些能力分别在 M2–M4 接入；M1 不放置无法运行的占位实现。

## 环境要求

- Python 3.12（项目运行基线）
- Git
- ripgrep（`rg`）
- Docker Desktop 或 Docker Engine，且 daemon 已启动

创建并激活 Python 3.12 虚拟环境后安装：

```bash
python -m pip install -e ".[dev]"
```

构建无网络测试阶段使用的镜像：

```bash
docker build -t repopilot-runner:m1 .
```

Docker daemon 未运行时，Docker 集成测试会给出明确跳过原因；这种跳过不算 M1 验收通过。

## 验证命令

单元测试：

```bash
python -m pytest tests/unit -q
```

不依赖 Docker 的全部测试：

```bash
python -m pytest -m "not docker" -q
```

Worktree 集成测试：

```bash
python -m pytest tests/integration/test_worktree.py -q
```

Docker 与完整 M1 端到端验收：

```bash
docker build -t repopilot-runner:m1 .
python -m pytest -m docker -q
```

源码编译检查与完整验证：

```bash
python -m compileall -q src tests
python -m pytest -q
```

## 安全边界

- 文件结果只暴露相对 POSIX 路径；读取有字节上限，NUL 数据按二进制拒绝。
- 代码搜索使用 `rg --json --fixed-strings` 参数数组，不经过 shell，并限制结果数和超时。
- Git 状态和 diff 是只读操作；未跟踪文件通过 `git diff --no-index` 纳入结果，不执行 `git add`。
- 补丁中全部 source/target 路径会在任何写入前完成规范化与边界验证；`git apply --check` 通过后才整包应用。
- Docker 默认关闭网络、根文件系统只读、清空额外 capabilities、启用 `no-new-privileges`，并限制 CPU、内存、进程数和墙钟时间。
- 测试容器只挂载当前 Worktree，不挂载用户目录、凭据、SSH 配置或 Docker socket，也不转发宿主机环境变量。
- Worktree 清理和文件删除返回 `approval_required`，只有显式批准后才执行。

## 已知限制

- M1 的 Docker 镜像只预装 pytest，不会自动安装目标仓库依赖；依赖准备与审批属于后续里程碑。
- 仅支持 UTF-8 文本读取和统一 diff 文本补丁。
- 测试命令白名单目前面向 Python/pytest；额外执行器需要显式审批。
- 不自动合并、不修改原始 checkout、不执行 `git push`。
- Docker 验收必须在 Linux 容器 daemon 可用且 `repopilot-runner:m1` 镜像构建成功后执行。

## 里程碑路线

- M2：单 Agent 修复循环、LangGraph 状态机、SQLite checkpoint、FastAPI 与 SSE。
- M3：Human-in-the-loop 持久化审批、MCP Server、任务产物、评测集与指标。
- M4：OpenAI Agents SDK 主管/开发/测试/审查 Agent、Handoff，以及千问、DeepSeek 和通用 OpenAI-compatible 模型配置与 tracing。

