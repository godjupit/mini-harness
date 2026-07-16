# Mini OpenHarness 架构代码阅读手册

> 分析状态：阶段零至阶段四全部完成  
> 代码基线：Mini OpenHarness 0.6.0，提交 `deeb105`  
> 验证基线：2026-07-16 执行 `.venv/bin/pytest -q`，82 项测试全部通过

## 项目概览

- **已确认｜一句话定位**：这是一个小型 coding-agent runtime，以 `AgentLoop` 串联模型、工具、权限、Hook、上下文治理与审计。
- **主要入口**：CLI `mini-oh → mini_openharness.cli:main`；库 API `AgentLoop.run(prompt)`。
- **核心调用链**：CLI 装配 → prompt Hook → Message history → Provider → ToolCall → pre Hook → effect lock → Schema/Permission → Tool → post Hook → observation → Provider → stop Gate → done。
- **核心模块数量**：14 个分析主题，其中 4 个 P0、9 个 P1、1 个 P2；按实际职责分成 8 篇 `modules/`、4 篇 `cross-cutting/`、2 篇 `integrations/`。
- **系统中心**：`AgentLoop` 是运行控制中心，`ToolRegistry + effect scheduler` 是 capability/副作用中心，`Message/ToolCall/ModelReply` 是稳定内部语言。
- **非目标**：没有 Workflow graph、Memory store、Session manager、Plugin loader、EventBus、Web API 或 multi-agent runtime，不为这些预设概念建文件。

## 总览与架构发现

1. [项目概览](00-project-overview.md)
2. [真实模块地图](01-module-map.md)
3. [核心执行流](02-core-execution-flow.md)
4. [模块计划与划分依据](module-plan.md)
5. [模块依赖图](diagrams/module-dependencies.mmd)
6. [核心执行时序图](diagrams/core-execution-flow.mmd)

## 推荐阅读顺序

```text
README --demo 最小示例
→ 核心执行流
→ AgentLoop 中央协调
→ 内部对话协议
→ Provider 与 Tool 两个边界
→ Hook / Permission
→ Context / Trace
→ Skill / MCP / Docker
→ 异步生命周期与信任边界
```

时间受限时直接使用 [30 分钟、2 小时、1 天阅读路线](notes/reading-roadmap.md)。代码跳转见 [重要代码索引](notes/important-code-index.md)，名词辨析见 [术语表](notes/glossary.md)。

## 模块进度表

| 模块 | 类型 | 优先级 | 文件 | 状态 | 一句话结论 |
| -- | -- | -- | -- | -- | -- |
| AgentLoop 运行编排 | Core | P0 | [01-agent-loop-runtime.md](modules/01-agent-loop-runtime.md) | 已验证 | 有界反馈状态机拥有 history、终止、熔断与 per-run 控制状态。 |
| 内部对话与工具调用协议 | Domain | P0 | [02-conversation-protocol.md](modules/02-conversation-protocol.md) | 已验证 | 三个 dataclass 隔离 Provider wire protocol，并维持 call/result 配对。 |
| 流式模型 Provider 边界 | Adapter | P0 | [03-streaming-model-providers.md](modules/03-streaming-model-providers.md) | 已验证 | Responses/Chat SSE 收敛为 typed events、ModelReply 和错误族。 |
| 工具能力边界与 effect-aware 调度 | Core | P0 | [04-tool-capability-and-effect-scheduling.md](modules/04-tool-capability-and-effect-scheduling.md) | 已验证 | Schema、权限、timeout 与层级资源锁共同控制模型副作用。 |
| CLI 组合根与 Trace 子命令 | Infrastructure | P1 | [05-cli-composition-root.md](modules/05-cli-composition-root.md) | 已验证 | 唯一完整装配点决定默认安全姿态、可达组件与退出码。 |
| 生命周期 Hook 与 Verification Gate | Extension | P1 | [06-lifecycle-hooks.md](modules/06-lifecycle-hooks.md) | 已验证 | 四个不可绕过生命周期点可改写、阻断，并在 done 前验证。 |
| 上下文压缩与 Artifact offload | Infrastructure | P1 | [07-context-compaction-and-artifacts.md](modules/07-context-compaction-and-artifacts.md) | 已验证 | 原子 tool turn、阈值/反应式压缩和大输出外置共同控制 context。 |
| Skill 渐进披露 | Extension | P2 | [08-skill-progressive-disclosure.md](modules/08-skill-progressive-disclosure.md) | 已验证 | Skill 是指令目录扩展，不是动态 Plugin 系统。 |
| 权限策略与人工审批 | Cross-cutting | P1 | [01-permission-and-approval.md](cross-cutting/01-permission-and-approval.md) | 已验证 | 第一匹配规则与 mutation 默认值在副作用前产生 allow/deny/ask。 |
| JSONL Trace 与安全 Replay | Cross-cutting | P1 | [02-trace-and-safe-replay.md](cross-cutting/02-trace-and-safe-replay.md) | 已验证 | Trace 是审计证据；Replay 只渲染，不重新执行副作用。 |
| 异步、取消、超时与资源生命周期 | Cross-cutting | P1 | [03-async-cancellation-and-lifecycle.md](cross-cutting/03-async-cancellation-and-lifecycle.md) | 已验证 | async generator、task、deadline 和 cleanup 跨六个模块共同成立。 |
| 信任边界与 secret hygiene | Cross-cutting | P1 | [04-trust-boundaries-and-secret-hygiene.md](cross-cutting/04-trust-boundaries-and-secret-hygiene.md) | 已验证 | 安全来自 workspace、权限、trust、凭据与 OS 隔离的分层组合。 |
| MCP 工具桥接与 OAuth | Adapter | P1 | [01-mcp-tool-bridge-and-oauth.md](integrations/01-mcp-tool-bridge-and-oauth.md) | 已验证 | stdio/HTTP 远端能力被适配回统一 Tool 链，OAuth 只服务 MCP。 |
| Docker-only sandbox shell | Adapter | P1 | [02-docker-sandbox-shell.md](integrations/02-docker-sandbox-shell.md) | 已验证 | 可选一次性无网络容器提供 shell，但不是恶意多租户保证。 |

