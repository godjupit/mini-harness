    # 第 4 章：理解 Message、ToolCall 和 ModelReply 数据结构

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读 `src/mini_openharness/models.py`。这是项目最短的源码文件之一，却定义了模型、工具和 AgentLoop 之间的共同语言。

## 2. 为什么需要内部统一模型

Chat Completions API 和 Responses API 的 JSON 格式不同，未来也可能接入其他模型厂商。如果整个项目到处直接使用某个 API 的原始字典，其他模块会被外部格式绑死。

因此项目先定义自己的中立对象：

```text
ToolCall   模型请求执行的一个工具调用
Message    消息历史中的一条消息
ModelReply 模型一次回复的汇总结果
```

Provider 负责外部格式与这些对象之间的转换。

## 3. ToolCall

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
```

字段解释：

- `id`：本次调用的唯一标识；
- `name`：工具名，例如 `read_file`；
- `arguments`：参数对象，例如 `{"path": "README.md"}`。

为什么必须有 `id`？因为一次模型回复可能同时请求多个工具，返回结果时需要准确配对。

```text
call-1 → read_file README.md
call-2 → read_file pyproject.toml
```

对应的工具结果必须分别标明 `call-1` 和 `call-2`。

## 4. Message 的四种 role

```python
Role = Literal["system", "user", "assistant", "tool"]
```

### system

运行规则和系统指令。通常放在消息列表第一项。

### user

用户输入，以及运行时为了让模型继续修正而追加的反馈。

### assistant

模型回复。它可以同时包含普通文本和工具调用。

### tool

工具执行结果，必须通过 `tool_call_id` 对应之前的调用。

## 5. Message 字段

```python
@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
```

一个 assistant 消息可能是：

```python
Message(
    role="assistant",
    content="我先读取说明文件。",
    tool_calls=(
        ToolCall("call-1", "read_file", {"path": "README.md"}),
    ),
)
```

工具完成后：

```python
Message(
    role="tool",
    content="# Mini OpenHarness ...",
    tool_call_id="call-1",
    name="read_file",
)
```

## 6. tuple 而不是 list

`tool_calls` 使用元组：

```python
tuple[ToolCall, ...]
```

再加上 `frozen=True`，体现作者希望消息对象创建后尽量保持不可变。不可变对象更容易追踪和安全共享，尤其在异步代码中。

注意：`frozen=True` 只阻止给字段重新赋值；字段内部若是可变字典，仍然需要谨慎。但本项目通常把这些对象当作值对象使用。

## 7. 序列化与反序列化

```python
def to_dict(self):
    return asdict(self)
```

`asdict()` 会把 dataclass 递归转换为普通字典，便于写 Trace 或发送请求。

```python
@classmethod
def from_dict(cls, data):
    calls = tuple(ToolCall(**call) for call in data.get("tool_calls", ()))
    ...
```

`classmethod` 表示这个方法属于类本身，调用方式是：

```python
message = Message.from_dict(payload)
```

## 8. ModelReply 与 Message 的区别

```python
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int
    output_tokens: int
```

`ModelReply` 是 Provider 一次调用的返回结果，还带 token 使用量。AgentLoop 收到它后，会把内容转换成一条 `Message("assistant", ...)` 加入历史。

可理解为：

```text
ModelReply：刚从模型服务回来的“临时结果”
Message：已经写入对话历史的“持久记录”
```

## 9. 核心协议不变量

必须记住：

```text
每一个 assistant tool call
必须有一个 tool message 使用相同 tool_call_id 回应
```

上下文压缩、Provider 格式转换和并行工具执行都必须保护这个关系。

## 10. 本章练习

1. 构造一条请求 `list_files` 的 assistant Message。
2. 为什么工具结果不能只写 `name="read_file"`，还要写 `tool_call_id`？
3. `ModelReply` 为什么包含 token，而普通 `Message` 不包含？

## 11. 参考答案

1. `Message("assistant", tool_calls=(ToolCall("x", "list_files", {"path":"."}),))`。
2. 同名工具可在一轮中被调用多次，只有唯一 ID 能准确配对。
3. token 是一次 Provider 请求的计量信息，不属于对话协议本身。
