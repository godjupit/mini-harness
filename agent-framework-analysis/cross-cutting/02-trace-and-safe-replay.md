# JSONL Trace 与安全 Replay

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Cross-cutting  
> 主要代码：`src/mini_openharness/trace.py`；CLI/Engine/Tools/Hooks 调用点

## 1. 模块职责与边界

**已确认**：Trace 为一次 run 生成 append-only JSONL 证据，覆盖模型、Hook、权限、资源锁、工具、MCP/Skill 归因、compaction、usage/cost 与终态；TraceStore 提供查询和仅渲染记录的 replay；`trace.py:16-144`。

它不是可执行 event sourcing：replay 不重建 state、不调用模型/工具，也不保证可从 trace 恢复 Session。

## 2. 独立性依据

拥有 run ID、sequence、时间基准、线程锁、文件格式、脱敏、summary/read/replay API 和独立 CLI 入口；被 Engine/Tools/Hooks/CLI 横向调用。

## 3. 对外接口

- `TraceWriter(root,run_id,metadata,redact_secrets)`。
- `emit(kind,data)`、`finish(status,data)`。
- `TraceEvent`、`TraceSummary`。
- `TraceStore.list/read/replay`。
- `render_event`。

`finish()` 幂等：首次 run_end 被缓存，后续返回相同 event；`trace.py:65-68`。

## 4. 写入模型

Writer 构造时创建 root/run ID，并立即写 `run_start`。每次 emit 在 `threading.Lock` 内增加 sequence、计算 UTC/elapsed、JSON-safe、可选 redaction，然后 append 一行并关闭 file handle；`trace.py:25-63`。

线程锁使并发 tool tasks 的 sequence/file append 有序。它是进程内锁，不协调多个 Writer 写同一路径。

## 5. 观测点

- CLI：prompt/workspace/provider/model metadata。
- Engine：model request/response/delta/retry/error、tool start/end、loop guard、context、终态。
- ToolRegistry：permission decision。
- Scheduler：resource wait/acquire/release 与耗时。
- HookExecutor：hook start/end、payload/update/failure。

**推断**：Trace 是直接函数调用，不是统一 EventBus；新增阶段必须主动增加 emit。

## 6. 安全 Replay 与读取

`TraceStore.read()` 先限制 run ID 字符集，再读 `<id>.jsonl`，避免路径穿越。`replay()` 只把每个 TraceEvent 交给 `render_event()`；`trace.py:109-144`。

List 会完整读取每个 trace 文件以生成 summary，规模增长后成本为 O(总事件数)，没有索引。

## 7. 脱敏机制

序列化后递归处理：敏感字段名整值替换；字符串内 Bearer 和常见 `sk-` pattern 替换；`trace.py:166-202`。默认开启，CLI 只有显式 `--unsafe-trace-secrets` 才关闭。

这是 best effort：业务秘密、文件内容、Hook output、artifact 不保证识别。Trace 文件权限也只依赖目录/umask，没有像 OAuth storage 显式 chmod 0600。

## 8. 扩展方式

AgentLoop 当前类型绑定 TraceWriter，但任何提供 emit/finish/run_id 的对象可能 duck-type 工作。正式接入 OpenTelemetry/remote sink 应定义 Tracer Protocol，明确 I/O failure policy、batching、redaction ownership 和 async backpressure。

## 9. 错误与边界情况

- emit 的 I/O/serialization 异常未隔离，可中断 Agent；意图待确认。
- 初始化后、AgentLoop 前失败可能留下只有 run_start 的“running” trace。
- 同一 run ID 多 Writer 可交错/sequence 冲突。
- read 一次性 `read_text()`，大 trace 内存开销高。
- list 把最后一个事件当 status 来源；缺 run_end 显示 running。
- Trace 记录完整 model messages/tool output，脱敏不是保密存储替代品。

## 10. 测试依据

- `tests/test_trace.py::test_trace_jsonl_list_show_and_safe_replay`
- `test_trace_rejects_path_traversal_run_id`
- `test_finish_is_idempotent`
- `test_trace_redacts_secret_keys_and_common_credentials_by_default`
- `tests/test_engine.py::test_agent_loop_trace_covers_model_tool_permission_usage_and_finish`
- `test_mcp_server_is_attributed_on_tool_start_and_end`
- `tests/test_hooks.py::test_stop_hook_blocks_completion_then_agent_recovers_and_trace_proves_it`

## 11. 设计评价与阅读建议

- 值得学习：小而可审计的 JSONL、并发 sequence、Replay 明确不重放副作用、默认脱敏。
- 潜在问题：同步逐事件 I/O、失败策略不明确、无权限/TTL/index、隐式事件 schema。
- 改进方向：Tracer Protocol、streaming read、manifest/index、0600/retention、typed event schema、sink failure mode。
- 精读：`TraceWriter.__init__/emit/finish`、`TraceStore.list/read/replay`、`_redact/_is_sensitive_key`，再检索所有 `.emit(` 调用点。
