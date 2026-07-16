# 模块计划与执行结果

> 阶段零形成计划；阶段一至四已按计划完成全部 14 个分析主题。  
> 计划原则：按职责、状态边界、替换性、调用阶段和测试证据聚类，不按每个 Python 文件机械拆分。

## 执行状态

- M01–M04：阶段一完成，位于 `modules/01-04`。
- M05、M07、M08、M10：阶段二完成，位于 `modules/05-08`。
- M11–M12：阶段二完成，位于 `integrations/`。
- M06、M09、X01、X02：阶段三完成，位于 `cross-cutting/`。
- **已确认**：深入分析未发现需要新增、拆分或合并的模块；阶段零划分保持成立。

## 1. 候选模块总表

| 编号 | 建议模块名 | 模块类型 | 涉及代码 | 核心职责 | 拆分理由 | 优先级 | 是否独立成文 |
| -- | ----- | ---- | ---- | ---- | ---- | --- | ------ |
| M01 | AgentLoop 运行编排 | Core | `engine.py::AgentLoop`、`AgentEvent` | 驱动 model→tools→model 状态机，管理消息、终止、熔断与取消 | 系统中心；独立状态和完整生命周期；测试最密集 | P0 | 是 |
| M02 | 内部对话与工具调用协议 | Domain | `models.py::Message/ToolCall/ModelReply` | 在 Runtime、Provider、Tool 之间传递 provider-neutral 数据 | 稳定数据边界；序列化与协议配对不变量贯穿主链 | P0 | 是 |
| M03 | 流式模型 Provider 边界 | Adapter | `provider.py` | 把 Responses/Chat SSE、错误和 tool calls 标准化 | 两个可替换 Protocol；独立网络生命周期、异常族和协议测试 | P0 | 是 |
| M04 | 工具能力边界与 effect-aware 调度 | Core | `tools.py`；`engine.py::_execute_timed/_execute_all` | 注册/描述工具，校验参数、授权、timeout，并按资源冲突调度 | 公开扩展点；副作用边界；独立并发状态和大量测试 | P0 | 是，合并 Registry 与调度并设交互章节 |
| M05 | CLI 组合根与 Trace 子命令 | Infrastructure | `cli.py`、`pyproject.toml` | 解析参数、装配对象、管理外部资源并映射退出码 | 主要用户入口和唯一完整 composition root | P1 | 是 |
| M06 | 权限策略与人工审批 | Cross-cutting | `permissions.py`；`tools.py:165-198`；`cli.py:226-250` | 对 capability/effect 做 allow/deny/ask 决策 | 独立规则状态、配置文件、人工回调和测试；横跨所有 Tool 实现 | P1 | 是 |
| M07 | 生命周期 Hook 与 Verification Gate | Extension | `hooks.py`；`engine.py` 四个调用点 | 在 prompt、工具前后与完成前执行可信策略 | 独立 Protocol/Registry/Executor、生命周期、异常策略和配置 | P1 | 是 |
| M08 | 上下文压缩与 Artifact offload | Infrastructure | `compaction.py`；`engine.py::_compact_if_needed/_offload` | 控制 history 体积并把大输出完整保存到外部文件 | 独立状态转换；维护 tool-call 原子性；支持 reactive retry | P1 | 是，两个机制因共同服务 context budget 而合并 |
| M09 | JSONL Trace 与安全 Replay | Cross-cutting | `trace.py`；CLI/Engine/Tools/Hooks 调用点 | 记录执行证据、脱敏、查询和无副作用回放 | 跨全链路、独立持久化格式、线程安全状态和 CLI 入口 | P1 | 是 |
| M10 | Skill 渐进披露 | Extension | `skills.py`；CLI system prompt 组装 | 发现 metadata，按模型请求加载完整 `SKILL.md` | 有独立目录契约、Catalog API 和 Tool adapter；规模较小 | P2 | 是（短文） |
| M11 | MCP 工具桥接与 OAuth | Adapter | `mcp.py`、`mcp_auth.py` | 管理 stdio/HTTP MCP session，把远端工具适配进 Registry，并处理 OAuth | 独立第三方协议、连接生命周期、trust boundary 与专门测试 | P1 | 是；OAuth 依附 MCP，不另拆 |
| M12 | Docker-only sandbox shell | Adapter | `sandbox.py` | 以一次性受限容器提供 opt-in shell Tool | 独立外部系统、资源生命周期、异常与安全边界 | P1 | 是 |
| X01 | 异步、取消、超时与资源生命周期 | Cross-cutting | `engine.py`、`provider.py`、`tools.py`、`hooks.py`、`mcp.py`、`sandbox.py` | 解释 task、async generator、timeout、cancel 与 cleanup 如何贯穿系统 | 横跨至少六个模块；单模块分析会遗漏传播与清理语义 | P1 | 是 |
| X02 | 信任边界与 secret hygiene | Cross-cutting | `tools.py`、`permissions.py`、`hooks.py`、`trace.py`、`mcp*.py`、`sandbox.py` | 汇总 workspace、审批、凭据、远端 trust 与 OS 隔离边界 | 安全结论由多层共同成立；不是某一个目录的职责 | P1 | 是 |

