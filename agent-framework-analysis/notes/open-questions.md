# 开放问题与划分调整记录

> 这里记录阶段零证据不足、接口意图不明确或需在后续精读中复核的问题。没有用“项目没有功能”的占位模块替代这些问题。

## 1. 模块边界待确认

### Q1｜配置是否值得独立成文？

- **已确认**：CLI argparse、Permission JSON、Hook JSON、MCP JSON、`.env` 各自存在 loader。
- **已确认**：没有统一 `Config` 对象、schema、precedence engine 或配置生命周期。
- **当前决定**：不创建 Configuration 模块，分别归入 CLI/Permission/Hook/MCP。
- **待确认**：若后续版本引入统一配置或多个 loader 的共同验证机制，应新增横切配置模块。

### Q2｜工具 Registry 与资源 Scheduler 是否应该拆开？

- **已确认**：`ResourceAccess/ResourceLockManager` 在 `tools.py`，但 batch 并发与 acquire lifecycle 在 `engine.py::_execute_timed/_execute_all`。
- **当前决定**：合并为“M04 工具能力边界与 effect-aware 调度”，因为它们共同完成一个 tool-call stage，分拆会重复 effect、permission 与 hook 交互。
- **待确认**：如果阶段一文件过大，或发现 scheduler 有独立 API/替换需求，再拆为接口与调度两个文件。

### Q3｜MCP OAuth 是否独立模块？

- **已确认**：OAuth 代码约 200 行，有独立存储和 callback lifecycle；但唯一调用者是 HTTP MCP。
- **当前决定**：与 MCP bridge 合并，专设 OAuth/trust 章节。
- **待确认**：若 OAuth storage/provider 被其他 integration 复用，再独立。

### Q4｜CLI 是模块还是纯入口？

- **已确认**：CLI 不只解析参数，还承担唯一 composition root、外部连接/close 和 exit code 语义。
- **当前决定**：作为 P1 Infrastructure 独立成文，但不把它当核心运行抽象。

## 2. API 与运行语义待确认

### Q5｜`messages=` 是否是受支持的 session 恢复契约？

- **代码实现**：`AgentLoop.__init__(messages=...)` 接受预载 history 并刷新 system message；`Message.from_dict()` 支持反序列化。
- **已确认**：`tests/test_engine.py::test_preloaded_history_refreshes_runtime_context` 覆盖 system refresh。
- **待确认**：CLI 没有读取 session/history 的入口，也没有文档承诺持久化格式；因此当前只视为程序化预载能力，不创建 Session 模块。

### Q6｜同一 `AgentLoop` 是否支持并发调用 `run()`？

- **代码实现**：每次 run 会覆盖同一实例的 `cancel_event`、resource locks、repeat counters，并共享 `messages`/token counters。
- **推断**：顺序复用有测试意图，并发复用不安全且未声明。
- **待确认**：是否应显式防止 overlapping runs，或把 per-run state 抽成独立对象。

### Q7｜Compactor 真的是可替换策略吗？

- **文档声称**：可替换为 LLM summarizer 而无需改变 AgentLoop 接口；`README.md:195`、`TECHNICAL_DESIGN.md:38`。
- **代码实现**：构造参数类型是具体 `ContextCompactor`，调用固定 `.compact(messages, force=...)`。
- **推断**：duck typing 可以替换，但没有 Protocol、contract test 或 async summarizer 支持。
- **待确认**：后续分析应把“当前实现”和“预期扩展点”明确分开。

### Q8｜`AgentEvent.data` 与 Hook payload 是否有稳定 schema？

- **已确认**：两者都使用 `dict[str, Any]`，字段由分支内临时构造。
- **推断**：CLI、Trace 和自定义 Hook 实际依赖这些 key，存在隐式协议。
- **待确认**：项目是否打算版本化 payload，或接受它们是内部实现细节。

## 3. 正确性与工程风险待确认

