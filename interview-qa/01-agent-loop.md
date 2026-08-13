# 问题 1：完整讲述 Agent Loop

> 请完整讲述用户提交 Prompt 后，`AgentLoop` 从接收输入到最终返回 `done` 的调用流程。同时说明模型返回 Tool Call 时系统如何处理，以及工具执行失败时为什么 Agent 不一定立即退出。

回答这道题时，建议先给面试官一句结论：

> `AgentLoop` 本质上是一个 `model → tools → observations → model` 的循环。Runtime 负责维护消息历史、执行模型请求、控制工具副作用，并一直循环到模型不再调用工具且通过完成验证为止。

然后沿着真实代码往下讲。

首先要区分会话状态和单次运行状态。

[engine.py：ConversationState 和 RunState](../src/mini_openharness/engine.py#L62)

关键代码：

```python
@dataclass
class ConversationState:
    messages: list[Message]
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunState:
    run_id: str
    cancel_event: asyncio.Event
    resource_locks: ResourceLockManager
    tool_slots: asyncio.Semaphore
    file_snapshots: FileSnapshotStore
    last_tool_batch: str | None = None
    repeated_tool_batches: int = 0
```

`ConversationState` 保存多轮对话需要继续使用的消息历史和累计 Token。`RunState` 只属于当前这一次运行，保存取消事件、资源锁、并发槽、文件快照和重复调用计数。

这样拆分是为了避免一次运行的取消状态、锁和重复计数污染下一轮，同时又能让多轮对话继续共享历史消息。

用户真正调用的入口是 `run()`。

[engine.py：AgentLoop.run()](../src/mini_openharness/engine.py#L180)

关键代码：

```python
async def run(self, prompt: str) -> AsyncIterator[AgentEvent]:
    if self._active_run is not None:
        raise RunAlreadyActiveError(...)

    state = RunState(
        run_id=uuid4().hex,
        cancel_event=asyncio.Event(),
        resource_locks=ResourceLockManager(),
        tool_slots=asyncio.Semaphore(self.max_concurrent_tools),
        file_snapshots=FileSnapshotStore(),
    )
    self._active_run = state
    execution = self._run(prompt, state)
    try:
        async for event in execution:
            yield event
    finally:
        try:
            await execution.aclose()
        finally:
            if self._active_run is state:
                self._active_run = None
```

这一层主要管理生命周期，而不是处理具体业务。它先阻止同一个 `AgentLoop` 出现重叠运行，再为本轮创建独立的 `RunState`。

这里使用 async generator，所以模型增量、工具开始、工具结束和最终完成等事件都可以实时交给调用方。最外层的 `finally` 很重要：无论正常结束、抛出异常、任务取消，还是调用方提前关闭生成器，都要释放 active run。

接下来进入真正的状态机 `_run()`。Prompt 不会直接进入消息历史，而是先经过 Hook。

[engine.py：处理 Prompt 和创建 ToolContext](../src/mini_openharness/engine.py#L207)

关键逻辑可以简化为下面的伪代码：

```python
prompt_result = await hooks.execute(USER_PROMPT_SUBMIT, {"prompt": prompt})

if prompt_result.blocked:
    yield error
    return

prompt = prompt_result.payload.get("prompt", prompt)
messages.append(Message("user", prompt))

context = ToolContext(
    workspace=self.workspace,
    permission_policy=self.permission_policy,
    approval_callback=self.approval_callback,
    tracer=self.tracer,
    file_snapshots=state.file_snapshots,
)
```

Prompt Hook 可以拒绝输入，也可以在进入 history 之前修改输入。`ToolContext` 则把 workspace、权限、审批、Trace、超时和文件快照统一注入后续工具，避免工具直接依赖整个 `AgentLoop`。

准备完成后进入受 `max_steps` 限制的主循环。

[engine.py：Agent 主循环](../src/mini_openharness/engine.py#L233)

伪代码：

```python
for step in range(1, max_steps + 1):
    if cancelled:
        yield cancelled
        return

    compact_context_if_needed()
    reply = await provider(messages, tools.schemas())

    messages.append(
        Message(
            "assistant",
            reply.content,
            tool_calls=reply.tool_calls,
        )
    )

    if not reply.tool_calls:
        try_to_finish()
    else:
        execute_tools_and_append_results()
```

每轮先检查取消，再检查是否需要压缩上下文，然后调用 Provider。Provider 可以持续返回文本增量，Runtime 会把这些增量转换成 `assistant_delta` 事件；拿到完整 `ModelReply` 后，再把 Assistant Message 加入会话历史。

这里的数据结构很简单。

[models.py：ToolCall、Message 和 ModelReply](../src/mini_openharness/models.py#L13)

关键代码：

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ModelReply:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
```

如果模型没有返回 Tool Call，说明它想结束，但 Runtime 还要经过最后一道验证。

[engine.py：没有 Tool Call 时的完成逻辑](../src/mini_openharness/engine.py#L353)

伪代码：

```python
if not reply.tool_calls:
    stop_result = await hooks.execute(STOP, {"response": reply.content})

    if stop_result.blocked:
        messages.append(
            Message(
                "user",
                "Completion was rejected. Fix the issue and try again.",
            )
        )
        continue

    tracer.finish(status="completed")
    yield AgentEvent("done")
    return
```

Stop Hook 可以执行测试、lint 或安全验证。验证成功才真正返回 `done`；验证失败时，失败原因会进入消息历史，让模型继续修复，而不是直接把一个未经验证的答案交给用户。

如果模型返回 Tool Call，Runtime 会先检查它是否一直重复相同调用，然后再执行工具。

[engine.py：重复调用保护和工具结果回填](../src/mini_openharness/engine.py#L398)

伪代码：

```python
repeated = record_tool_batch(reply.tool_calls)

if repeated > max_repeated_tool_batches:
    results = create_loop_guard_errors()
else:
    results = await execute_all(reply.tool_calls)

for call, result in zip(reply.tool_calls, results):
    append_tool_result(call.id, call.name, result)
```

`max_steps` 限制总轮数，重复 Tool Batch 检测则专门阻止模型连续提交完全相同的工具名和参数。这两个保护解决的是不同层次的无限循环问题。

多个工具会并发启动，但是并不代表所有副作用都可以并发。

[engine.py：并发执行和取消处理](../src/mini_openharness/engine.py#L649)

关键代码：

```python
return await asyncio.gather(
    *(self._execute_timed(call, context, state) for call in calls)
)
```

`gather()` 负责并发启动并保持结果顺序。与此同时，Runtime 还单独等待 `cancel_event`；如果用户取消，就取消整个工具 batch，并等待任务真正退出，避免留下后台任务。

每个工具还要经过并发槽、Hook 和资源锁。

[engine.py：单个工具的执行边界](../src/mini_openharness/engine.py#L471)

这部分可以记成：

```text
Semaphore 并发槽
→ Pre Tool Hook
→ 使用修改后的参数解析资源
→ 获取 ResourceLock
→ ToolRegistry.execute()
→ 释放 ResourceLock
→ Post Tool Hook
```

Semaphore 控制工具总并发量，资源锁负责副作用冲突。例如两个不同文件的写入可以并发，但同一个文件的读写或写写必须串行。

拿到资源锁以后，真正的工具安全边界在 `ToolRegistry.execute()`。

[tools.py：ToolRegistry.execute()](../src/mini_openharness/tools.py#L306)

它的执行顺序可以概括为：

```text
lookup
→ JSON Schema validate
→ permission authorize
→ timeout-wrapped execute
→ normalize result
```

Runtime 不能因为参数来自模型就直接信任它。参数校验和权限判断必须发生在真实副作用之前；工具超时、异常或返回类型错误则会被转换成统一的 `ToolResult` 和 `ToolFailure`。

工具执行完以后，结果通过原始调用 ID 加回消息历史。

[engine.py：追加 Tool Message](../src/mini_openharness/engine.py#L719)

关键代码：

```python
self.messages.append(
    Message(
        "tool",
        prefix + result.output,
        tool_call_id=call_id,
        name=name,
    )
)
```

配对关系是：

```text
Assistant ToolCall(id="call_123")
             ↓
Tool Message(tool_call_id="call_123")
```

下一轮模型调用可以看到这条 observation，然后决定继续调用工具、修改参数或者生成最终答案。

最后解释为什么工具失败不一定让 Agent 退出。

文件不存在、参数不合法、权限拒绝、编辑内容没有匹配或者工具超时，通常都是模型可以理解并修复的任务级错误。Runtime 会把错误作为 Tool Message 回填，例如：

```text
模型调用 read_file("wrong.py")
→ 工具返回 File not found
→ 错误作为 observation 加入 history
→ 模型看到错误后改用正确路径
```

自然语言 `output` 让模型理解失败原因，结构化的 `code`、`stage` 和 `retryable` 则供 Runtime、Trace 和测试判断。

Provider 错误不一样。它意味着这一轮没有拿到完整有效的模型回复，Agent 无法决定下一步，所以通常会终止当前运行。明确的上下文窗口超限是例外。

[engine.py：ProviderContextWindowError 的恢复](../src/mini_openharness/engine.py#L291)

Runtime 会强制压缩一次上下文，并在同一个逻辑模型步骤中进行一次受控重试。如果仍然失败，就结束运行，避免无限重试。

面试现场可以把整段回答收束成下面这条主线：

```text
创建 RunState
→ Prompt Hook
→ 上下文检查
→ Provider
→ Assistant Message
→ Tool 调度
→ Pre Hook
→ 资源锁
→ 校验、权限和执行
→ Post Hook
→ Tool Result 回填
→ 下一轮 Provider
→ Stop Hook
→ Done
```

最后再强调三个设计点就够了：会话状态和运行状态分离；工具并发由 Semaphore 和资源锁共同控制；工具错误作为可恢复 observation，Provider 错误通常作为运行级错误处理。
