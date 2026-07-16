    # 第 3 章：精读 CLI——依赖装配与生命周期

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

本章阅读 `src/mini_openharness/cli.py`，重点不是记住每个命令行参数，而是理解“程序在运行前需要准备哪些对象”。

建议先看：

```text
main()          323～332 行
_run()           95～214 行
_permission_policy() 226～232 行
_approval_callback() 235～250 行
```

## 2. main 是同步入口，_run 是异步主函数

```python
def main(argv=None) -> int:
    _load_environment()
    ...
    return asyncio.run(_run(...))
```

初学者需要理解两层：

- 普通终端程序从同步函数 `main()` 开始；
- Agent 内部大量使用网络和并发，所以真正工作放在 `async def _run()`；
- `asyncio.run()` 创建事件循环并运行异步函数。

`main()` 还判断第一个参数是否是 `trace`，因此同一命令支持：

```bash
mini-oh "任务"
mini-oh trace list
```

## 3. composition root：所有零件在这里组装

`_run()` 是项目的 composition root，可以译作“依赖装配根”。它不应该亲自实现模型协议、文件工具或权限规则，而是创建这些对象并把它们连接起来。

主顺序：

```text
解析 workspace 和 prompt
→ 发现 Skills，拼 system prompt
→ 创建 Provider
→ 创建 TraceWriter
→ 注册默认和可选工具
→ 创建 MCP Manager
→ 创建权限和 Hooks
→ 创建 AgentLoop
→ 消费 run() 产生的事件
→ 最后关闭资源
```

## 4. 工作区边界

```python
workspace = Path(args.workspace).resolve()
```

`Path` 是 Python 的路径对象。`resolve()` 把相对路径转成绝对路径，并尽量消除 `.`、`..` 等片段。

后续文件工具会确保目标路径仍在这个 workspace 内。也就是说，CLI 先定义边界，具体工具再执行边界检查。

## 5. System Prompt 是怎样拼起来的

```python
system_parts = [
    "You are a concise coding assistant. Inspect before editing."
]
skill_prompt = skills.prompt()
if skill_prompt:
    system_parts.append(skill_prompt)
```

最后传入：

```python
system_prompt="\n\n".join(system_parts)
```

这体现了一个简单但实用的模式：先用列表收集多个提示片段，最后统一拼接。

## 6. Provider 的选择

```text
--demo                  → DemoProvider
--api-mode responses    → OpenAIResponsesProvider
--api-mode chat         → OpenAICompatibleProvider
```

如果不是 demo 且没有 API Key，CLI 直接停止。这是“早失败”：与其让请求到更深处才报认证错误，不如在装配阶段给清楚提示。

## 7. 工具注册

```python
tools = default_tools()
```

先得到包含 `read_file`、`list_files`、`write_file` 的注册表。然后根据参数追加：

```text
--sandbox-shell → SandboxedShellTool
存在 Skills     → LoadSkillTool
--mcp-config    → 连接 MCP 后注册远程工具
```

关键设计是：这些来源不同的工具最终进入同一个 `ToolRegistry`。AgentLoop 不需要为每种来源写一套执行逻辑。

## 8. 创建 AgentLoop 时发生依赖注入

```python
loop = AgentLoop(
    provider=provider,
    tools=tools,
    workspace=workspace,
    permission_policy=policy,
    approval_callback=approval,
    tracer=tracer,
    compactor=ContextCompactor(...),
    artifact_store=ArtifactStore(...),
    hooks=hooks,
)
```

所谓依赖注入，就是 `AgentLoop` 不在内部偷偷创建固定 Provider 或固定工具，而是由外部把依赖传入。这样测试可以传入假的 Provider，真实运行可以传入网络 Provider。

## 9. async for 消费 Agent 事件

```python
async for event in loop.run(prompt):
    _print_event(event)
```

`loop.run()` 不是一次性返回最终答案，而是异步生成一连串事件，例如：

```text
model_start
assistant_delta
assistant
tool_start
tool_end
done
```

CLI 只负责把这些事件打印成适合人看的终端文本。这实现了“运行逻辑”和“显示逻辑”的分离。

## 10. try/finally：无论成功失败都要清理

```python
finally:
    if mcp_manager:
        await mcp_manager.close()
    close = getattr(provider, "close", None)
    if close is not None:
        await close()
```

网络客户端、MCP 进程和会话都属于资源。如果不关闭，可能出现连接泄漏或子进程残留。

`finally` 表示：无论 `try` 中成功、报错还是提前返回，这段清理逻辑都应当执行。

## 11. 权限回调为什么可能为 None

非交互环境中：

```python
if not sys.stdin.isatty():
    return None
```

例如 CI 流水线没有用户坐在终端前输入 `y`。此时不能假装获得批准。工具执行层会把 `ask` 决策安全地当作未批准。

## 12. 本章练习

1. 为什么说 `_run()` 是装配者，而不是业务实现者？
2. 如果要增加一个新的本地工具，最自然的接入点在哪里？
3. 为什么 Provider 和 MCP Manager 必须在 `finally` 中关闭？

## 13. 参考答案

1. 它主要创建对象、连接依赖和管理生命周期，具体模型/工具逻辑位于其他模块。
2. 可以加入 `default_tools()`，或在 CLI 创建注册表后按配置 `tools.register(...)`。
3. 运行中可能在任意阶段失败；`finally` 能保证资源回收。
