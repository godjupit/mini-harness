# 00｜项目概览

## 1. 分析范围与仓库事实

- **已确认**：分析对象是 `/home/godjupit/harness/mini-openharness`。工作区同级另有完整项目 `OpenHarness/`，但 Mini 有独立 `.git`、`pyproject.toml`、包、测试和版本号，因此不把完整版代码纳入模块依赖统计。
- **已确认**：包名为 `mini-openharness`，版本 `0.6.0`，支持 Python 3.10+；运行依赖只有 `httpx`、`jsonschema`、`mcp`、`python-dotenv` 和 `sse-starlette`。依据：`pyproject.toml:5-18`。
- **文档声称**：项目目标是“面试中讲清楚、现场跑通”的精简 coding-agent runtime，并刻意不实现 TUI、插件市场和多 Agent。依据：`README.md:1-3`、`README.md:292-300`。
- **代码实现**：包内共 13 个 Python 文件；没有子包，职责按文件横向展开，但真实模块边界并不完全等同于文件边界。例如资源锁定义在 `tools.py`，真正的批调度却在 `engine.py`。

## 2. 顶层结构

| 路径 | 真实职责 | 架构意义 |
| -- | -- | -- |
| `src/mini_openharness/` | 可安装 Python 包 | 所有运行时实现 |
| `tests/` | 82 个通过的 pytest case | 主要行为证据和边界验证 |
| `examples/` | 权限、Hook、MCP 配置与 MCP server | CLI 配置入口的最小集成示例 |
| `skills/repository-guide/SKILL.md` | 内置示例 Skill | 渐进披露链路的实际输入 |
| `README.md` | 用户入口与能力说明 | 文档声称，需与代码交叉验证 |
| `TECHNICAL_DESIGN.md` | 设计取舍与不变量 | 解释意图，但不能替代实现证据 |
| `.env.example` | Provider 环境变量样例 | CLI 环境加载入口 |

## 3. 系统入口分级

### 3.1 主要入口

1. **已确认｜CLI**：`pyproject.toml:23-24` 将 `mini-oh` 映射到 `mini_openharness.cli:main`。`main()` 加载当前目录 `.env`，区分 `trace` 子命令与 agent run，然后用 `asyncio.run()` 进入 `_run()`；依据：`src/mini_openharness/cli.py:323-337`。
2. **已确认｜库 API**：包根导出 `AgentLoop`、两种生产 Provider、`ModelProvider`、`ToolRegistry/default_tools`、Docker sandbox 与 `SkillCatalog`；依据：`src/mini_openharness/__init__.py:3-24`。
3. **已确认｜运行启动方法**：`AgentLoop.run(prompt)` 是异步生成器，以 `AgentEvent` 暴露流式运行事件；依据：`src/mini_openharness/engine.py:133-379`。

### 3.2 次要入口

- **已确认｜Trace 查询**：`mini-oh trace list/show/replay` 进入 `_trace_command()` 和 `TraceStore`，不进入 Agent Loop；依据：`src/mini_openharness/cli.py:253-279`。
- **已确认｜配置加载器**：`PermissionPolicy.from_file()`、`load_hook_registry()`、`McpManager.from_file()` 分别解析独立 JSON 配置；它们是 CLI 组装时的次要入口，而非统一配置系统。
- **已确认｜程序化 Hook API**：用户可创建 `HookRegistry` 并注册 `CallbackHook`；`README.md:161-178` 给出示例，`src/mini_openharness/hooks.py:193-293` 实现。

### 3.3 内部入口

- `ToolRegistry.execute()`：所有本地、Skill、MCP、sandbox 工具的统一执行入口。
- `StreamingModelProvider.stream()` / `CompletionModelProvider.complete()`：Runtime 对模型端的结构化边界。
- `McpManager.connect_and_register()`：在运行前连接远端服务并将其工具动态加入 Registry。
- `HookExecutor.execute()`：四个生命周期点的统一调度入口。

### 3.4 实验性、废弃或不可达入口

- **已确认**：没有弃用标记、compat shim 或 legacy API。
- **推断**：`DemoProvider` 是演示/测试适配器，不是废弃路径；README 的 30 秒示例直接使用它，测试与 CLI 均可达。
- **已确认**：没有 Web API、scheduler、workflow DSL、plugin loader、多 Agent 或独立 session/storage 入口。

## 4. 最小可运行示例

```bash
.venv/bin/mini-oh --demo --workspace . "解释这个项目"
```

**已确认**：该命令不需要 API key。`cli._run()` 创建 `SkillCatalog`、`DemoProvider`、Trace、默认文件工具、可选 `load_skill` 工具、权限策略、Compactor 与 ArtifactStore，然后调用 `AgentLoop.run()`；依据：`src/mini_openharness/cli.py:95-214`。

