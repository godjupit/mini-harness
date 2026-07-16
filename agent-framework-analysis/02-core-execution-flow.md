# 02｜核心执行流

## 1. 典型链路选择

本页追踪“CLI 启动、模型请求一个或多个工具、工具完成后模型给出最终回答”的完整路径。这比纯文本一步结束更能暴露框架真实职责。

最小离线触发方式：

```bash
.venv/bin/mini-oh --demo --workspace . "解释这个项目"
```

**已确认**：`DemoProvider` 的前三轮会请求 `list_files`、可选 `load_skill` 和 `read_file`，随后返回最终文本；`provider.py:529-552`。

## 2. 一次运行的完整顺序

### A. 入口与装配

1. `mini-oh` console script 调用 `cli.main()`；`pyproject.toml:23-24`。
2. `main()` 从 cwd 的 `.env` 加载变量但不覆盖 shell，解析 `run` 参数并用 `asyncio.run(_run(...))` 进入事件循环；`cli.py:323-337`。
3. `_run()` 确定 workspace/prompt，发现 Skill metadata 并拼接 system prompt；`cli.py:95-103`。
4. `_run()` 创建 Demo 或 OpenAI Provider、可选 TraceWriter、默认 ToolRegistry，并按参数注册 Docker shell、Skill tool 与 MCP tools；`cli.py:104-159`。
5. CLI 创建 PermissionPolicy、HookRegistry、ContextCompactor、ArtifactStore，最终构造 AgentLoop；`cli.py:150-183`。

### B. 用户输入进入运行时

6. `AgentLoop.run(prompt)` 为本轮重建 cancel event、resource locks 和重复 batch counter；`engine.py:133-139`。
7. Runtime 执行 `user_prompt_submit` Hooks。Hook 可按顺序改写 prompt；阻断则 trace failed 并发出 `hook_blocked`、`error` 后返回；`engine.py:140-152`。
8. 接受后的 prompt 作为 `Message(role="user")` 追加到 `self.messages`，并创建本轮 `ToolContext`；`engine.py:152-161`。

### C. 模型阶段

9. 进入最多 `max_steps` 次的 for-loop。每步先检查取消，再执行阈值式 compaction；`engine.py:164-170`。
10. Runtime 发出 `model_start`/trace `model_request`，把当前 messages 与 `tools.schemas()` 交给 Provider；`engine.py:172-195`。
11. 若 Provider 有 `stream()`，Runtime消费 `ProviderTextDelta`、`ProviderRetry`、`ProviderComplete`；否则调用 `complete()`。文本 delta 立即转换为 `AgentEvent("assistant_delta")`；`engine.py:187-218`。
12. Provider adapter 把内部 Message/Schema 转成 Responses Items 或 Chat messages，解析 SSE，再收敛为 `ModelReply`；`provider.py:118-256,262-401,478-526`。
13. 若遇 typed `ProviderContextWindowError`，本次 user run 最多强制 compact 一次并在同一逻辑 step 重试；无法 compact 或再次失败则发 `error` 终止；`engine.py:222-246`。
14. 其他 `ProviderError` 终止 run；provider 内部只有 429/5xx/timeout/network 且尚未输出内容时才指数退避；`engine.py:247-253`、`provider.py:125-153,404-421`。
15. 完整 reply 的 token 加入累计值，assistant Message（含 tool calls）写入 history，并发出 response/assistant events；`engine.py:263-282`。

### D1. 无工具调用：验证并终止

16. 若 `reply.tool_calls` 为空，Runtime 先执行 `stop` Hooks；`engine.py:284-294`。
17. stop Hook 允许：TraceWriter `finish(completed)`，发出 `AgentEvent("done")` 并返回；`engine.py:313-316`。
18. stop Hook 阻断：发出 `hook_blocked`，把验证失败原因追加为新的 user Message，继续下一 model step；`engine.py:295-312`。

### D2. 有工具调用：能力执行链

