# 流式模型 Provider 边界

> 分析状态：已验证  
> 优先级：P0  
> 模块类型：Adapter  
> 主要代码：`src/mini_openharness/provider.py`

## 1. 模块职责

**已确认**：Provider 模块把内部 `Message + tool schemas` 翻译成 OpenAI Responses 或 Chat Completions-compatible 请求，解析 SSE、重组文本与 ToolCall，并把网络/协议错误归一化；`provider.py:15-526`。

它不管理多步 Agent history、不执行工具、不决定 context 如何压缩，也不自动在 Responses 404 后切换 Chat，以避免隐式重放请求。

## 2. 为什么它是独立模块

- 两个显式 Protocol 是主要替换点；`provider.py:75-91`。
- 拥有独立 `httpx.AsyncClient` 生命周期和 `close()`。
- ProviderError 异常族隔离认证、限流、timeout、network、server、context、invalid/truncated/cancel。
- Responses 与 Chat 有不同 wire protocol，但都输出统一 ModelReply。
- `tests/test_provider.py` 对流事件和错误契约有密集覆盖。

## 3. 对外接口

- `StreamingModelProvider.stream(messages, tools, cancel_event=...)`：产生 delta/retry/complete。
- `CompletionModelProvider.complete(messages, tools)`：返回完整 ModelReply。
- `ModelProvider`：以上两个 Protocol 的 union。
- `OpenAICompatibleProvider`：Chat SSE adapter，同时提供 stream 和 complete wrapper。
- `OpenAIResponsesProvider`：继承公共 client/retry，覆盖 wire parser/payload。
- `DemoProvider`：确定性 completion adapter，用于无 API key 的真实闭环演示。
- `ProviderTextDelta/ProviderRetry/ProviderComplete`：Runtime 可识别的 provider event。

包根导出 ModelProvider 与两个生产 adapter，不导出异常族和 DemoProvider；`__init__.py:4-8,13-18`。

## 4. 内部实现

### 公共重试壳

`OpenAICompatibleProvider.stream()` 先创建 payload，再循环 `max_retries + 1` 次调用 `_stream_once()`。只对 typed retryable error、尚未输出文本且仍有额度的情况退避；`provider.py:118-153`。

### Chat adapter

逐行处理 `data:` SSE，将分片 content 和按 index 分片的 id/name/arguments 累积，检查 finish reason，再用 `_tool_call_from_parts()` JSON decode；`provider.py:164-256`。

### Responses adapter

识别 output text delta、function item added/done、arguments delta/done、completed/incomplete/failed。final arguments done 覆盖增量值，而 call ID 继续来自 item；`provider.py:265-372`。

### Schema strict eligibility

Responses payload 仅在每层 object `additionalProperties=false`、所有 properties required、无 default 且子结构兼容时发送 `strict: true`；否则省略 strict；`provider.py:374-401,452-475`。执行侧仍由 ToolRegistry 用原始 schema 校验。

## 5. 输入与输出

- 输入：完整 history、工具 name/description/parameters、可选 cancel event。
- 输出：流式文本、retry 通知、最终 ModelReply。
- 副作用：HTTP 请求、退避 sleep、client socket；DemoProvider 无外部副作用。
- 状态：model/max_retries/delay/client；不保存 conversation。
- 异常：所有已识别 Provider 故障归一化为 ProviderError 子类。

## 6. 调用关系

- 上游：AgentLoop；CLI 创建/关闭生产 Provider。
- 下游：`httpx` 与远端 API；内部 models。
- AgentLoop 用 `getattr(provider, "stream")` 判断走流式或 complete；`engine.py:187-218`。
- ContextWindowError 由 AgentLoop 专门恢复，其他 retryable errors 在 Provider 内恢复，职责没有混淆。

## 7. 核心执行顺序

```text
serialize internal messages/tools
→ POST chat/completions 或 responses
→ read SSE lines
→ emit text delta
→ accumulate function-call parts
→ validate terminal event / finish reason
→ JSON-decode tool arguments
→ emit ProviderComplete(ModelReply)
```

失败时：

```text
normalize HTTP/network error
→ content emitted? yes: raise
→ retryable and attempts remain? emit retry + exponential wait
→ otherwise raise typed error
```

## 8. 关键技术原理

- **Adapter/anti-corruption layer**：把两个外部协议收敛成内部模型。
- **SSE 增量重组**：文本可直接流出，ToolCall 必须等 JSON arguments 完整后才产生。
- **At-most-safe retry**：首个内容前才重试，降低重复输出风险；它仍不是完整幂等保证，因为服务端可能已产生不可见副作用。
- **Typed recovery signal**：只有明确 context marker 产生 ContextWindowError，避免把任意 400 当作可压缩问题。
- **Cancellation**：请求前、读流中、退避期间检查 event；task cancellation 转换为 ProviderCancelledError。

## 9. 扩展方式

最小扩展是实现 `complete()`；完整流式扩展实现 `stream()` 并以 ProviderComplete 收尾。若复用现有 retry/client，可继承 `OpenAICompatibleProvider` 并覆盖 `_payload/_stream_once`，但这两个方法是事实上的模板方法而非公开 Protocol。

新增 Provider 应保证：

1. ToolCall arguments 是 dict；
2. 每个 stream 正常结束恰好产生一个 ProviderComplete；
3. truncation/failed terminal 不伪装成成功；
4. cancellation 不吞掉；
5. usage 缺失时明确置零或估算。

## 10. 错误与边界情况

- Chat 只处理 `data:` lines；非 SSE 兼容行为会被忽略并可能得到空完成。
- `_status_error` 依赖响应 body marker 识别 context overflow，供应商措辞变化可能漏判。
- ServerError、rate limit、timeout/network 可重试；authentication/普通 4xx/invalid response 不重试。
- Responses 流必须出现 `response.completed`，仅 `[DONE]` 不足以成功。
- 已经输出文本后遇到 truncation 会抛错，CLI 可能已经显示部分文本，但不会发 done。
- `complete()` 没有 cancel_event 参数；非流式自定义 Provider 的取消只能靠 task cancellation。

## 11. 测试依据

- `tests/test_provider.py::test_streaming_text_fragmented_tool_call_and_usage`
- `test_responses_stream_uses_typed_items_call_id_and_usage`
- `test_responses_function_arguments_done_is_authoritative`
- `test_responses_enables_strict_only_for_compatible_function_schemas`
- `test_chat_length_finish_reason_never_becomes_success`
- `test_responses_incomplete_max_output_tokens_never_becomes_success`
- `test_retryable_failures_retry_before_any_content`
- `test_context_window_http_errors_are_typed_for_runtime_recovery`
- `test_pre_cancelled_request_never_reaches_network`

## 12. 设计评价

- 值得学习：wire protocol 差异完全隔离；terminal event 被当作协议契约；错误类型直接服务 Runtime 恢复。
- 复杂度来源：SSE 的分片 function calls 和两个 API 的不同 item 模型。
- 潜在问题：context marker 与兼容端点行为是启发式；继承层把 Chat 类作为 Responses 基类，命名略误导。
- 改进方向：抽公共 HTTP/retry base、为 event parser 做独立纯函数、增加 Retry-After/jitter、为 Provider contract 建共享 conformance tests。

## 13. 阅读建议

精读 `stream`、两个 `_stream_once`、两个 `_payload`、`_status_error`、`_tool_call_from_parts`、`_strict_schema_compatible`、`_to_responses_items`。