## 核心结论

### 一次运行经历什么

`AgentLoop.run()` 先执行 prompt Hook并写入 user Message；每步压缩 history、调用 Provider并追加 assistant Message。有 ToolCall 时经过 Hook、资源锁、Schema、权限和 timeout，结果以同 ID tool Message 回填；无 ToolCall 时必须通过 stop Hook 才产生 done。

### 状态在哪里

- Conversation、token/cost：`AgentLoop` 持有并跨顺序 run 累积。
- Cancel、resource locks、repeat guard：每个 run 重置。
- Tool/MCP registrations：run 前构建的 Registry/Manager 状态。
- 持久状态：Trace JSONL、Artifact 文本、OAuth token/client info；没有通用 Session/Memory。

### 公开扩展点

- Provider：实现 `stream()` 或 `complete()`。
- Tool：实现 Tool Protocol并注册，可声明结构化 resources。
- Hook：实现 Hook Protocol并按 lifecycle event 注册。
- Skill：增加 `<name>/SKILL.md`。
- MCP：配置 stdio/HTTP server，动态注册远端 Tools。

### 隐藏但关键的机制

- pre Hook 改参数后，资源、Schema 和权限用新参数重新计算。
- 并行 ToolCall 的完成顺序不改变 history 回填顺序。
- Context compaction 不拆 assistant tool calls 与结果。
- Provider context overflow 在同一逻辑 step 最多强制恢复一次。
- MCP readOnlyHint 默认不可信。
- Replay 从不执行 Provider/Tool。

## 最值得学习与最大风险

最值得学习：错误作为 observation、effect-aware 调度、typed recovery、完成验证门、统一 Tool boundary 和协议保持式 compaction。

最大风险：`AgentLoop.run()` 职责密度、未类型化 event/Hook payload、同实例并发 run、Trace/Artifact 数据治理、名称/字段驱动的 Tool metadata、stdio MCP 完整环境和单实例资源锁。

详细的动手任务与理解检查题见 [阅读路线](notes/reading-roadmap.md)。尚未被代码证据完全回答的问题见 [开放问题](notes/open-questions.md)。

## 模块依赖概览

- 用户入口：CLI 与包根 Library API。
- 核心：AgentLoop、Tool capability/effect scheduler。
- 稳定领域：Message/ToolCall/ModelReply。
- 可替换适配器：Provider、Tool、Hook；MCP/Docker 是具体外部 adapter。
- 横切：Permission、Trace、async lifecycle、trust boundaries。
- **已确认**：包内静态 import 无循环；model→tool→model 是业务反馈环而非模块循环。

查看 [Mermaid 模块依赖图](diagrams/module-dependencies.mmd) 和 [文字解释](01-module-map.md)。

## 文档事实标记

- **已确认/代码实现**：可由当前实现或通过测试直接证明。
- **推断**：根据依赖、状态与调用关系得出。
- **文档声称**：README/设计文档描述，但实现证据不足或扩展契约未正式化。
- **待确认**：见开放问题，未以占位模块伪造结论。
