# 异步、取消、超时与资源生命周期

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Cross-cutting  
> 主要代码：`engine.py`、`provider.py`、`tools.py`、`hooks.py`、`mcp.py`、`sandbox.py`

## 1. 横切职责

**已确认**：整个 Runtime 基于单进程 `asyncio`：Agent run 是 async generator，Provider 是异步流，tool batch 并发，资源锁是 Condition，Tool/Hook 用 timeout，MCP/HTTP/subprocess 都有异步生命周期。

该主题独立成文，因为“取消是否传播、资源是否释放、timeout 在哪一层生效”无法从任一模块单独回答。

## 2. 异步边界图

```text
CLI asyncio.run
└─ AgentLoop.run async generator
   ├─ Provider.stream async iterator
   ├─ HookExecutor.execute → wait_for Hook.run
   └─ _execute_all
      ├─ gather(_execute_timed × N)
      │  └─ ResourceLockManager.acquire
      │     └─ ToolRegistry.execute → wait_for Tool.run
      └─ cancel_event.wait task
```

MCP transport、httpx client、Docker/Hook subprocess 在这些调用内部使用 async context/process API。

## 3. 并发语义

Tool batch 的所有 calls 同时创建 coroutine；ResourceLockManager 只阻塞 effect 冲突段，pre Hooks 可先并发运行。`gather()` 按输入顺序返回，即使实际完成顺序不同；`engine.py:502-506`。

TraceWriter 使用线程锁而非 asyncio lock，因为 emit 是同步函数并可能从并发 tasks 调用。文件读写工具用 `asyncio.to_thread()` 避免阻塞 loop。

## 4. 取消传播

- 用户调用 `AgentLoop.cancel()` 设置 event。
- 每个 step 顶部检查；Provider 请求前/读流/退避检查。
- `_execute_all()` 同时等待 gather 与 cancel event；取消获胜时 cancel gather 并 await 清理。
- Provider task cancellation 转为 ProviderCancelledError；Engine 转成 cancelled event。
- CommandHook/Sandbox 捕获 CancelledError，kill/wait 子进程后重新抛。
- MCP/Provider 的外层资源由 CLI finally close。

**重要差异**：显式 cancel event 与 task cancellation 是两条路径；实现扩展必须都考虑。

## 5. Timeout 层次

| 层 | 实现 | 结果 |
| -- | -- | -- |
| Provider HTTP | httpx client timeout | typed ProviderTimeoutError，可重试 |
| Tool | Registry `asyncio.wait_for` | error ToolResult，Agent 可恢复 |
| Hook | Executor `asyncio.wait_for` | 按 failure_mode block/continue |
| Sandbox | adapter 内 wait_for + Registry 外层 | 强制 rm container，error result |
| OAuth callback | MCP SDK provider timeout | 授权失败/异常 |

Timeout 是 wall-clock，不区分排队与执行；resource lock 等待发生在 Registry timeout 之外，因此 ToolContext timeout 不包含 lock wait/pre Hook 时间。

## 6. 资源生命周期

- httpx Provider client：CLI finally `close()`。
- McpManager：每 server AsyncExitStack；CLI finally reverse close。
- Resource lock：asynccontextmanager finally 释放并 notify_all。
- Hook subprocess：communicate 完成或 cancel kill/wait。
- Docker container：`--rm` 正常回收；timeout/cancel 显式 `docker rm -f`。
- Trace file：每次 emit 短打开，不维持 handle。
- OAuth callback server：callback handler finally close/wait_closed。

## 7. 扩展约定

自定义 Provider/Tool/Hook 应：

1. 不吞 `CancelledError`；
2. 在 finally/async context 中释放 socket/process/temp file；
3. 长循环检查取消或使用可取消 await；
4. 不在 event loop 中做大块同步 I/O；
5. Tool failure返回 ToolResult，基础设施故障用约定异常；
6. 共享状态自行加锁，因为批 Tool/Hook 可能并发。

## 8. 边界与风险

- 同一 AgentLoop 并发 run 不安全。
- ResourceLockManager 无严格 FIFO，可能理论写饥饿；作用域仅单 run/loop instance。
- `asyncio.to_thread` 的底层函数在 coroutine cancel 后可能继续运行。
- Tool `wait_for` 依赖实现正确响应 cancellation；恶意/错误 Tool 可阻塞。
- consumer 若不完整消费/显式关闭 async generator，清理语义没有专用 context manager 保护。
- CLI 的 `KeyboardInterrupt` 与 task cancellation 行为依赖 `asyncio.run` teardown。

## 9. 测试依据

- `tests/test_engine.py::test_parallel_tool_calls_preserve_result_order`
- `test_cancel_stops_in_flight_tool_task`
- `test_stream_deltas_retries_and_provider_failure_are_traced`
- `tests/test_tools.py::test_tool_timeout_becomes_recoverable_observation`
- `test_tree_read_lock_blocks_child_write_until_release`
- `tests/test_hooks.py::test_timeout_obeys_failure_mode`
- `test_command_timeout_kills_child_process`
- `tests/test_provider.py::test_pre_cancelled_request_never_reaches_network`
- `tests/test_sandbox.py::test_docker_shell_timeout_removes_container`

## 10. 设计评价与阅读建议

- 值得学习：取消与 error observation 分层、effect-aware concurrency、ExitStack、子进程清理。
- 潜在问题：双取消机制、timeout 范围不一致、无 Run context manager、锁公平性有限。
- 改进方向：RunState/TaskGroup、统一 deadline/cancellation token、async generator context wrapper、scheduler 指标与公平队列。
- 阅读顺序：`AgentLoop.run/_execute_all` → Provider `stream` → `ResourceLockManager.acquire` → `ToolRegistry.execute` → `HookExecutor.execute` → MCP ExitStack → Sandbox cleanup。
