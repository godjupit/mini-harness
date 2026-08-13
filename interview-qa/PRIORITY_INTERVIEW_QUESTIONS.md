# Mini OpenHarness：最高优先级面试题清单

目标不是背源码，而是做到：能说清设计动机、核心流程、边界条件和自己的取舍。先掌握 P0；P1 用于技术深挖；P2 用于追问。

## P0：必须能独立回答

### 1. 用 60 秒介绍这个项目。

回答必须覆盖：这是一个精简的 coding-agent runtime；主循环是 `model -> tool calls -> tool results -> model`；支持权限、Hook、MCP、Docker sandbox、上下文压缩、trace 和可恢复 session；设计目标是保留生产 Agent 最关键的正确性与安全边界，同时降低教学/面试复杂度。

### 2. 一次用户请求从 CLI 到最终输出的完整链路是什么？

从 `cli.py` 解析参数、构造 Provider/Tools/Policy/Trace/Session 开始，进入 `AgentLoop.run()`；写入 user message，调用 Provider；将 assistant reply 写入历史；若有 tool call，按策略并发执行并写入 tool result；无 tool call 时通过 stop Hook 后发送 done event。

### 3. `AgentLoop` 为什么是状态机，而不只是一个 while 循环？

指出状态包括：对话消息、当前 run 的取消事件、并发槽、资源锁、文件快照、重复工具批次计数、token/cost 统计。状态机保证模型、工具、取消、错误和完成都可观测且有明确转换。

### 4. assistant tool call 和 tool result 为什么必须配对？

模型协议要求每个 tool call 都有对应 result。中断发生在工具执行前/中时会留下悬空调用；恢复前必须过滤所有未解析调用，否则后续 API 请求可能被拒绝或模型上下文出现不一致。

### 5. session 如何做到中断恢复？append-only JSONL 的优缺点是什么？

每条消息立即追加到 JSONL，进程被杀时只可能留下最后一条半行，读取时可忽略。恢复时读取历史、判断中断状态、过滤悬空调用、再继续模型循环。优点是实现简单、审计友好、崩溃容忍；代价是日志不可原地修正，需要读取时做重放/修正，并要考虑日志增长。

### 6. 你最近修复了哪些 session 相关问题？

准备按“现象—根因—修复—测试”回答：

- 多次恢复时，旧的悬空 tool call 可能不在末尾，原逻辑只过滤最后一条；改为过滤全部未解析调用。
- stop Hook 拒绝完成后注入的 user message 没写入 session；改为统一调用 `_persist`。
- `_ACTIVE_SESSION` 在函数内赋值却未声明 `global`，没有真正清理；补上 `global` 并加回归断言。
- 用户传入的 session ID 能拼入路径；限制为字母、数字、`-`、`_`。

### 7. Permission 和 Hook 的区别是什么？为什么两者都要有？

Permission 决定某个 capability 能不能做，例如写文件、调用未知 MCP；Hook 是不可被模型跳过的生命周期组织策略，例如改写 prompt、拦截工具、校验完成。权限是能力边界，Hook 是业务/治理边界，不能互相替代。

### 8. stop Hook / verification gate 如何避免 Agent “假装完成”？

模型无 tool call 只表示它声称完成；stop Hook 可运行测试或验证命令。验证失败则阻止 done，将失败原因作为新的 user message 返给 Agent，令其继续修复。需要说明 Hook 是受信任代码，不能当作沙箱。

### 9. 工具为什么要做 effect-aware 调度，而不是直接 `asyncio.gather`？

并行可降低延迟，但两个写文件工具可能发生竞态。工具声明读/写 effect 和资源 key；相同文件或目录树冲突时加锁，不同文件可并发；未知资源按全局写锁 fail-closed。

### 10. Docker sandbox 的安全边界是什么？它不解决什么？

Docker-only shell 没有 host fallback，并采用只读 rootfs、无网络、资源限制、capability 降权等。它降低执行 workspace 命令的风险，但不是恶意多租户安全边界；绝不能暴露 Docker socket，也不能把不可信 Hook 当作已经沙箱化。

## P1：技术深挖高频题

### 11. Provider 层为什么要统一为 `ModelReply`？

将 Responses API、Chat Completions、流式事件、重试和错误差异封装在 Provider 中；AgentLoop 只处理统一的文本、tool calls 与 token 统计，减少核心状态机对供应商协议的耦合。