**代码实现**：`DemoProvider.complete()` 依次请求 `list_files`、可用时请求 `load_skill`、请求 `read_file README.md`，最后返回无 tool call 的回答；依据：`src/mini_openharness/provider.py:529-552`。这是一条覆盖真实闭环的确定性路径，不是伪造 CLI 输出。

## 5. 核心抽象与状态边界

| 抽象 | 状态/契约 | 替换性 | 证据 |
| -- | -- | -- | -- |
| `AgentLoop` | 拥有消息历史、token/cost、循环熔断计数、cancel event、资源锁 | 中央实现，不是 Protocol | `engine.py:57-139` |
| `Message` / `ToolCall` / `ModelReply` | Provider-neutral 不可变对话协议 | 稳定交换对象 | `models.py:9-47` |
| `StreamingModelProvider` / `CompletionModelProvider` | 一次模型 turn 的流式或完成式接口 | 可替换 | `provider.py:75-91` |
| `Tool` | 名称、描述、JSON Schema、effect 声明和 async `run` | 可扩展 | `tools.py:95-103` |
| `Hook` | 生命周期策略的优先级、matcher、timeout、failure mode 与执行接口 | 可扩展 | `hooks.py:80-93` |
| `PermissionPolicy` | 有序规则 + mutation 默认动作 | 可配置，当前非 Protocol | `permissions.py:30-84` |
| `TraceWriter` | 单次 run 的 sequence、时间与 JSONL 文件 | 可选依赖，当前非 Protocol | `trace.py:25-68` |
| `McpManager` | MCP session/exit-stack 生命周期 | 外部适配器 | `mcp.py:37-155` |

## 6. 状态在哪里创建和修改

- **对话状态**：`AgentLoop.__init__()` 初始化或接收 `messages`，强制刷新首个 system message；`run()` 追加 user、assistant、tool 消息；compaction 会整体替换 `self.messages`。依据：`engine.py:108-121`、`engine.py:152-153`、`engine.py:263-265`、`engine.py:543-563`、`engine.py:570-574`。
- **运行计数**：输入/输出 token 跨多次 `run()` 累计，而重复 tool batch 计数与 cancel event 每次 `run()` 重置；依据：`engine.py:117-121`、`engine.py:133-139`、`engine.py:263-264`。这是可复用 loop 的重要语义差异。
- **工具注册状态**：`ToolRegistry._tools` 由 CLI 在 run 前构建并可被 Skill、sandbox、MCP 扩展；运行期间代码没有动态注销。
- **外部持久状态**：Trace 写入 JSONL；大工具输出写入 artifact 文本；OAuth token/client info 写入权限 0600 的 JSON。项目没有通用 session database 或 memory store。

## 7. 外部系统与边界

| 外部系统 | 接入位置 | 生命周期 | 默认安全姿态 |
| -- | -- | -- | -- |
| OpenAI/兼容 HTTP API | `provider.py` | Provider client 由 CLI 创建并最终 close | typed error；只在输出前对可重试错误退避 |
| MCP stdio server | `mcp.py` | `AsyncExitStack` 管理进程/流/session | annotation 默认不可信，非只读默认需审批 |
| MCP Streamable HTTP/OAuth | `mcp.py` + `mcp_auth.py` | HTTP client、transport、session、token storage | HTTPS/loopback、PKCE S256、0600 原子 token 文件 |
| Docker CLI/daemon | `sandbox.py` | 每次工具调用一个 disposable container | opt-in、无 host fallback、network none、资源限制 |
| Hook 子进程 | `hooks.py::CommandHook` | 每次 Hook 启动 argv 子进程 | 不经 shell，默认最小环境；仍属于受信任代码 |
| 本地文件系统 | tools/trace/artifacts/OAuth | workspace 或显式 storage root | workspace containment、secret deny；不同 store 边界不同 |

## 8. 架构特征与非目标

- **已确认**：没有显式 Workflow/Node/Graph；Agent 行为由 `AgentLoop.run()` 中的有界循环和分支实现。
- **已确认**：没有独立 Memory/Session 模块；历史就是进程内 `AgentLoop.messages`，持久化只覆盖 Trace、Artifact 与 OAuth 凭据。
- **已确认**：没有独立 Prompt 模块；默认 system prompt 在 `AgentLoop` 与 CLI 中构造，Skill 元数据追加到 system prompt。
- **已确认**：没有插件加载系统；真正的扩展点是 Provider Protocol、Tool Protocol/Registry、Hook Protocol/Registry、Skill 文件目录与 MCP 动态工具注册。
- **推断**：这是一种“组合根 + 小型状态机 + capability registry”的架构，而不是 Workflow 框架或依赖注入容器。

## 9. 验证基线

阶段零执行 `.venv/bin/pytest -q` 得到 `82 passed in 2.46s`。因此本文引用的主循环、Provider、工具、权限、Hook、compaction、Trace、Skill、MCP 和 sandbox 行为均至少有对应测试；真实付费 Provider 调用不在普通测试中，Docker 集成测试在环境不可用时可能 skip，具体限制见开放问题。
