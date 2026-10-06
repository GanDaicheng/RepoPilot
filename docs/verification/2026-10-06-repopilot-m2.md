# RepoPilot M2 验收记录

验收日期：2026-10-06  
实现提交：`79a08e8beff3e40fa2afb45bfaef474061999803`  
基线提交：`1d6f49b`  
平台：Windows，Python 3.14.2，Git 2.54.0.windows.1，Docker Client/Server 29.8.0  
Docker 镜像：`repopilot-runner:m1`，`sha256:32da20566ecd70a7a954ebc333ceaef4d724d0b86e7e207ec23ae086ad48ac75`

## 最终命令与结果

| 命令 | 结果 |
| --- | --- |
| `docker build -t repopilot-runner:m1 .` | 成功；镜像重新构建并标记 |
| `python -m compileall -q src tests` | 成功，无诊断输出 |
| `python -m pytest -q` | `264 passed, 2 skipped in 95.28s`；真实 Docker 用例实际运行 |
| 基于 `git ls-tree 1d6f49b` 生成的 M1 测试文件列表执行 `python -m pytest -q` | `80 passed in 28.31s` |
| `python -m pytest tests/smoke/test_deepseek.py tests/smoke/test_qwen.py -q -rs` | `2 skipped`；均因未同时提供显式开关和完整安全配置 |
| `git diff --check` | 成功，无空白错误 |
| `python -m pip check` | `No broken requirements found.` |
| 对工作树执行凭据形态扫描（排除示例变量文件） | `credential_shape_file_count=0` |

DeepSeek 跳过条件：缺少 `RUN_DEEPSEEK_SMOKE=1`、轮换后的 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL`。  
千问跳过条件：缺少 `RUN_QWEN_SMOKE=1`、`QWEN_MODEL` 和 `QWEN_BASE_URL`。  
默认验收未发起任何付费模型网络请求。

## 13 项验收标准映射

1. Fake Model 首次补丁成功：通过 `tests/e2e/test_m2_scenarios.py` 的成功场景验证。
2. 首次失败后自动修复成功：通过同一 E2E 套件的失败分析、补丁修复场景验证。
3. 连续失败在上限停止：通过重试耗尽 E2E 和补丁应用失败路由单测验证，终态为 `retry_exhausted`。
4. 取消阻止后续副作用：通过运行中取消 E2E、节点二次取消检查和事务级终态竞争测试验证。
5. 使用原 `thread_id` 恢复：通过 SQLite checkpoint 集成测试和 Worker 重启 E2E 验证；`base_commit` 只写一次并在 checkpoint 间隙复用。
6. SSE 断线续传：通过 `Last-Event-ID` 集成测试和 E2E 验证，无跨任务泄漏或重复事件。
7. 危险操作暂停：删除文件和未列入白名单的测试命令进入 `awaiting_approval`；审批事件只含规范化摘要，不含原始参数。
8. DeepSeek 真实冒烟：测试入口和 OpenAI-compatible 传输已就绪；本次未提供轮换后的完整配置，按规范明确跳过，未用 Fake 冒充。
9. 千问真实冒烟：测试入口和 OpenAI-compatible 传输已就绪；本次未提供完整显式配置，按规范明确跳过。
10. M1 回归：从 M1 基线枚举的原测试文件共 `80 passed`，超过规范最初记录的 70 项。
11. 新增测试与 Docker：全量 `264 passed`；Docker daemon 可用，真实容器测试和 7 个 M2 E2E 场景实际运行。
12. 原仓库隔离：E2E 验证内容、索引和当前分支不变；系统未执行自动 commit、merge、push 或清理。
13. 无 API Key：请求入口、SSE 和持久化均有脱敏/拒绝测试；代码库凭据形态扫描为 0；`.env` 与本地运行数据已忽略。

## 审查结论与已知限制

全分支复核后无 Critical 遗留。复核问题均已用回归测试关闭，包括带连字符的 Key 检测、取消与终态的原子优先级、允许仓库根目录、SDK 重试边界、基础提交只写一次、审批载荷最小化、模型工具预算、API 请求体限制、Worker 持久化故障恢复，以及 Docker 字节码缓存隔离。

真实 DeepSeek/千问连通性仍是显式的本地验收项：必须使用已轮换的凭据和实际模型 ID 后单独运行。M2 仍采用单 Worker，危险操作只能暂停、不能在线批准；这两点属于设计中明确记录的后续范围。