## 2. 每个候选模块的依赖与证据

### M01｜AgentLoop 运行编排

- **独立理由**：`AgentLoop` 拥有 `messages`、token/cost、重复 batch 计数、cancel event 与资源锁；`run()` 覆盖一次用户任务的完整生命周期。
- **调用链位置**：对外 API 之后、Provider/Tool 之前，是中央协调节点。
- **依赖**：M02、M03、M04、M06、M07、M08、M09。
- **被依赖**：M05；库用户直接依赖；测试构造 `AgentLoop`。
- **用户可见 API**：是，包根直接导出。
- **扩展点**：自身不是 Protocol，但通过构造参数接收 Provider、Registry、Hooks、Policy、Tracer、Compactor/Store。
- **证据状态**：**已确认**，`engine.py:57-574` 与 `tests/test_engine.py`、`tests/test_hooks.py` 直接覆盖。

### M02｜内部对话与工具调用协议

- **独立理由**：三个 frozen dataclass 是跨边界共享语言，且 `Message` 明确支持序列化/恢复；tool call/result 的 ID 配对是核心不变量。
- **调用链位置**：贯穿模型输入、模型输出、工具 observation 和 compaction。
- **依赖**：无包内依赖。
- **被依赖**：M01、M03、M08。
- **用户可见 API**：未从包根导出，但模块路径可访问；主要是内部稳定抽象。
- **扩展点**：否；修改字段会影响 Provider 转换和 history。
- **证据状态**：**已确认**，`models.py:9-47`；稳定性级别仍属**推断**，未声明兼容策略。

### M03｜流式模型 Provider 边界

- **独立理由**：两个 Protocol 与统一事件族隔离供应商协议；拥有 HTTP client 生命周期、独立错误分类、重试和密集协议测试。
- **调用链位置**：AgentLoop 的模型调用阶段，输入 messages/tool schemas，输出 delta/retry/complete。
- **依赖**：M02；外部 `httpx`。
- **被依赖**：M01、M05；库用户可自定义实现。
- **用户可见 API**：是，包根导出 `ModelProvider` 与两种生产 adapter。
- **扩展点**：是，实现 `stream()` 或 `complete()` 即可。
- **证据状态**：**已确认**，`provider.py:15-552`、`tests/test_provider.py`。

### M04｜工具能力边界与 effect-aware 调度

- **独立理由**：Tool Protocol/Registry 是公开 capability registry；ResourceAccess/LockManager 有独立并发状态；执行阶段统一执行 schema、permission、timeout 和错误 observation 转换。
- **调用链位置**：模型返回 tool calls 后、结果写回 history 前。
- **依赖**：M06；`engine.py` 中的调度胶水依赖 M07、M09。
- **被依赖**：M01、M10、M11、M12；所有工具实现依赖它。
- **用户可见 API**：是，包根导出 `ToolRegistry/default_tools`；`Tool` Protocol 未从包根导出。
- **扩展点**：是，注册满足 Tool Protocol 的对象，可选提供 `resources()`。
- **证据状态**：**已确认**，`tools.py:21-318`、`engine.py:381-521`、`tests/test_tools.py`、并发相关 engine tests。
- **划分决定**：Registry 与调度横跨两个源文件，但共享一次 tool-call lifecycle，拆开会重复 schema/effect/resource 交互，因此先合并成文。

### M05｜CLI 组合根与 Trace 子命令

