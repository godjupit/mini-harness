    # 第 6 章：精读 AgentLoop.run() 主状态机

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

这是全项目最重要的一章。阅读：

```text
src/mini_openharness/engine.py
AgentLoop.__init__()  60～121 行
AgentLoop.run()      133～379 行
```

不要一开始逐行背代码。先把它当成一个有限步数的状态机。

## 2. 为什么叫 Loop

一次模型回复可能只完成任务的一部分：它可能先列目录，再读文件，再写文件，最后总结。因此程序必须反复执行：

```text
模型 → 工具 → 模型 → 工具 → 模型
```

但循环不能无限进行，所以有：

```python
for step in range(1, self.max_steps + 1):
```

超过限制抛出 `MaxStepsExceeded`。

## 3. __init__ 保存了哪些状态

重要字段：

```text
provider             模型适配器
tools                工具注册表
workspace            文件边界
messages             对话历史
input/output_tokens  用量计数
cancel_event          取消信号
_resource_locks       并发资源锁
_last_tool_batch      上一批工具签名
_repeated_tool_batches 重复次数
```

`AgentLoop` 不是一个纯函数，它是有状态对象。

## 4. 每次 run 开始为何重建部分状态

开头：

```python
self.cancel_event = asyncio.Event()
self._resource_locks = ResourceLockManager()
self._last_tool_batch = None
self._repeated_tool_batches = 0
```

同一个 AgentLoop 对象可能被多次调用。取消事件和锁可能绑定旧的事件循环，重复工具计数也不能污染下一次用户任务，所以每次运行重置。

注意：`messages` 没有全部清空，这允许多轮对话复用上下文。

## 5. 用户输入先经过 Hook

```python
prompt_hooks = await self.hook_executor.execute(
    HookEvent.USER_PROMPT_SUBMIT,
    {"prompt": prompt},
)
```

Hook 可以：

- 拒绝输入；
- 修改 prompt；
- 允许原样继续。

只有通过后才执行：

```python
self.messages.append(Message("user", prompt))
```

这说明 Hook 位于消息进入历史之前。

## 6. 每步开始检查取消与压缩

```python
if self.cancel_event.is_set():
    ...
compact_event = self._compact_if_needed()
```

取消是协作式的：外部调用 `loop.cancel()`，内部在安全节点检查事件。

上下文压缩在模型请求前发生，避免请求长度超过阈值。

## 7. 为什么模型调用还有一层 while True

外层 `for step` 表示逻辑步骤；内层 `while True` 用于“同一步模型请求的恢复重试”。

特别是上下文超限：

```text
ProviderContextWindowError
→ 强制压缩 messages
→ 不增加 step
→ 重试同一个模型步骤
```

网络重试主要由 Provider 自己处理；上下文错误需要 Engine 改变消息历史，所以由 Engine 恢复。

## 8. 流式 Provider 事件

如果 Provider 有 `stream()`：

```python
async for provider_event in stream_method(...):
```

可能收到：

```text
ProviderTextDelta  一小段文本
ProviderRetry      正在重试
ProviderComplete   一次完整 ModelReply
```

AgentLoop 把它们转换成统一的 `AgentEvent`，供 CLI 或其他界面消费。

## 9. 把模型回复写入历史

```python
self.messages.append(
    Message("assistant", reply.content, tool_calls=reply.tool_calls)
)
```

无论是否有工具调用，assistant 回复都会先进入历史。这很重要，因为后续 tool 消息必须紧跟其对应的 assistant 工具调用。

## 10. 无工具调用并不一定立即结束

```python
if not reply.tool_calls:
    stop_hooks = await ...
```

Stop Hook 可以验证答案，例如确认测试是否通过。若阻止完成，AgentLoop 会追加一条 user 消息：

```text
Completion was rejected ... Fix the issue, verify it, and try again.
```

然后进入下一步。这说明“模型认为完成”和“系统接受完成”是两件事。

## 11. 有工具调用时的分支

先为每个调用发出 `tool_start` 事件，然后检查是否重复相同工具批次。

重复保护使用 JSON 签名：

```python
[{"name": call.name, "arguments": call.arguments}, ...]
```

如果连续重复超过阈值，不再真正执行，而是为每个调用生成错误型 `ToolResult`。模型仍可看到错误并改变策略。

## 12. 执行结果回填

工具完成后：

1. 超长输出可能写入 Artifact；
2. 调用 `_append_tool_result()`；
3. 产生 `tool_end` 事件；
4. 下一轮模型请求读取这些新消息。

错误结果会加前缀：

```text
ERROR: Permission denied ...
```

但它仍是一条 tool 消息，而不是让循环直接崩溃。

## 13. 三类结束

```text
成功：yield done，然后 return
可恢复工具错误：写入 tool message，继续循环
不可恢复 Provider 错误：yield error，然后 return
步数耗尽：抛 MaxStepsExceeded
```

## 14. 本章练习

1. 为什么上下文错误重试不应增加外层 step？
2. Stop Hook 阻止后为什么追加的是 user 消息？
3. 工具不存在为什么通常不直接终止 run？

## 15. 参考答案

1. 模型还没有完成这一逻辑步骤，只是请求因输入过长失败；压缩后应视为同一步重试。
2. 需要把可信验证器的反馈作为新指令交给模型，让它修正。
3. 工具失败是模型可以观察并恢复的信息，例如改用别的工具或修正名称。