19. Runtime 为每个 ToolCall 发 `tool_start`，并从 Registry 查询 source；MCP 的 server 归因由名称前缀推导；`engine.py:318-330`。
20. `_record_tool_batch()` 对 name+arguments 生成稳定签名。连续重复超过阈值时不执行真实工具，而为每个 call 生成 loop-guard error result；`engine.py:332-347,523-535`。
21. 正常 batch 通过 `_execute_all()` 并发创建每个 `_execute_timed()`；`asyncio.gather()` 保证返回顺序与 call 顺序相同，cancel task 可取消整批；`engine.py:502-521`。
22. 每个 call 先执行 `pre_tool_use` Hooks。Hook 可阻断或改写 `tool_input`；改写后的 dict 才进入资源解析、schema 与权限，避免借改写绕过检查；`engine.py:381-419`。
23. Registry 解析 `ResourceAccess`。所有 call 已并发启动，但只有无冲突资源能同时获得 `ResourceLockManager`；冲突调用等待，Trace 记录 wait/acquire/release；`tools.py:21-75,136-156`、`engine.py:418-453`。
24. 锁内 `ToolRegistry.execute()`：查找工具 → JSON Schema 校验 → PermissionPolicy evaluate → 可选人工 approval → trace decision → `asyncio.wait_for(tool.run)` → 把 timeout/异常转为 `ToolResult(is_error=True)`；`tools.py:158-215`。
25. 锁释放后执行 `post_tool_use` Hooks。它可改写 output/metadata/is_error 或把结果变成错误；`engine.py:454-500`。
26. 每个结果按原 call 顺序处理。过大输出由 ArtifactStore 原子落盘，history 只保存头尾与路径；`engine.py:351-357,537-541`、`compaction.py:61-85`。
27. Runtime 追加带相同 `tool_call_id` 的 tool Message，发 `tool_end`/trace；`engine.py:357-374,570-574`。
28. 进入下一 model step，Provider 收到 assistant tool call 与一一对应的 tool result，模型可继续、纠错或结束。

## 3. 核心状态转移

| 状态 | 进入条件 | 主要动作 | 离开条件 |
| -- | -- | -- | -- |
| Prompt gate | 每次 `run(prompt)` | Hook 改写/阻断 | 接受后写 user Message；阻断终止 |
| Model request | 每个 step | compact、serialize、stream/complete | reply、typed error 或 cancel |
| Tool batch | reply 有 tool calls | 熔断检查、并发 task、资源锁 | 每个 call 得到一个 ToolResult |
| Observation append | ToolResult 到达收口点 | artifact offload、tool Message 配对 | 回到下一 model request |
| Completion gate | reply 无 tool calls | stop Hook 验证 | allow→done；block→反馈并继续 |
| Terminal | provider error/cancel/max steps/done | trace finish 或抛硬上限异常 | run 结束 |

## 4. 关键不变量

1. **已确认**：每个已接受的模型 ToolCall 最终按相同 ID 追加一个 tool Message；即使 unknown、permission deny、schema error、timeout、hook block 或 loop guard，也用错误 observation 保持协议闭合。
2. **已确认**：`asyncio.gather` 保持结果顺序，因此并行完成顺序不会改变 history 的 call/result 对应顺序；`engine.py:502-506`。
3. **已确认**：assistant tool-call Message 与其后所有 tool results 在 compaction 中属于同一 atomic unit；`compaction.py:88-108`。
4. **已确认**：pre Tool Hook 的参数改写发生在 resource/schema/permission 之前，post Hook 发生在执行后、history 回填前。
5. **已确认**：Provider retry 不在已输出 token 后发生，以避免重复文本；`provider.py:129-140`。
6. **已确认**：Replay 只读取/渲染 TraceEvent，不调用 Provider/Tool；`trace.py:109-144`。

## 5. 错误、重试与终止的区别

| 情况 | 是否终止 run | 如何反馈模型/用户 |
| -- | -- | -- |
| Tool unknown/schema/permission/timeout/exception | 否 | `ToolResult(is_error=True)` 写回 history |
| pre/post Hook block | 通常否 | 工具错误 observation；prompt block 例外并终止 |
| stop Hook block | 否 | 可信验证失败作为 user feedback，进入下一 step |
| repeated tool batch 超限 | 否 | 不执行副作用，逐 call 写 loop_guard error |
| Provider 429/5xx/timeout/network（未输出） | Provider 内重试 | 发 `provider_retry` event |
| Provider context-window error | 最多一次同 step 强制 compact 重试 | compact event；失败后 error |
| 其他 ProviderError | 是 | `AgentEvent("error")`，CLI exit 1 |
| 用户 cancel / task cancel | 是 | cancel event 或 CancelledError，清理 task/资源 |
| max steps | 是 | 抛 `MaxStepsExceeded`，CLI exit 1 |
| 正常 done | 是 | `AgentEvent("done")`，CLI exit 0 |

## 6. 多轮复用语义

- **已确认**：同一个 AgentLoop 可多次调用 `run()`；messages 和 token/cost 会保留并继续累计。
- **已确认**：每轮会重建 cancel event/resource locks，重置 repeated batch counter 与 reactive context retry flag。
- **推断**：这形成轻量 in-memory session 行为，但项目没有 session ID、持久化或并发多 run 保护；不应据此创建 Session 模块。

## 7. 对照图

完整 Mermaid 时序/流程图见 `diagrams/core-execution-flow.mmd`。
