# 面试讲解手册

## 60 秒项目介绍

“Mini OpenHarness 是我从完整 coding-agent 系统中提炼出的可验证运行时。核心是受控状态机：模型根据历史产生 tool calls，权限策略做 allow/deny/ask 决策，runtime 并发执行工具并回填 observations，直到最终答案。Skills 使用渐进披露；本地与 MCP 工具走同一条 schema、权限和 Trace 链路；Session 与长期 Memory 分层。长上下文会协议安全地压缩，大输出落 artifact。Provider 支持 SSE、重试、取消和费用统计。最后不是靠截图证明可用，而是用 JSONL Trace、安全 Replay 和七个自动 Eval 场景验证。”

## 5 分钟白板顺序

1. 画 `prompt → model → permission → tools → observations → model` 闭环。
2. 标出 Tool Registry 是所有副作用的唯一 capability boundary。
3. 展示 Skills、Memory 和 MCP 如何复用同一 loop，而不是各自旁路。
4. 解释 `tool_call_id` 配对，以及 compaction 为什么按 atomic unit 工作。
5. 展示 Trace 覆盖 model/permission/tool/cost/final state，Replay 不执行副作用。
6. 运行 `mini-oh eval`，重点指出 MCP 是真实 stdio 调用，权限场景验证文件未生成。

## 推荐现场 Demo

```bash
mini-oh --demo --workspace . "介绍项目"
mini-oh trace list
mini-oh trace replay <run-id>
mini-oh eval
pytest -q
```

## 常见追问

### 为什么不用现成 Agent Framework？

目标是展示 runtime 机制，而不是快速拼业务流程。模型协议、循环、权限、压缩、追踪和评测都显式存在，因此故障可以定位到具体边界。

### Tool 错误为什么不直接终止？

文件不存在、参数错误和未知工具通常是可恢复 observation。把错误返回模型允许其换工具或参数；Provider invariant、取消和最大步数才属于 runtime 终止条件。

### 并发工具有哪些坑？

每个 call 无论成功失败都必须产生相同 `tool_call_id` 的 result，且结果顺序保持稳定。当前并发所有已批准调用；生产版还应分析多个写工具之间的资源冲突。

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

Trace 包含 prompt、tool 参数和输出，因此应视为敏感本地数据。当前支持 `--no-trace`；生产化会进一步加入字段级 redaction、加密、保留期限和访问控制。

### 当前安全性够生产吗？

还不够。已有 workspace containment、规则审批和审计，但没有 OS sandbox。加入 shell 前必须补进程、文件系统、网络和资源隔离。

## 可继续扩展但不建议抢主线

- OpenTelemetry exporter；
- LLM/embedding compaction summarizer；
- 加密 Trace 和 Memory；
- sandboxed shell tool；
- 基于录制 trace 的回归数据集。

TUI、插件市场和多 Agent 应排在这些可靠性能力之后。
