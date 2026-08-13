如果目标是理解 Mini OpenHarness 的核心机制，优先掌握下面 6 个模块就够了：

```text
cli.py（组装）
   ↓
engine.py（调度主循环）
   ├── provider.py（调用模型）
   ├── tools.py（执行工具）
   ├── permissions.py（授权判断）
   └── models.py（消息与调用的数据结构）
```

## 第一优先级：核心运行链路

1. [models.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/models.py:1)

最小、最适合先读。主要理解：

- `Message`：对话消息
- `ToolCall`：模型发出的工具调用
- `ModelReply`：一次模型回复

这是其他模块之间传递数据的共同语言。

2. [tools.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/tools.py:209)

重点最多，主要看：

- `Tool` Protocol：一个工具必须提供什么
- `ToolRegistry.register()`：注册工具
- `ToolRegistry.schemas()`：把工具定义提供给模型
- `ToolRegistry.execute()`：参数校验并执行工具
- `ToolDescriptor`：工具来源、读写属性和路径信息
- `ResourceLockManager`：并发工具之间的资源冲突控制
- `FileSnapshotStore`：防止基于旧文件内容进行编辑
- `ReadFileTool`、`WriteFileTool`、`EditFileTool`：内置工具示例

这是 mini 的工具系统和主要安全边界。

3. [provider.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/provider.py:75)

重点理解“内部数据如何变成模型 API 请求”：

- `ModelProvider` 协议
- `OpenAICompatibleProvider._payload()`：Chat Completions 请求格式
- `OpenAIResponsesProvider._payload()`：Responses API 请求格式
- `_to_openai_message()` / `_to_responses_items()`：消息转换
- `_tool_call_from_parts()`：把流式工具调用拼接回来
- `_status_error()`：API 错误分类
- `DemoProvider`：不联网的测试 Provider

你刚才问的工具描述传递，核心就在 `ToolRegistry.schemas()` 和这两个 `_payload()` 方法里。

4. [engine.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/engine.py:91)

这是整个项目的核心。

重点阅读：

- `AgentLoop.run()`：一次运行的外层生命周期
- `AgentLoop._run()`：模型与工具之间的循环
- `_execute_all()`：并行执行多个工具
- `_execute_with_slot()`：并发限制、权限和资源锁
- `_append_tool_result()`：把执行结果放回对话
- `_record_tool_batch()`：防止模型重复调用相同工具
- `_compact_if_needed()`：上下文压缩
- `AgentEvent`：向 CLI/UI 输出运行事件

主流程可以概括为：

```text
用户输入
→ 调用 Provider
→ 收到 ModelReply
→ 没有 ToolCall：结束
→ 有 ToolCall：权限检查
→ 执行工具
→ 将结果追加为 Message
→ 再次调用 Provider
```

## 第二优先级：装配与安全

5. [permissions.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/permissions.py:17)

主要看：

- `PermissionRule`
- `PermissionPolicy.evaluate()`
- 只读工具为什么可以自动通过
- 写操作什么时候需要确认
- 工具来源、工具名和路径规则如何匹配

注意它和 `tools.py` 的职责区别：

- `ToolDescriptor` 描述工具有什么副作用。
- `PermissionPolicy` 决定本次调用能不能执行。
- `ToolRegistry.execute()` 负责真正执行。

6. [cli.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/cli.py:124)

这是 composition root，即程序组装入口。重点看 `_run()`：

- 创建 Provider
- 创建默认工具注册表
- 加载 Skill
- 连接 MCP
- 构造权限策略
- 构造 `AgentLoop`
- 消费并打印 `AgentEvent`
- 最后关闭 Provider、MCP 和 Sandbox

不建议第一篇就通读全部 CLI 参数；先看 [cli.py:124](/home/godjupit/harness/mini-openharness/src/mini_openharness/cli.py:124) 到 `AgentLoop` 创建完成即可。

## 第三优先级：扩展能力

核心链路理解后再看：

- [compaction.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/compaction.py:33)：上下文压缩和超长工具输出落盘。
- [skills.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/skills.py:20)：扫描 Skill，并通过 `LoadSkillTool` 按需加载完整指令。
- [mcp.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/mcp.py:37)：连接 MCP Server，将远程 MCP 工具适配成普通 `Tool`。
- [hooks.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/hooks.py:193)：模型调用和工具执行前后的生命周期扩展。
- [trace.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/trace.py:19)：记录、脱敏和回放运行轨迹。
- [sandbox.py](/home/godjupit/harness/mini-openharness/src/mini_openharness/sandbox.py:35)：用 Docker 隔离 Shell 工具。
- `mcp_auth.py`：MCP OAuth，最后再看。

推荐阅读顺序是：

```text
models.py
→ tools.py 的 Tool、ToolRegistry
→ provider.py 的两个 _payload()
→ engine.py 的 run()、_run()
→ permissions.py
→ cli.py 的 _run()
→ compaction / skills / MCP / hooks / trace / sandbox
```

如果时间有限，只精读 `models.py + tools.py + provider.py + engine.py`，你就能解释这个 mini Agent 约 80% 的核心工作原理。