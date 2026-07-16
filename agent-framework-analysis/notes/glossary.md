# 术语表

| 术语 | 本项目中的准确含义 | 主要代码 |
| -- | -- | -- |
| Agent Loop | 有界的 model→tool→observation 反馈循环，不是 Workflow graph | `engine.py::AgentLoop.run` |
| AgentEvent | `run()` 向调用者流式产生的运行事件；不是 EventBus 消息 | `engine.py::AgentEvent` |
| Model step | 一次逻辑 Provider turn；context error 的同一步重试不增加 step | `engine.py:164-254` |
| Message | Provider-neutral history 单元，role 为 system/user/assistant/tool | `models.py::Message` |
| ToolCall | 模型请求的工具名、ID 与 arguments；ID关联后续结果 | `models.py::ToolCall` |
| ModelReply | 一次完整 assistant turn，含文本、calls 与 usage | `models.py::ModelReply` |
| Provider | 将内部消息/Schema 转为外部模型协议并标准化响应的 adapter | `provider.py` |
| ProviderEvent | text delta、retry 或 complete；只用于模型边界 | `provider.py:55-72` |
| Tool | 模型可请求的 capability，声明 schema/effect并异步执行 | `tools.py::Tool` |
| ToolRegistry | Tool 注册、Schema 暴露、权限/timeout/错误归一化边界 | `tools.py::ToolRegistry` |
| ToolContext | 一次 run 中传给 Tool 的 workspace、权限、Trace、timeout 上下文 | `tools.py::ToolContext` |
| ToolResult | Tool 的字符串 observation、错误标记与 metadata | `tools.py::ToolResult` |
| Observation | 写回模型 history 的工具结果；失败也作为 observation | `engine.py::_append_tool_result` |
| Effect | Tool 是否只读及访问哪些逻辑资源的运行期描述 | `Tool.read_only`、`ResourceAccess` |
| ResourceAccess | `key + read/write + exact/tree` 的逻辑锁请求 | `tools.py::ResourceAccess` |
| Effect-aware scheduling | 所有 ToolCall 并发启动，只让资源冲突段等待 | `engine.py::_execute_all`、`ResourceLockManager` |
| PermissionPolicy | 有序 glob rules + mutation default 的 allow/deny/ask evaluator | `permissions.py` |
| ApprovalCallback | ask 决策时异步请求人类/外部系统返回 bool | `permissions.py::ApprovalCallback` |
| Hook | 模型不可绕过的可信生命周期扩展；不同于模型可选 Tool | `hooks.py::Hook` |
| Verification Gate | stop Hook 在 done 前执行验证，失败反馈模型继续修复 | `engine.py:284-312` |
| Failure mode | Hook 运行失败/timeout 时 block（fail-closed）或 continue（fail-open） | `hooks.py::FailureMode` |
| Compaction | 把旧 history 变为确定性 summary并保留 recent atomic units | `compaction.py::ContextCompactor` |
| Atomic unit | 含 calls 的 assistant Message 与随后 tool results 的不可拆组 | `compaction.py::_atomic_units` |
| Reactive context recovery | typed context error 触发一次 force compact并在同一步重试 | `engine.py:222-240` |
| Artifact | 过大 Tool output 的完整本地文件，history 仅保留头尾/路径 | `compaction.py::ArtifactStore` |
| Trace | 单次 run 的 append-only JSONL 执行证据 | `trace.py::TraceWriter` |
| Replay | 只渲染 TraceEvent，不重新执行 Provider/Tool | `trace.py::TraceStore.replay` |
| Skill | `<name>/SKILL.md` 目录内容扩展；metadata先注入，正文按需加载 | `skills.py` |
| MCP bridge | 将远端 MCP Tool 适配并注册进本地 ToolRegistry | `mcp.py::McpManager/McpTool` |
| Trust annotation | MCP `readOnlyHint` 等远端提示；默认不信任 | `mcp.py:136-140,178-180` |
| Sandbox shell | 可选 Docker-only mutation Tool，不是宿主 shell fallback | `sandbox.py::SandboxedShellTool` |
| Session | 本项目没有正式模块；仅有 AgentLoop 进程内 messages 的轻量多轮状态 | `engine.py:108-121` |
| Memory | 本项目没有检索/持久化 Memory；不能把 Trace/Artifact 等同 Memory | — |
| Plugin | 本项目没有动态 plugin system；Tool/Hook/Skill/MCP 是不同扩展点 | — |

## 容易混淆的成对概念

- Tool vs Hook：Tool 由模型选择，Hook 由 Runtime 强制。
- Permission vs Resource Lock：前者决定能不能做，后者决定何时并行做。
- Trace vs Session：前者是审计记录，后者需要可恢复的对话状态契约；项目只实现前者。
- Compaction vs Memory：前者缩减当前 history，不提供长期检索。
- Skill vs Plugin：Skill 是指令文件内容，不加载任意框架代码。
- Provider retry vs reactive retry：前者恢复网络/服务错误，后者恢复输入 context overflow。