### Q9｜Tracer I/O 失败应该终止 Agent 吗？

- **代码实现**：多数 `tracer.emit()` 没有 try/except；文件写失败会向上传播。
- **待确认**：这是审计 fail-closed 的有意选择，还是可观测性不应影响业务的缺口。测试未覆盖磁盘满/权限失败。

### Q10｜ResourceLockManager 的公平性是否足够？

- **代码实现**：Condition wait predicate + active set，没有显式 FIFO waiter queue；docstring 称 “Fair-enough”。
- **文档声称**：生产规模可加入 FIFO 防止写饥饿；`TECHNICAL_DESIGN.md:63`。
- **待确认**：Mini 小 batch 下风险可接受，但需要在 X01 中明确不保证严格公平。

### Q11｜Tool path-based 权限能覆盖自定义工具吗？

- **代码实现**：`extract_path()` 只识别 `path/file_path/root`；其他字段只能按 tool glob 规则授权。
- **推断**：自定义 Tool 若用 `destination` 等字段，path rule 不会生效。
- **待确认**：是否应允许 Tool 提供结构化 permission resources，而不是通用字段猜测。

### Q12｜Trace secret redaction 的边界如何表达？

- **已确认**：它覆盖常见敏感 key、Bearer 与 `sk-` pattern；README 正确标注 best effort。
- **已确认**：Trace 默认文件创建未显式设置 0600，与 OAuth storage 不同。
- **待确认**：后续安全分析需评估目录 umask、retention、artifact 中敏感输出和 `--unsafe-trace-secrets` 的整体风险。

### Q13｜Artifact 路径是否应视为 workspace 外泄通道？

- **代码实现**：CLI 默认 artifact root 位于 workspace `.mini-oh/artifacts`；自定义库用户可传任意 root。
- **代码实现**：inline observation 包含宿主绝对 artifact path。
- **待确认**：对远端 Provider 发送绝对路径是否泄露宿主目录结构；当前 tests 只验证完整输出保存。

### Q14｜真实外部集成测试基线

- **已确认**：本次全量 pytest 得到 82 passed。
- **待确认**：测试输出没有显示 skip，但真实付费 Provider 没有普通测试；Docker/MCP real transport 的覆盖依赖测试内条件与本机能力。阶段二需逐个核对 fixture/skip 条件，避免把协议模拟测试描述为生产联调。

## 4. 文档声称与代码实现的差异

1. **文档声称**“Provider failure、取消和 max steps 才终止 runtime”；但 prompt Hook block 也明确终止，Tracer/Hook config 等未捕获异常也可能终止。后续应把该不变量限定为“正常运行期的模型/工具错误分类”。
2. **文档声称**“MCP input/output schema 被校验”；代码中 input schema 实际由统一 `ToolRegistry.execute()` 校验，output schema 在 `McpTool.run()` 校验。结论成立，但职责位置需准确表述。
3. **文档声称**“TraceWriter observes ... final state”；max-steps 路径会 `finish(failed)` 后抛异常，成立；但 CLI 在 AgentLoop 构造前或 MCP connect 失败时未必 finish trace，需后续核对失败覆盖。

## 5. 阶段零划分调整记录

- 初扫时“并发调度”看似只是 ToolRegistry 内部细节；追踪 `_execute_timed/_execute_all` 与 tests 后，确认它跨 `engine.py/tools.py` 且影响主链，因此并入独立 P0 工具调度分析主题。
- 初扫时 OAuth 可按文件单拆；确认唯一消费者与生命周期后，合并到 MCP integration，减少重复。
- 初扫时 Trace 可视为 Infrastructure；确认它横跨 Engine/Tools/Hooks/CLI 且有独立 replay 入口后，改为 Cross-cutting P1。
- 新增 X01“异步、取消、超时与资源生命周期”和 X02“信任边界与 secret hygiene”，原因是它们均横跨至少六个职责模块，无法由单个文件名准确代表。