- **独立理由**：唯一同时实例化 Provider、Tool、Skill、MCP、Policy、Hook、Trace、Compactor、Artifact 与 Sandbox 的位置，并负责外部资源 close 和 exit code。
- **调用链位置**：最外层用户入口；在 M01 之前装配，在运行后清理。
- **依赖**：除 M02 外几乎所有职责模块。
- **被依赖**：console script 与最终用户。
- **用户可见 API**：是，主要 CLI；`main` 未从包根导出。
- **扩展点**：参数驱动的组合入口，不是抽象扩展点。
- **证据状态**：**已确认**，`cli.py:35-341`、`tests/test_cli.py`。

### M06｜权限策略与人工审批

- **独立理由**：有序 glob rules、mutation 默认动作和 async approval callback 构成独立策略状态；有配置入口和专项测试。
- **调用链位置**：Tool 参数通过 Schema 后、真实副作用前。
- **依赖**：无包内运行依赖。
- **被依赖**：M04、M05、M01 的 ToolContext 构造。
- **用户可见 API**：CLI 可见；Python 类型未从包根导出。
- **扩展点**：规则和 callback 可配置；Policy 本身不是 Protocol。
- **证据状态**：**已确认**，`permissions.py:12-92`、`tools.py:165-198`、`tests/test_permissions.py`。

### M07｜生命周期 Hook 与 Verification Gate

- **独立理由**：独立 Hook Protocol、Registry、Executor、callback/command adapter、配置 loader、priority/matcher/timeout/failure mode。
- **调用链位置**：prompt 入 history 前；工具权限/执行前；工具结果回填前；最终 done 前。
- **依赖**：M09（可选 Trace）。
- **被依赖**：M01、M05。
- **用户可见 API**：CLI 配置可见；Python API 可通过模块导入。
- **扩展点**：是，新增 Hook implementation 不需改 Executor。
- **证据状态**：**已确认**，`hooks.py:22-382`、`engine.py:140-152,291-312,389-500`、`tests/test_hooks.py`。

### M08｜上下文压缩与 Artifact offload

- **独立理由**：对 message history 做独立状态变换；大输出写入独立 artifact 生命周期；二者共同控制模型上下文规模。
- **调用链位置**：每次模型调用前；每个 tool result 写回前；Provider context error 时反应式重试。
- **依赖**：M02；文件系统。
- **被依赖**：M01、M05。
- **用户可见 API**：CLI 参数可见；未从包根导出。
- **扩展点**：当前构造器可注入具体对象，但没有 Compactor/Artifact Protocol。
- **证据状态**：**已确认**当前行为；“可无缝替换 LLM summarizer”仅为**文档声称/待确认**。

### M09｜JSONL Trace 与安全 Replay

- **独立理由**：有独立 run identity、序列状态、线程锁、持久化格式、查询 API、脱敏和 CLI 子命令。
- **调用链位置**：观察 CLI metadata、模型、Hook、权限、资源锁、工具、compaction、cost 和终态；不控制主流程（除 I/O 异常可能传播）。
- **依赖**：仅标准库。
- **被依赖**：M01、M04、M05、M07。
- **用户可见 API**：Trace CLI 可见；类未从包根导出。
- **扩展点**：可通过 `AgentLoop(tracer=...)` 开关，但类型标注固定为 `TraceWriter`。
- **证据状态**：**已确认**，`trace.py:16-202`、`tests/test_trace.py` 与 engine trace tests。

### M10｜Skill 渐进披露

- **独立理由**：定义 `<root>/<name>/SKILL.md` 的发现契约，并通过专用 Tool 把正文按需送入 history；有专项测试。
- **调用链位置**：CLI 启动时贡献 system prompt metadata；运行中走普通 Tool 链路。
- **依赖**：M04。
- **被依赖**：M05、包根 API。
- **用户可见 API**：`SkillCatalog` 从包根导出；CLI `--skills-dir` 可见。
- **扩展点**：目录内容扩展点，不是 Python Protocol。
- **证据状态**：**已确认**，`skills.py:13-101`、`tests/test_skills.py`。

### M11｜MCP 工具桥接与 OAuth

