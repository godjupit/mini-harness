# 内部对话与工具调用协议

> 分析状态：已验证  
> 优先级：P0  
> 模块类型：Domain  
> 主要代码：`src/mini_openharness/models.py::ToolCall/Message/ModelReply`

## 1. 模块职责

**已确认**：该模块定义 Provider-neutral 的最小对话语言，使 AgentLoop、两种 OpenAI 协议、Compactor 和测试 Provider 不共享外部 API 的原始 payload；`models.py:9-47`。

它只表达一次对话及工具调用所需数据，不负责验证 tool arguments、执行工具、管理 history 生命周期或表达富媒体内容。

## 2. 为什么它是独立模块

- 无包内依赖，是干净的领域叶节点。
- 被 Engine、Provider、Compaction 三个关键模块共同依赖。
- ToolCall ID 配对直接决定工具协议正确性。
- `Message.to_dict/from_dict` 暗示跨运行边界的序列化用途。
- 改动 role/字段会同时影响两个 Provider wire converter 和 compaction 原子性。

## 3. 对外接口

### `ToolCall`

不可变 dataclass：`id`、`name`、`arguments`。arguments 默认空 dict；`models.py:12-16`。

### `Message`

不可变 dataclass：role、content、assistant tool calls，以及 tool result 的 call ID/name。支持 `to_dict()` 与 `from_dict()`；`models.py:19-39`。

Role 只有 `system/user/assistant/tool`。类型提示限制 role，但运行时 dataclass 不验证非法字符串。

### `ModelReply`

一次完整 assistant turn：文本、零或多个 ToolCall、input/output token usage；`models.py:42-47`。

## 4. 内部协议不变量

### Tool call/result 配对

Provider 返回 assistant Message 中的 `ToolCall(id=...)`；执行结果必须成为 `Message(role="tool", tool_call_id=同一 id)`。Responses converter 据此生成 `function_call` 与 `function_call_output`；`provider.py:499-525`。

### Assistant 可以同时有文本和 tool calls

`ModelReply.content` 与 `tool_calls` 不是互斥字段；Engine 会先输出 assistant 文本，再执行 calls；`engine.py:263-284`。

### System Message 的特殊性

AgentLoop 保证当前 system prompt 位于第一条；Responses converter 把所有 system content 合并成 `instructions` 并从 Items 中排除；`engine.py:108-116`、`provider.py:374-384,499-504`。

### Atomic tool unit

Compactor 把一个含 tool calls 的 assistant Message 与随后所有 tool Messages作为不可拆单元；`compaction.py:88-108`。

## 5. 输入与输出转换

| 边界 | 输入 | 输出 |
| -- | -- | -- |
| AgentLoop → Provider | `list[Message]`、tool schemas | Provider wire payload |
| Provider → AgentLoop | SSE chunks | `ModelReply`/Provider events |
| AgentLoop → History | `ModelReply` | assistant `Message` |
| Tool → History | `ToolResult` | tool `Message`，错误加 `ERROR:` 前缀 |
| Compactor | `list[Message]` | 新 messages + compact metadata |
| Trace | `Message.to_dict()` | JSON-safe event data |

## 6. 调用关系

- 上游创建者：AgentLoop、Provider parser、Demo/test Provider、Compactor summary。
- 下游消费者：Provider serializers、AgentLoop、Trace payload、Compactor。
- 无循环依赖；该模块是静态依赖图的底层。
- 未从包根 `__all__` 导出，属于可直接 import 但未强调的 API。

## 7. 关键技术原理

### Anti-corruption layer

内部模型屏蔽 Responses Items 与 Chat messages 的差异。Provider 是翻译层，AgentLoop 不理解 `response.output_item.done` 或 Chat `finish_reason`。

### Frozen dataclass

对象不可重新赋字段，降低并行与历史修改中的意外共享状态；但 `arguments` dict 本身仍可变，frozen 不是深不可变。

### 宽松序列化

`asdict()` 递归转为字典；`from_dict()` 只重建 ToolCall，没有 schema/version/未知字段验证。这适合 Mini，但不构成长期持久格式承诺。

## 8. 扩展方式

当前没有注册机制。若增加 role、image content、reasoning item 或 provider-specific metadata，需要同步：

1. dataclass/类型；
2. `_to_openai_message()`；
3. `_to_responses_items()`；
4. Trace JSON safety；
5. Compactor token 估算和 atomic unit；
6. 序列化兼容测试。

## 9. 错误与边界情况

- `Message.from_dict()` 对非法 role、缺失/错误类型字段缺少显式校验。
- tool result 缺 `tool_call_id` 时只在 Responses 转换阶段抛 `ProviderInvalidResponseError`；Chat converter 会生成缺少 ID 的消息。
- JSON Schema 不在 ToolCall 模型层验证，必须在执行前再次校验。
- token usage 默认为 0，兼容不返回 usage 的 provider，但 cost 可能被低估。
- 只支持字符串 content；MCP 非文本内容会在 adapter 层序列化成 JSON 字符串。

## 10. 测试依据

- `tests/test_engine.py::test_model_tool_model_loop`
- `test_parallel_tool_calls_preserve_result_order`
- `tests/test_compaction.py::test_compaction_keeps_recent_tool_call_and_all_results_together`
- `tests/test_provider.py::test_responses_stream_uses_typed_items_call_id_and_usage`
- `test_responses_function_arguments_done_is_authoritative`

## 11. 设计评价

- 值得学习：小而明确的内部协议显著降低 Provider/Runtime 耦合。
- 复杂度来源：工具调用需要跨多条 Message 保持 ID 和顺序不变量。
- 潜在问题：浅层不变、无 runtime validation、无版本化，尚不适合作为长期 Session 文件格式。
- 改进方向：若引入 session persistence，可增加显式 codec/schema version；若支持多模态，优先把 content 改成 typed parts 而非继续堆可选字段。

## 12. 阅读建议

精读 `ToolCall`、`Message.from_dict`、`ModelReply`，然后对照 `provider._to_openai_message`、`provider._to_responses_items`、`engine._append_tool_result` 和 `compaction._atomic_units`。
