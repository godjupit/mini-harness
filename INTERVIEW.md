# 面试讲解手册

## 60 秒项目介绍

“Mini OpenHarness 是我从完整 coding-agent 系统中提炼出的可验证运行时。核心是受控状态机：OpenAI Responses 或 Chat Provider 产生 tool calls，权限策略做 allow/deny/ask，resource-aware 读写锁只串行冲突副作用，再按 call order 回填 observations。每个工具有 timeout 和重复调用熔断。stdio/HTTP MCP 走同一 schema、OAuth、权限和脱敏 Trace 链路；shell 只在无网络、只读 rootfs、资源受限的 disposable Docker 中执行，绝不回退宿主机。最后用安全 Replay、自动 Eval、真实 HTTP MCP 和真实 Docker 集成测试证明行为。”

## 5 分钟白板顺序

1. 画 `prompt → model → permission → tools → observations → model` 闭环。
2. 标出 Tool Registry 是所有副作用的唯一 capability boundary。
3. 展示 Skills、Memory 和 MCP 如何复用同一 loop，而不是各自旁路。
4. 解释 `tool_call_id` 配对，以及 compaction 为什么按 atomic unit 工作。
5. 展示 Trace 覆盖 model/permission/tool/cost/final state，Replay 不执行副作用。
6. 运行 `mini-oh eval` 和集成测试，指出 MCP stdio/HTTP 与 Docker 隔离都是真实进程，不是 mock。

## 推荐现场 Demo

```bash
mini-oh --demo --workspace . "介绍项目"
mini-oh trace list
mini-oh trace replay <run-id>
mini-oh eval
pytest -q tests/test_sandbox.py -s
pytest -q
```

## 常见追问

### 为什么不用现成 Agent Framework？

目标是展示 runtime 机制，而不是快速拼业务流程。模型协议、循环、权限、压缩、追踪和评测都显式存在，因此故障可以定位到具体边界。

### Tool 错误为什么不直接终止？

文件不存在、参数错误和未知工具通常是可恢复 observation。把错误返回模型允许其换工具或参数；Provider invariant、取消和最大步数才属于 runtime 终止条件。

### 并发工具有哪些坑？现在如何处理？

每个 call 无论成功失败都必须产生相同 `tool_call_id` 的 result，且结果顺序保持稳定。当前每次调用声明 exact/tree resource read/write lock：同资源冲突串行，不同文件 mutation 并发，未知 effect 获取全局写锁。`gather` 保持返回顺序，lock manager 决定实际执行并发度。

### Responses API 为什么不能只把 endpoint 改成 `/responses`？

它使用 typed Items，不是 Chat message 数组。assistant function call、tool output 分别是 `function_call` 和 `function_call_output`，靠 `call_id` 关联；流式事件也是 `response.output_text.delta` 等类型。Mini 在 Provider boundary 做双向映射，AgentLoop 保持协议中立。

### 为什么熔断后不直接终止 Agent？

重复调用通常是模型策略错误，不一定是 runtime 崩溃。熔断器阻止真实副作用并生成带 `loop_guard` 元数据的 error observation，模型还能改参数、换工具或给出解释；`max_steps` 仍是最终硬上限。

### 超时应该放 Tool 内还是 Runtime 内？

两层都可存在。Runtime 的统一超时保证任何第三方/MCP 工具都不会无限占用 loop；工具内部超时更了解子进程清理或网络语义。这里用 `asyncio.wait_for` 提供统一上限，具体工具仍应在取消时释放资源。

### Replay 为什么不重新执行工具？

真实重放可能再次写文件、付款或发送消息。这里的 replay 是审计时间线：只读取 JSONL 并渲染，明确保证零模型请求和零工具副作用。若要做确定性执行重放，应使用隔离 sandbox 和录制的 provider/tool stub。

### 权限规则优先级是什么？

按配置顺序匹配 `tool` 和 `path` glob；第一个命中规则生效。未命中时 read-only 自动允许，mutation 与 MCP 使用 default（默认 ask）。非交互环境没有审批者时 ask 会安全拒绝。显式 deny 不会被 `--allow-write` 覆盖。

### 如何保证压缩不破坏 Provider 消息协议？

Compactor 先把消息分成 atomic units。含 tool calls 的 assistant 消息和紧随其后的所有 tool results 是不可拆分单元：要么一起进入 summary，要么一起保留。这样不会出现孤立 tool result 或缺失结果的 tool call。

### 为什么失败后不总是自动重试？

在还没有输出 token 时，429/5xx/网络错误可以安全重试；已经输出部分内容后重试会产生重复文本，甚至重复 tool call，所以选择失败并让上层明确处理。

### Session 与 Memory 为什么分开？

Session 是逐字协议记录，用于继续当前对话；Memory 是经过选择的稳定事实，用于新会话召回。分层避免上下文无限增长，也方便修正或删除错误记忆。

### Trace 会不会泄露敏感信息？

Trace 包含 prompt、tool 参数和输出，因此始终应视为敏感本地数据。当前默认做字段级和常见 credential pattern 脱敏，也支持 `--no-trace`。生产化仍需加密、保留期限、访问控制和组织级 DLP；脱敏不是完整的数据安全边界。

### 为什么不直接相信 MCP 的 `readOnlyHint`？

MCP 规范把 tool annotations 定义为提示，并要求来自不可信 server 时不能据此做安全决策。Mini 默认把 MCP tool 当 mutation；只有配置 `trustToolAnnotations` 的 server 才能用 `readOnlyHint` 进入并行只读路径。权限规则仍独立执行。

### HTTP MCP OAuth 做了哪些安全约束？

使用 Authorization Code + PKCE，发现 protected resource 和 authorization server，token request 带 RFC 8707 resource audience，校验随机 state，并支持 refresh/scope step-up。Mini 额外拒绝未声明 PKCE S256 的 server，只监听 loopback callback，token 原子保存为 `0600`，也不允许 token passthrough。

### Docker shell 为什么算 fail-closed？

工具没有宿主机 subprocess 分支。Docker CLI、daemon或镜像不可用都会报错。容器只有 workspace bind mount，rootfs 只读、network none、capabilities 全移除，并有限制 CPU/memory/PID；timeout/cancel 会按随机 container name 强制删除。

### 当前安全性够生产吗？

用于本地 coding agent 已形成明确分层：普通文件工具有 workspace containment，shell 有 Docker 隔离，远端 MCP 有 OAuth 与审批。但还不是恶意多租户执行平台：Docker daemon/镜像供应链、跨进程锁、token keychain 加密和域名级 egress 仍需要部署层解决。

## 可继续扩展但不建议抢主线

- OpenTelemetry exporter；
- LLM/embedding compaction summarizer；
- 加密 Trace 和 Memory；
- OpenTelemetry trace context exporter；
- 基于录制 trace 的回归数据集。

TUI、插件市场和多 Agent 应排在这些可靠性能力之后。
