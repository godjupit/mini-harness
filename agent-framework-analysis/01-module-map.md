# 01｜真实模块地图

## 1. 地图结论

**推断**：Mini OpenHarness 的形态不是“若干平级 Agent 组件”，而是三层结构：

```text
应用组合层：CLI
    ↓ 创建/连接/清理
运行控制层：AgentLoop ── Hook / Permission / Context / Trace
    ↓ 使用稳定协议
能力与适配层：Provider ｜ ToolRegistry ── Local / Skill / MCP / Docker
```

其中 `Message/ToolCall/ModelReply` 是跨层协议；权限、Trace、取消和信任边界则横切多层。详图见 `diagrams/module-dependencies.mmd`。

## 2. 系统中心

### 2.1 中央协调模块

**已确认**：`AgentLoop` 是系统运行中心。它：

- 在构造时持有 Provider、ToolRegistry、workspace 及所有可选 control-plane 依赖；`engine.py:60-121`。
- 在 `run()` 中维护有界 model/tool 循环；`engine.py:133-379`。
- 决定何时调用四类 Hook、何时 compaction、何时终止、何时把错误留作 observation。
- 将 `AgentEvent` 作为流式外部输出，而不是调用某个 EventBus。

### 2.2 副作用中心

**推断**：`ToolRegistry` 是第二中心，但它不是另一个 orchestrator，而是 capability/effect boundary。所有本地文件、Skill、MCP 与 Docker shell 最终使用同一 Tool Protocol，统一经过 JSON Schema、PermissionPolicy、approval、timeout 和错误归一化；`tools.py:95-215`。

**关键交界**：`AgentLoop._execute_timed()` 在 Registry 外包裹 pre/post Hook、资源锁和 Trace；`ToolRegistry.execute()` 在锁内执行 schema、permission、timeout 和真实 Tool。这一职责分割是当前最强耦合点。

## 3. 稳定抽象

以下“稳定”表示代码中形成清晰边界，不表示项目承诺语义版本兼容：

| 抽象 | 稳定性判断 | 依据 |
| -- | -- | -- |
| `Message / ToolCall / ModelReply` | **推断｜高** | 两个 Provider、AgentLoop、Compactor 共用；无供应商字段泄漏。 |
| `StreamingModelProvider / CompletionModelProvider` | **已确认｜高** | 显式 Protocol；AgentLoop 用 duck typing 选择 stream/complete。 |
| `Tool` | **已确认｜高** | 显式 Protocol；本地、Skill、MCP、sandbox 均实现同一形状。 |
| `Hook` | **已确认｜高** | 显式 Protocol；Executor 不按 adapter 类型分支。 |
| `AgentEvent` | **推断｜中** | 公开导出且是 `run()` 输出，但 event data payload 没有独立 schema/version。 |
| `PermissionPolicy` | **推断｜中** | 边界清晰、有测试，但 Runtime 类型固定为具体类。 |
| `ContextCompactor` / `TraceWriter` | **推断｜低至中** | 可选注入，但没有 Protocol；调用者依赖具体方法和属性。 |

## 4. 实现细节与可替换适配器

### 4.1 可替换适配器

- **Provider**：最明确的 adapter boundary。`OpenAIResponsesProvider` 与 `OpenAICompatibleProvider` 都产出 `ProviderEvent/ModelReply`；用户也可实现自己的 stream 或 complete。
- **Tool**：Tool Protocol 是 capability adapter boundary。`ReadFileTool`、`LoadSkillTool`、`McpTool`、`SandboxedShellTool` 对 Runtime 一视同仁。
- **Hook**：`CallbackHook` 与 `CommandHook` 是两个实现；新增 HTTP/policy-engine hook 无需修改 Executor。
- **MCP transport**：stdio 与 Streamable HTTP 在 `McpManager.connect_and_register()` 内选择，向上都变成 `McpTool`。

### 4.2 当前绑定的实现细节

- `ContextCompactor` 的 token 估算固定为字符数/4，summary 固定为截断式文本；`compaction.py:15-21,111-120`。
- `ArtifactStore`、`TraceWriter`、`FileOAuthStorage` 都直接绑定本地文件系统。
- CLI 直接选择 Provider class、Docker class 和各路径默认值，没有 DI container 或工厂 registry。
- Tool Schema 使用 `jsonschema.validate()`，不是独立 schema adapter。

## 5. 模块依赖解释

### 5.1 主依赖方向

1. `cli` 向内依赖所有被装配模块，是 composition root 的合理扇出。
2. `engine` 依赖 domain models、Provider contract、Tool boundary 以及四类横切/扩展机制。
3. `provider` 和 `compaction` 只依赖 domain models，形成较干净的下层。
4. `skills`、`mcp`、`sandbox` 向内依赖 Tool contract，实现依赖倒置的效果。
5. `hooks` 只直接依赖 Trace；`permissions` 与 `models` 不依赖其他包内模块。

