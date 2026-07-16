# 阅读路线与理解检查

## 1. 30 分钟路线：建立主干

目标：能口述一次 Agent 运行和四个核心边界。

1. 3 分钟：`README.md:21-48` 的 demo 和退出语义。
2. 5 分钟：本手册 `02-core-execution-flow.md` 与 Mermaid 时序图。
3. 10 分钟：`engine.py::AgentLoop.run`，只看 prompt、Provider、tool/no-tool 两个大分支。
4. 4 分钟：`models.py` 三个 dataclass。
5. 4 分钟：`tools.py::ToolRegistry.execute`。
6. 4 分钟：`provider.py` 两个 Protocol 与 ProviderEvent。

完成标准：能解释为什么 Tool 错误通常不终止 run，以及 `done` 为什么仍可能被 stop Hook 阻断。

## 2. 2 小时路线：理解扩展与可靠性

目标：能修改一个 Tool/Hook/Provider，并预测权限、并发、Trace 和取消行为。

### 前 30 分钟：主状态机

- 阅读 `modules/01-agent-loop-runtime.md`。
- 对照 `engine.py:133-379` 和 `tests/test_engine.py::test_model_tool_model_loop`。

### 30–60 分钟：两个核心边界

- `modules/03-streaming-model-providers.md`：重点看 Responses Items/SSE/terminal error。
- `modules/04-tool-capability-and-effect-scheduling.md`：重点看 ResourceAccess、Registry.execute、`_execute_timed` 顺序。

### 60–90 分钟：控制平面

- `modules/06-lifecycle-hooks.md` 与 `cross-cutting/01-permission-and-approval.md`。
- 对照一个 pre/post Hook test 和 stop verification test。

### 90–120 分钟：长任务与证据

- `modules/07-context-compaction-and-artifacts.md`。
- `cross-cutting/02-trace-and-safe-replay.md`。
- 运行 `.venv/bin/mini-oh --demo --workspace .`，再用 trace show/replay 查看事件。

完成标准：能画出 `pre Hook → resource lock → schema → permission → Tool → post Hook`，并说明每层失败去向。

## 3. 1 天路线：能够评审和扩展

### 上午第一段：源码主链（2 小时）

按 `important-code-index.md` 精读 Engine、Models、Provider、Tools；逐项对照 tests，手工记录每个状态修改点和 terminal path。

### 上午第二段：扩展机制（1.5 小时）

精读 Hook、Skill、MCP adapter；回答“为什么三者不能合并成 Plugin”。实现一个只读 Tool 和一个 callback Hook，补测试。

### 下午第一段：可靠性与安全（2 小时）

阅读四篇 cross-cutting 文档和 Sandbox/OAuth 集成。画出 secret/data flow，区分可信/不可信主体与 fail-open/fail-closed。

### 下午第二段：验证与实验（1.5 小时）

- 运行全量 pytest。
- 修改测试 fixture 模拟 Provider context overflow、Tool timeout、Hook block、Trace redaction。
- 观察 event/history/trace 三个视图是否一致。

### 收尾（1 小时）

阅读 `open-questions.md`，选择一个风险写出设计草案：RunState、Tracer Protocol、ToolMetadata 或 Artifact security。

## 4. 最值得学习的设计

1. **错误作为 observation**：Tool schema/permission/timeout/exception 不轻易打断闭环。
2. **协议不变量优先**：并发结果仍按 call 顺序回填；compaction 不拆 tool turn。
3. **Effect-aware 调度**：并发不是全开/全关，而按层级资源冲突。
4. **Typed recovery**：Provider error 类型直接决定退避、压缩恢复或终止。
5. **不可绕过的完成门**：stop Hook 把测试等验证变成真正终止条件。
6. **统一 capability boundary**：Skill、MCP、Docker 都回到 ToolRegistry，而非特殊后门。
7. **Replay 不重放副作用**：明确把审计回放与 event-sourcing 执行分开。

## 5. 最大工程风险

1. `AgentLoop.run()` 职责密度高，扩展新阶段容易破坏调用顺序。
2. Hook/AgentEvent/Trace data 使用未版本化 dict，存在隐式协议耦合。
3. 同一 AgentLoop 并发 run 未防护；per-run 与跨 turn 状态混合。
4. Trace/Artifact 的权限、TTL、加密和 I/O failure policy 不足。
5. Tool source/effect/permission resources 依赖名称与字段约定。
6. stdio MCP 继承完整环境，可信 server 边界较大。
7. Deterministic summary 可能丢重要语义；字符 token 估算粗糙。
8. 锁只在单 AgentLoop 内，无法协调多进程/多实例写入。

## 6. 推荐动手修改任务

按风险从低到高：

1. 为重复 Skill name 增加显式错误与测试。
2. 给 Tool 增加结构化 `source` metadata，移除 Engine 的 `mcp__`/`load_skill` 特判。
3. 定义 `Compactor` Protocol 和共享 contract tests。
4. 把 Hook event payload 改为 typed dataclass，同时保持 command JSON codec。
5. 引入 `RunState`，明确每轮重置与跨 turn 累积字段，并阻止并发 run。
6. 为 TraceWriter 加 sink failure mode、0600、streaming read 和 retention。
7. 让 sandbox stdout 在截断前写入 Artifact，避免完整证据丢失。
8. 为 stdio MCP 配置显式环境 allowlist。

## 7. 理解检查题

1. 为什么无 tool calls 仍不必然产生 `done`？
2. Provider 的网络 retry 与 context reactive retry 分别在哪一层，为什么？
3. pre Tool Hook 改写 path 后，哪些检查会用新 path 重做？
4. 为什么两个不同文件的 write 可以并发，而 list workspace 与 write 子文件会冲突？
5. 未知 Tool 为什么既被视为 mutation，又仍返回 observation？
6. `asyncio.gather` 在这里保证什么，不保证什么？
7. 为什么 Trace replay 不算可恢复 Session？
8. MCP `readOnlyHint` 为什么默认不能决定权限与并发？
9. Skill 为什么不是 Plugin？
10. 同一个 AgentLoop 第二次 run 时，哪些状态保留，哪些重置？
11. ArtifactStore 解决了什么，又引入哪些数据安全问题？
12. 如果自定义 Tool 吞掉 CancelledError，Runtime 的 timeout/cancel 保证会怎样退化？

## 8. 检查题答案要点

1. stop Hook 可阻断并反馈模型。
2. Provider 内恢复瞬态传输错误；Engine 根据 typed context signal 改 history 后同 step 重试。
3. resource resolution、Schema、Permission 和真实 Tool 都看到新参数。
4. 精确 file locks 不冲突；目录 tree read 与后代 write 冲突。
5. effect fail-closed；错误仍遵守 Agent 协议闭环。
6. 保持结果顺序；不提供资源安全、公平性或跨进程协调。
7. 只渲染事件，不重建可执行 history/状态。
8. 它是远端提示，误标会导致默认放行和错误并发。
9. 只加载指令内容，不动态加载框架代码/生命周期。
10. messages/token/cost 保留；cancel/locks/repeat/reactive retry 重置。
11. 保留完整大输出并缩小 context；带来权限、TTL、secret、绝对路径与覆盖风险。
12. task 可能无法及时停止，底层副作用可能在 Runtime 已报告取消后继续。
