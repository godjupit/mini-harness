# Mini OpenHarness

一个可以在面试中讲清楚、现场跑通、用证据验证的 coding-agent runtime。它保留了完整 Harness 最有价值的设计：Agent Loop、Skills、MCP、Memory、权限审批、上下文压缩、流式 Provider、Trace/Replay 和自动 Eval。

```text
Skills catalog ─┐
Relevant memory ├──→ AgentLoop ───→ Streaming ModelProvider
Session history ┘        ↑                    │
                         │              text / tool calls
                         │                    ↓
                  observations ← PermissionPolicy
                         ↑          allow / deny / ask
                         │                    ↓
                  ToolRegistry ← local / skill / memory / MCP
                         │
                  artifacts + compaction

TraceWriter observes model, permission, tool, memory, MCP, cost and final state.
```

## 30 秒运行

要求 Python 3.10+：

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY
.venv/bin/mini-oh --demo --workspace . "解释这个项目"
.venv/bin/mini-oh eval
```

CLI 会自动读取当前目录的 `.env`，但不会覆盖 shell 中已经设置的环境变量。真实 `.env` 已被 Git 忽略。

`--demo` 不需要 API Key，但真实经过 Agent Loop、skill 渐进加载、文件工具调用、结果回填、Trace 和最终回答。`mini-oh eval` 还会启动一个真实 MCP stdio server。

连接任意 OpenAI-compatible endpoint：

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-4.1-mini
mini-oh --workspace /path/to/project "分析架构并提出三个改进点"
```

## 1. Trace、可观测性与安全 Replay

每次运行默认写入 `.mini-oh/traces/<run-id>.jsonl`，包括：

- model request/response 与流式 token delta；
- tool 参数、来源、耗时、输出与错误；
- skill 加载、memory 命中/写入、MCP server；
- permission 决策与人工审批结果；
- compaction 前后 token 估算；
- token usage、估算费用及最终成功/失败/取消原因。

```bash
mini-oh trace list
mini-oh trace show <run-id>
mini-oh trace replay <run-id>
```

`trace replay` 只按时间线渲染已记录事件，绝不重新请求模型或执行工具，因此不会重复写文件或调用远程 MCP 副作用。

可用 `--input-cost`、`--output-cost` 设置每百万 token 的美元价格；`--no-trace` 可关闭记录。

## 2. 权限策略与人工审批

默认行为：

- read-only 工具自动允许；
- 文件写入、memory 写入默认询问；
- MCP 工具因为远端副作用未知，默认询问；
- 非交互环境无法询问时安全拒绝。

交互批准、一次性全批准和规则配置：

```bash
# 交互终端会显示 y/N 审批
mini-oh --workspace . "创建 docs/design.md"

# 自动批准所有 ask 决策
mini-oh --yes --workspace . "创建 docs/design.md"

# default + tool/path glob 规则
mini-oh --permission-config examples/permissions.json --workspace . "更新文档"

# 兼容旧用法：默认允许 mutation，但显式 deny 规则仍优先
mini-oh --allow-write --permission-config examples/permissions.json "执行任务"
```

示例规则：

```json
{
  "default": "ask",
  "rules": [
    {"tool": "write_file", "path": "secrets/*", "action": "deny"},
    {"tool": "write_file", "path": "docs/*", "action": "allow"},
    {"tool": "mcp__*", "path": "*", "action": "ask"}
  ]
}
```

## 3. Context Compaction 与 Artifacts

每次模型调用前估算 context token。超过阈值后：

1. 把旧消息折叠成可读 summary；
2. 保留最近若干 atomic units；
3. assistant tool calls 与其所有 tool results 永远作为同一个 unit，避免产生孤儿消息；
4. 大型工具输出保存到 `.mini-oh/artifacts/<run-id>/`，history 只保留头尾和 artifact 路径。

```bash
mini-oh --context-threshold 12000 --keep-recent 6 --max-inline-output 8000 "长任务"
```

当前 summary 是确定性、低成本实现。生产版可以替换为 LLM summarizer，而无需改变 AgentLoop 接口。

## 4. Provider 可靠性

OpenAI-compatible Provider 支持：

- SSE token streaming；
- 分片 tool-call arguments 重组；
- 429、5xx、timeout、network error 指数退避；
- 认证、限流、超时、网络、无效响应和取消的统一错误类型；
- `Ctrl-C`/cancel event；
- usage 跨 step 累计和费用估算。

为了避免内容重复，如果连接已经输出 token 后才失败，本轮不会自动重试。

## 5. 自动 Eval

```bash
mini-oh eval
mini-oh eval --json
```

内置场景同时检查最终答案、工具 trace、步骤/token/耗时和副作用：

| 场景 | 验证内容 |
|---|---|
| `tool_recovery` | 未知工具变成 observation，模型继续恢复 |
| `skill_loading` | skill 正文按需加载 |
| `memory_recall` | 相关长期记忆注入新任务 |
| `mcp_tool_call` | 真实 stdio MCP tools/list + tools/call |
| `permission_block` | 被拒绝写入没有文件副作用 |
| `context_compaction` | 旧上下文被压缩 |
| `provider_retry_stream` | 429 后重试并继续 SSE 输出 |

## Skills、MCP、Memory

- Skills：启动时只注入 name/description，模型调用 `load_skill` 后正文才进入 history。
- MCP：远程 schema 被适配成普通 Tool，仍经过 JSON Schema、权限、Trace 和 AgentLoop。
- Memory：稳定事实存入 `.mini-oh/memory.json`；`remember` 写入，`search_memory` 查询，CLI 自动召回相关内容。
- Session：`--session` 保存逐字消息协议，用于继续同一次对话；Memory 用于跨新会话保存少量稳定事实。

真实 MCP 示例：

```bash
mini-oh --demo --yes --mcp-config examples/mcp.json
```

MCP 配置中的 `{python}` 会解析为运行 `mini-oh` 的 Python，避免写死虚拟环境路径。

## 核心代码导航

| 文件 | 职责 | 面试重点 |
|---|---|---|
| `engine.py` | agent loop、事件与编排 | 状态机、并发、终止/取消 |
| `provider.py` | streaming provider | SSE、重试、错误分类 |
| `tools.py` | schema 与统一执行 | capability boundary |
| `permissions.py` | allow/deny/ask 规则 | 最小权限、人工介入 |
| `trace.py` | JSONL trace/replay | 可观测、审计、无副作用回放 |
| `compaction.py` | summary 与 artifacts | 协议不变量、context 成本 |
| `evals.py` | 自动行为评测 | 可重复证据，而非只看最终文本 |
| `skills.py` | skill 渐进加载 | progressive disclosure |
| `mcp.py` | MCP 生命周期与 adapter | 开放协议、统一权限路径 |
| `memory.py` | 持久事实与检索 | session/memory 分层 |

## 安全边界与非目标

当前已提供 workspace path containment、写权限、MCP 默认审批、最大步数、取消和审计 Trace，但它不是操作系统 sandbox。若增加 shell 工具，应再加入容器/系统调用隔离、网络策略和资源限制。

项目有意不实现 TUI、插件市场和多 Agent，以保持面试主线集中在 runtime 的可靠性与可验证性。

更多讲解见 [INTERVIEW.md](INTERVIEW.md)。
