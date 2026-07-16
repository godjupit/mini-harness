    # 第 11 章：理解 Provider——流式输出、协议转换、重试与错误

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读：

```text
src/mini_openharness/provider.py
错误类型与事件       15～72 行
Provider Protocol    75～91 行
Chat Provider        94～259 行
Responses Provider  262～401 行
转换辅助函数         404～526 行
DemoProvider         529～552 行
```

## 2. Provider 是反腐层

外部模型 API 的格式不稳定且彼此不同。Provider 把外部差异挡在边界外：

```text
内部 Message/ToolCall
    ↓ 转换
外部 API JSON/SSE
    ↓ 解析
内部 ModelReply
```

Engine 不需要知道 SSE 行长什么样，也不需要知道 Responses API 把工具调用称作 `function_call`。

## 3. 两种接口

```python
class StreamingModelProvider(Protocol):
    async def stream(...) -> AsyncIterator[ProviderEvent]

class CompletionModelProvider(Protocol):
    async def complete(...) -> ModelReply
```

Engine 优先检测 `stream()`，否则使用 `complete()`。这允许简单测试 Provider 只实现 complete，真实 Provider 实现流式接口。

## 4. ProviderEvent

```text
ProviderTextDelta  一小段生成文本
ProviderRetry      一次可重试故障及等待时间
ProviderComplete   完整 ModelReply
```

流式文本可以实时显示，但工具参数通常由多个片段拼接，必须等完整后才能 JSON 解析。

## 5. SSE 的基本概念

服务器持续发送类似：

```text
data: {"choices":[...]}

data: [DONE]
```

代码逐行读取，只处理 `data:`，解析 JSON，然后累积：

```text
content_parts
工具调用 id/name/arguments 的碎片
token usage
finish_reason
```

## 6. 为什么输出了一部分文本后不自动重试

Provider 的重试条件包含：

```python
if emitted_content:
    raise
```

假设用户已经看到“我将修改”，连接断开后若自动整次重试，可能重复输出，甚至重复产生不同工具调用。流式响应一旦对外产生内容，安全的自动重试就更困难。

## 7. 指数退避

```python
delay = retry_base_delay * (2 ** attempt)
```

例如基础 0.5 秒：

```text
0.5s → 1s → 2s → 4s
```

它避免在服务限流或短暂故障时立即高频重试。

## 8. 错误正规化

外部错误被转换成内部类型：

```text
401/403  ProviderAuthenticationError
429      ProviderRateLimitError
5xx      ProviderServerError
超时     ProviderTimeoutError
网络     ProviderNetworkError
上下文过长 ProviderContextWindowError
格式错误 ProviderInvalidResponseError
```

Engine 只处理这些稳定类型，不必判断各厂商的原始异常。

## 9. 可重试与不可重试

通常可重试：

```text
限流、超时、网络错误、服务器 5xx
```

通常不可重试：

```text
认证错误、参数错误、无效响应、输出被截断
```

上下文超长由 Engine 强制压缩后重试，因为只有 Engine 有权修改消息历史。

## 10. 工具调用参数的拼接

流式 API 可能分片发送：

```text
片段 1: {"path"
片段 2: :"README
片段 3: .md"}
```

Provider 按调用索引累积字符串，结束后 `json.loads()`。如果最终不是合法 JSON 对象，抛 `ProviderInvalidResponseError`，不能把半截参数交给工具。

## 11. 两种 OpenAI 风格 API 的转换

### Chat Completions

内部 Message 转换成带 `role/content/tool_calls/tool_call_id` 的消息列表。

### Responses API

System 内容放入 `instructions`；工具调用和结果转换为：

```text
function_call
function_call_output
```

缺少 `tool_call_id` 的工具结果会被拒绝，因为无法建立配对。

## 12. strict schema 判断

Responses API 对 strict function schema 有更严格要求。辅助函数保守判断：

- 对象必须 `additionalProperties=false`；
- 所有 properties 都必须在 required 中；
- 不支持含 default 的 schema；
- 子对象和数组递归检查。

不兼容时省略 strict，而不是错误宣称严格兼容。

## 13. DemoProvider 的教学价值

它根据消息历史决定下一步：先列文件，再加载 Skill，再读 README，最后完成。没有网络，却走完整的 AgentLoop 和工具执行路径。

## 14. 本章练习

1. 为什么 Provider 不直接返回原始 JSON？
2. 429 和 401 哪个应该自动重试？为什么？
3. 为什么工具参数必须完整 JSON 解析后才执行？

## 15. 参考答案

1. 统一内部协议，避免 Engine 被具体厂商 API 绑定。
2. 429 通常可等待后重试；401 多为 Key/权限配置错误，重复请求没有意义。
3. 半截或非对象参数可能导致错误甚至危险副作用，必须先验证完整性。
