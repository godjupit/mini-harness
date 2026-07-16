    # 第 5 章：用最小测试看懂 Model → Tool → Model 闭环

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

先不精读复杂的 `AgentLoop.run()`，而是从测试看到外部行为。目标测试位于：

```text
tests/test_engine.py
test_model_tool_model_loop
```

这是一种很有效的源码阅读方法：**测试像一个小剧本，告诉你系统在什么输入下应该发生什么。**

## 2. 测试由哪几部分组成

典型结构：

```text
Arrange：准备假的 Provider、工具和临时目录
Act：运行 AgentLoop
Assert：检查事件、调用次数和消息历史
```

项目定义了 `ScriptedProvider`，它不是访问真实网络，而是按预先准备的顺序返回多个 `ModelReply`。

可以想象：

```python
provider = ScriptedProvider([
    ModelReply(tool_calls=(ToolCall(...),)),
    ModelReply(content="完成"),
])
```

第一次模型调用请求工具；第二次模型调用给最终回答。

## 3. 完整时序

```text
第 1 次模型调用
  ↓
assistant: 请求 list_files
  ↓
AgentLoop 执行 list_files
  ↓
tool: 返回文件列表
  ↓
第 2 次模型调用（此时消息历史已有工具结果）
  ↓
assistant: 最终回答
  ↓
done
```

这就是最小 Agent 闭环。

## 4. 为什么工具结果必须进入 messages

模型服务通常没有你的本地文件访问权。它只知道自己请求了一个工具，但不知道工具实际返回了什么。

AgentLoop 必须追加：

```python
Message(
    role="tool",
    content="README.md\npyproject.toml",
    tool_call_id="call-1",
    name="list_files",
)
```

然后再次把完整消息历史交给模型。模型看到工具输出，才可以继续推理。

## 5. 测试里的临时目录

`tmp_path` 是 pytest 提供的 fixture。每个测试获得一个独立临时目录，避免测试修改真实仓库。

例如：

```python
(tmp_path / "README.md").write_text("hello")
```

工具的 workspace 设为 `tmp_path`，测试结束后环境可以被清理。

这是测试有副作用代码的重要原则：

```text
不要依赖用户机器上的真实文件
不要让不同测试共享可变状态
```

## 6. collect 辅助函数

由于 `AgentLoop.run()` 是异步生成器，测试通常需要一个辅助函数收集事件：

```python
async def collect_async():
    return [event async for event in loop.run(prompt)]
```

再通过 `asyncio.run(...)` 从同步测试中启动。

你需要区分：

- `await coroutine`：等待一个最终结果；
- `async for`：持续接收多个异步产生的结果。

## 7. 断言在证明什么

一个好的闭环测试通常会验证：

- Provider 被调用两次；
- 工具执行了一次；
- 消息历史角色顺序合理；
- 最后产生 `done` 事件；
- 工具输出确实出现在第二次模型调用的输入中。

测试不只是“代码跑完没报错”，而是在验证协议和状态转移。

## 8. 手动画一次消息历史

运行前：

```text
[system]
```

加入用户输入后：

```text
[system, user]
```

模型请求工具后：

```text
[system, user, assistant(tool_calls)]
```

工具完成后：

```text
[system, user, assistant(tool_calls), tool]
```

最终回复后：

```text
[system, user, assistant(tool_calls), tool, assistant(final)]
```

## 9. 怎样运行单个测试

```bash
pytest -q tests/test_engine.py::test_model_tool_model_loop
```

加 `-s` 可显示测试中的打印输出；加 `-vv` 可显示更详细的测试名。

## 10. 本章练习

1. 把测试改成第一轮同时请求两个 `read_file`，你预计消息历史怎样变化？
2. 如果 Provider 第一轮直接返回最终文本且没有工具调用，工具会执行吗？
3. 为什么测试使用假的 Provider 比真实 API 更可靠？

## 11. 参考答案

1. 一条 assistant 消息包含两个 ToolCall，后面追加两条对应的 tool 消息，然后再调用模型。
2. 不会；AgentLoop 会进入完成验证分支。
3. 假 Provider 行为确定、速度快、不需要网络和费用，也能精确构造边界情况。
