# AgentLoop 运行编排

> 分析状态：已验证  
> 优先级：P0  
> 模块类型：Core  
> 主要代码：`src/mini_openharness/engine.py::AgentLoop/AgentEvent/MaxStepsExceeded`

## 1. 模块职责

**已确认**：`AgentLoop` 是一次 Agent 运行的中央协调器，负责维护对话历史，并重复执行“模型 → 工具 → observation → 模型”，直到回答通过完成门、发生取消/Provider 错误，或耗尽步数；`engine.py:57-379`。

它负责控制顺序、状态与终止，不负责解析具体模型协议、实现工具副作用、决定具体权限规则或持久化通用 Session。后者分别委托给 Provider、ToolRegistry、PermissionPolicy 和 Trace/Artifact 组件。

## 2. 为什么它是独立模块

- **独立状态**：messages、token/cost、cancel event、重复 tool batch 计数、资源锁；`engine.py:87-121`。
- **完整生命周期**：从 prompt Hook 到 `done/error/cancelled/MaxStepsExceeded`。
- **公开入口**：包根导出 `AgentLoop` 和 `AgentEvent`；`__init__.py:3-24`。
- **独立测试**：`tests/test_engine.py` 覆盖主循环、并发、熔断、取消、Trace、compaction 和边界错误。
- **核心阶段**：所有模型与工具适配器都由它串入闭环。

## 3. 对外接口

### `AgentLoop(...)`

必需依赖是 `provider`、`tools`、`workspace`；其余控制项均可选注入。构造器会验证 `max_steps`、tool timeout 和 repeat limit，并确保 system Message 位于 history 首位；`engine.py:60-121`。

### `async AgentLoop.run(prompt) -> AsyncIterator[AgentEvent]`

异步生成运行事件，而不是一次返回最终字符串。调用者必须消费生成器才能驱动实际执行。重要事件包括 model start/delta、tool start/end、compact、hook blocked、retry、error、cancelled 和 done；`engine.py:30-50,133-379`。

### `AgentLoop.cancel()`

设置本轮 cancel event。Provider stream、批工具任务以及 run 顶层都会观察它；`engine.py:130-131,164-167,190-196,508-521`。

### `estimated_cost`

按累计 input/output tokens 与构造时价格计算；`engine.py:123-128`。**注意**：同一个 loop 多轮复用时 token 不重置。

## 4. 内部实现与状态

`run()` 开始时重建与当前 event loop 绑定的 `asyncio.Event` 和 `ResourceLockManager`，并重置重复调用熔断状态；这使顺序复用同一实例跨多个 `asyncio.run()` 成为可能；`engine.py:133-139`。

对话状态的修改点只有四类：

1. 构造时创建/刷新 system Message；
2. prompt Hook 允许后追加 user Message；
3. Provider 完成后追加 assistant Message；
4. 每个工具完成后追加带 call ID 的 tool Message；compaction 则整体替换 messages。

`reactive_context_retry_attempted` 是每个 `run()` 的局部变量，因此一次用户 turn 最多强制压缩重试一次，不消耗额外逻辑 step；`engine.py:162,222-240`。

## 5. 输入、输出和副作用

- 输入：字符串 prompt，以及构造时注入的历史/依赖。
- 输出：按时间产生的 `AgentEvent`；最终成功由 `done` 表示，不直接返回回答对象。
- 状态变化：history 与 token/cost 累积；per-run cancel/lock/repeat state 重置。
- 副作用：通过工具、Trace、Artifact、Hook、Provider 间接产生。
- 硬异常：参数错误在构造时抛 `ValueError`；步数耗尽抛 `MaxStepsExceeded`。
- 软错误：Provider/Hook/取消多转换为 AgentEvent；Tool 失败保持为 observation。

## 6. 调用关系