### 12. 流式输出和取消如何协作？

Provider 产生 text delta，Engine 转成 `assistant_delta` event；run state 保存 cancel event，取消时 Provider/工具应尽快停止并发出 cancelled event；`finally` 必须释放活动状态和外部连接。

### 13. 上下文过长时如何处理？为什么不只截断旧消息？

超过阈值或 Provider 返回 context-window error 时，按完整交互单元压缩较早历史，并保留最近单元。不能随意截断，否则会破坏 tool call/result 配对。对 context error 只强制重试一次，避免无限重试。

### 14. Artifact offload 解决什么问题？

超长工具输出会放入 artifact，并在消息中保留可追踪的摘要/路径，降低上下文成本；同时要保留原始输出可审计，避免模型上下文膨胀。

### 15. 重复工具批次熔断如何设计？

对同一批 tool calls 计数，超过阈值时不继续执行，而是把 loop-guard 失败结果回填给模型。这样既让模型知道发生了什么，也避免无限副作用、成本和资源消耗。

### 16. Trace 与 session 的职责差异是什么？

Session 是可重放的对话事实，用于恢复；Trace 是运行级审计，记录模型请求、工具、权限、Hook、成本和最终状态。Trace replay 只能渲染记录，绝不能再次调用模型或执行工具副作用。

### 17. Trace 为什么需要脱敏和严格写入模式？

trace 可能含 API key、工具输出和用户数据。默认脱敏降低泄露风险；strict mode 在无法安全写 trace 时失败退出，适用于审计必需的场景。需权衡可用性与合规性。

### 18. MCP 工具为什么默认更谨慎？

远端工具副作用与实现不可完全掌控，`readOnlyHint` 不能被盲信。应做 schema 校验、超时、权限审批、server 归因与最小 OAuth scope；未知或解析失败资源采用 fail-closed。

### 19. HTTP MCP OAuth 中 PKCE、state、resource audience 分别做什么？

PKCE 降低授权码被截获后的风险；state 防止登录流程关联/CSRF 问题；resource audience 限制 token 面向正确资源服务器。回答时强调这是远端能力的信任边界，不是“接上 OAuth 就安全”。

### 20. 文件编辑如何避免覆盖用户刚刚修改的内容？

在读取时记录文件快照或版本信息，写入/编辑时比对预期内容或快照；不匹配时返回冲突，而不是静默覆盖。写入采用原子替换，并做 workspace containment 校验。

## P2：追问与开放题

### 21. 如果把它做成生产系统，你会优先补什么？

可答：持久化元数据/索引、结构化 session 修正记录、文件锁跨进程化、可插拔 LLM summary、队列和限流、指标/告警、租户隔离、策略管理 UI、端到端故障注入测试。

### 22. append-only session 如何处理删除、隐私与长期存储？

增加加密、保留期、按用户/会话删除索引、可验证 tombstone 或重写/压缩机制；明确备份和审计策略。append-only 不等于永远不可删除。

### 23. 如何测试这类 Agent 系统？

分层测试：Provider/工具单测，脚本化 Provider 验证状态机，临时目录验证文件边界，Docker 集成测隔离，故障注入测取消/半行/超时/恢复，端到端测 CLI；避免只依赖真实模型的非确定性测试。

### 24. 如何定义并衡量 Agent 的可靠性？

成功率之外，还要有：工具失败率、恢复成功率、重复调用触发率、超时率、误拒绝/误允许率、token/cost、P95 耗时、验证门禁通过率，以及安全事件数。

### 25. 这个项目最重要的设计取舍是什么？

用较少的代码保留“正确的 runtime 边界”：统一事件和消息模型、明确安全策略、可测试的确定性实现。代价是功能不如完整产品丰富，例如 summary 质量、持久化索引、多 Agent 编排和隔离强度仍可继续增强。

## 面试前最后检查

1. 不看源码，画出 CLI、AgentLoop、Provider、Tools、Policy/Hook、Session/Trace 的关系图。
2. 能现场解释一次带 tool call 的请求，以及一次中断恢复。
3. 每个 P0 题练到 1 到 2 分钟；不要背段落，用自己的话讲。
4. 至少准备两段具体经历：一次 session bug 修复，一次安全/并发设计取舍。
5. 被问到不了解的细节时，先说模块职责和推理路径，再明确表示会回到代码验证；不要编造。