- **独立理由**：对接独立协议和 SDK，拥有 stdio/HTTP transport、session/exit stack 生命周期、schema/trust 转换与 OAuth token 状态。
- **调用链位置**：CLI run 前连接并注册；运行中作为普通 Tool；finally close。
- **依赖**：M04；外部 `mcp/httpx/jsonschema`。
- **被依赖**：M05；运行时通过 ToolRegistry 间接使用。
- **用户可见 API**：CLI `--mcp-config` 可见；未从包根导出。
- **扩展点**：配置驱动外部能力扩展。
- **证据状态**：**已确认**，`mcp.py:25-266`、`mcp_auth.py:19-215`、`tests/test_mcp.py`。
- **划分决定**：OAuth 只服务 HTTP MCP，没有独立消费者，因此与 MCP 合并分析。

### M12｜Docker-only sandbox shell

- **独立理由**：独立 Docker 外部系统、配置、可用性检查、一次性容器 lifecycle、专属 Tool adapter 和专项测试。
- **调用链位置**：CLI opt-in 注册；运行中经过统一 Tool 链后调用 Docker。
- **依赖**：M04；外部 Docker CLI/daemon/image。
- **被依赖**：M05、包根 API。
- **用户可见 API**：包根导出 `DockerSandbox/SandboxedShellTool`；CLI flags 可见。
- **扩展点**：当前只支持 Docker，Sandbox 不是 Protocol。
- **证据状态**：代码路径**已确认**；真实隔离强度依赖宿主 Docker，不能从单元测试推广到恶意多租户保证。

### X01｜异步、取消、超时与资源生命周期

- **独立理由**：async generator、provider streaming、parallel gather、condition lock、wait_for、cancel event、subprocess/HTTP/MCP cleanup 跨越多个职责边界。
- **调用链位置**：贯穿整个 run。
- **依赖/被依赖**：横切 M01/M03/M04/M07/M11/M12。
- **用户可见 API**：`AgentLoop.cancel()` 与流式 `AgentEvent` 可见。
- **扩展点**：所有异步 Provider/Tool/Hook 实现都必须遵守取消传播和资源清理约定。
- **证据状态**：**已确认**主要路径；公平性、跨进程协调等能力明确不存在。

### X02｜信任边界与 secret hygiene

- **独立理由**：没有单一 Security 模块；安全行为由 workspace containment、permission、Hook trust、trace redaction、MCP annotation trust、OAuth storage 和 Docker isolation 叠加形成。
- **调用链位置**：输入、工具执行、外部通信、日志和持久化全程。
- **依赖/被依赖**：横切 M04/M06/M07/M09/M11/M12。
- **用户可见 API**：CLI policy、unsafe trace、MCP trust、sandbox flags 可见。
- **扩展点**：新增 Tool/Hook/MCP server 时必须重新判断 trust/effect。
- **证据状态**：各机制**已确认**；整体安全保证只能按威胁模型解释，不能声称生产多租户隔离。

## 3. 没有形成独立模块的候选概念

| 概念 | 处理决定 | 理由 |
| -- | -- | -- |
| Configuration | 暂不独立 | 参数解析与三个 JSON loader 分散在各 owner 模块，没有统一 schema、对象或生命周期；先分别写入 M05/M06/M07/M11。 |
| Prompt | 不独立 | 只有默认 system string、CLI 拼接和 Skill metadata 注入，没有模板系统或独立测试边界。 |
| Memory / Session | 不创建 | 代码只有 `AgentLoop.messages` 进程内历史，没有独立检索或会话持久化。 |
| Workflow | 不创建 | 没有 graph/node/DSL；有界循环就是执行模型。 |
| Plugin | 不创建 | README 明确非目标，代码也没有动态 plugin loader。 |
| Event bus | 不创建 | `AgentEvent` 是 async generator 输出类型，Trace/Hook 各有直接调用，没有发布订阅总线。 |
| Retry | 合入 M03/M08/X01 | Provider 退避与 reactive context retry 语义不同，独立成文会脱离各自错误模型。 |
| Registry | 不按名称单拆 | ToolRegistry 与 HookRegistry 分属不同生命周期和职责，分别放入 M04/M07。 |

## 4. 后续阶段建议

1. 阶段一创建 M01–M04 四个 P0 文件。
2. 阶段二按主链邻近度处理 M07、M06、M08、M11、M12、M10、M05。
3. 阶段三处理 M09、X01、X02；Trace 虽是 P1，但放在横切阶段更利于避免重复。
4. 若后续证明统一配置对象或 session 恢复契约存在，再按“发现新模块”流程更新计划、地图、图与 README。