- 上游：CLI `_run()`、程序化库用户、engine/hook tests。
- 下游：Provider、ToolRegistry/ResourceLockManager、HookExecutor、PermissionPolicy、ContextCompactor、ArtifactStore、TraceWriter。
- 最强耦合：Tool 调度需要 Registry 的 schema/source/resources/execute 四个面向；Hook payload 又与 engine 内字段名直接耦合。
- **已确认**：没有静态循环 import；模型与工具构成的是运行期反馈回路。

## 7. 核心执行顺序

```text
reset per-run state
→ user_prompt_submit Hook
→ append user Message
→ for step in max_steps
   → threshold compaction
   → Provider stream/complete
   → append assistant Message
   → no calls: stop Hook → done 或反馈继续
   → calls: repeat guard → concurrent effect-aware execution
   → offload → append tool Messages
→ MaxStepsExceeded
```

完整时序见 `../diagrams/core-execution-flow.mmd`。

## 8. 关键技术原理

### 有界反馈状态机

它不是 Workflow graph，而是 `for step` 中的显式分支。工具失败被编码成消息，使模型能自我修复；只有 Provider/取消/步数等控制面错误终止。

### Async generator

运行事件与执行进度同流输出，支持 token delta 和工具生命周期可视化。代价是消费者中途停止迭代时的清理契约不如 async context manager 明确。

### 双层循环

外层计算 model step，内层仅处理同一步的 reactive context retry；因此恢复动作不虚耗 max steps。

### 完成验证反馈

无 tool calls 不等于立即成功。stop Hook 阻断时，Runtime 把可信原因作为 user Message 加入 history并继续；`engine.py:284-312`。

## 9. 扩展方式

- 实现 streaming 或 completion Provider 后注入 `provider=`。
- 在 ToolRegistry 注册 Tool，Runtime 无需增加类型分支。
- 注册 Hook 改写/阻断四个生命周期点。
- 注入 Policy、Tracer、Compactor 或 ArtifactStore 调整控制能力。

**待确认**：Compactor/Tracer 参数使用具体类标注，虽然可 duck-type 替换，但还不是正式 Protocol 扩展点。

## 10. 错误与边界情况

- `AgentLoop` 不保证同一实例并发执行多个 `run()`；共享字段会相互覆盖。
- Provider 已输出 delta 后失败不会在 provider 内重试，避免重复内容。
- 重复 batch 熔断只统计连续相同调用；交替重复不会触发。
- Tool task 使用 `gather()`；某个 `_execute_timed` 意外抛出未归一化异常可能取消整批。
- Trace emit 的文件 I/O 错误未隔离，可能打断主循环。
- stop Hook 持续失败最终仍受 max steps 硬上限约束。

## 11. 测试依据

- `tests/test_engine.py::test_model_tool_model_loop`
- `test_parallel_tool_calls_preserve_result_order`
- `test_repeated_tool_batch_is_blocked_but_model_can_recover`
- `test_loop_guard_counter_resets_for_each_user_run`
- `test_cancel_stops_in_flight_tool_task`
- `test_context_error_forces_one_compaction_and_retries_same_model_step`
- `tests/test_hooks.py::test_stop_hook_blocks_completion_then_agent_recovers_and_trace_proves_it`

## 12. 设计评价

- 值得学习：用内部消息协议把可恢复工具错误留在循环；同一步 context recovery；完成前可信验证门。
- 复杂度来源：`run()` 同时编排 Provider、Hook、Trace、compaction 和 Tool，函数超过 240 行。
- 潜在问题：核心通过 `mcp__`/`load_skill` 名称识别来源；per-run 与跨 turn 状态混在同一对象；未禁止并发 run。
- 改进方向：抽出结构化 tool source metadata、RunState、Provider-turn helper，并为 tracer/compactor 定义 Protocol。

## 13. 阅读建议

依次精读：`AgentLoop.__init__`、`run`、`_execute_timed`、`_execute_all`、`_record_tool_batch`、`_compact_if_needed`、`_append_tool_result`。