### 5.2 谁创建谁

- CLI 创建 Provider、TraceWriter、ToolRegistry、可选 DockerSandbox/Skill Tool/McpManager、Policy、HookRegistry、Compactor、ArtifactStore 和 AgentLoop。
- AgentLoop 创建 `HookExecutor`、每次 run 的 `ToolContext`、cancel event 与 `ResourceLockManager`。
- McpManager 创建 HTTP/stdio transport、ClientSession 和一个个 `McpTool`，再注册到已有 ToolRegistry。

### 5.3 谁读取/写入状态

- 只有 AgentLoop 修改 conversation history。
- Provider 只读取 messages 并返回 reply，不持有会话历史。
- Tool 实现接收只读 frozen `ToolContext`，但可对 workspace/远端产生副作用。
- Compactor 返回新 messages；AgentLoop 决定替换状态。
- TraceWriter 被多方调用并自行串行化 sequence/file append。

## 6. 最强耦合关系

1. **AgentLoop ↔ Tool capability/scheduler**：`engine.py` 需要 Registry 的 `schemas/source/resources/execute` 四个面向；Tool scheduling 又依赖 AgentLoop 的 batch、cancel 和 message 回填语义。
2. **AgentLoop ↔ Hook payload schema**：四个 Hook payload 都是未类型化 dict，字段名由 engine 与 hook/config 共同约定；例如 `tool_input` 改写必须在 engine 中重新读取和验证。
3. **Provider ↔ Message protocol**：两个 wire-protocol 转换函数直接枚举 Message role/field；新增 role 或富内容会同时修改 models/provider/compaction。
4. **CLI ↔ 全模块构造签名**：CLI 是合理的高扇出组合根，但任何构造参数变化都要同步到 argparse 和 `_run()`。

## 7. 不合理依赖与风险

- **推断｜轻度边界泄漏**：`engine.py` 通过工具名字符串前缀 `mcp__` 和精确名称 `load_skill` 做归因与 Trace 事件；`engine.py:325-327,367-373`。这让核心运行时知道扩展实现的命名约定。更稳妥的做法是 Registry/Tool 暴露结构化 source metadata。
- **推断｜职责分裂**：资源访问模型与锁管理在 `tools.py`，批执行和 lock lifecycle 在 `engine.py`。当前规模可读，但未来 scheduler 独立演进时可能拉扯两边接口。
- **推断｜可替换性不足**：文档说生产版可换 LLM summarizer，但 `AgentLoop` 标注和调用的是具体 `ContextCompactor.compact()`；没有 Protocol，属于可通过 duck typing 实现但未声明的契约。
- **推断｜观测失败可影响业务**：Tracer 是可选横切能力，但 `emit()` 文件 I/O 异常没有隔离，理论上可中断 Agent run。这是 fail-fast 还是意外耦合，尚无测试说明。
- **推断｜history 与 run 计数语义不完全一致**：同一 AgentLoop 多轮复用时 messages/token 累计，而重复 batch/cancel/reactive retry 每轮重置；这是合理但未形成显式 Session abstraction。

## 8. 循环依赖检查

- **已确认｜静态 import**：包内没有模块级循环。`models`、`permissions` 位于叶节点；`provider/compaction` 依赖 models；工具 adapter 依赖 tools；engine 汇聚依赖；CLI 位于最外层。
- **代码实现｜局部动态 import**：`McpTool.resources()` 在方法内导入 `ResourceAccess`；`mcp.py:183-188`。但 `mcp.py` 已在模块顶部导入其他 tools 类型，因此这不是打破真实循环所必需，更多像局部避免导入或风格残留。
- **推断｜运行期反馈环不是依赖循环**：model → tool → observation → model 是业务状态机回路，不等于 Python 模块循环依赖。

## 9. 看似重要但不在主调用链上的内容

- `TraceStore.list/read/replay`：重要运维入口，但在 Agent run 主链外，只读取已经落盘的 trace。
- `FileOAuthStorage` 与 loopback OAuth：只有配置 HTTP MCP OAuth 时可达，不影响默认/demo 路径。
- `DockerSandbox`：只有显式 `--sandbox-shell` 才注册；默认工具链没有 shell。
- `DemoProvider`：位于典型 demo 路径，但不代表生产 Provider 的 SSE/HTTP 行为。
- `Message.from_dict()`：代码提供恢复能力，但主 CLI 没有加载历史文件或 session 的入口。

## 10. 未出现的架构模块

**已确认**：未发现 Workflow、Memory store、Session manager、Plugin loader、Dependency Injection container、Event bus、Web transport、Scheduler、multi-agent registry。不要因 README 使用“runtime”或相邻完整版项目存在这些概念而创建对应分析文件。
