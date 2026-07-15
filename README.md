# Mini OpenHarness

一个可以在面试中讲清楚、现场跑通、用证据验证的 coding-agent runtime。它保留了完整 Harness 最有价值的设计：Agent Loop、Skills、MCP、Memory、权限审批、effect-aware 工具调度、上下文压缩、流式 Provider、Trace/Replay 和自动 Eval。

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

真实 Provider CI 会分别验证 OpenAI Responses、OpenAI Chat 和 DeepSeek Chat，并上传 model/protocol/tool/stream usage 契约结果；配置方法见 [PROVIDER_CONTRACTS.md](PROVIDER_CONTRACTS.md)。

官方 OpenAI 默认使用 Responses API；兼容端点可切回 Chat Completions：

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-4.1-mini
mini-oh --api-mode responses --workspace /path/to/project "分析架构"
mini-oh --api-mode chat --base-url https://compatible.example/v1 "分析架构"
```

若第三方端点没有实现 `POST /responses`，CLI 会在 HTTP 404 时提示切换
`--api-mode chat`，并以非零状态退出。`done` 返回 0，provider error、max steps
返回 1，取消返回 130，便于 CI 正确判断结果。

## 1. Trace、可观测性与安全 Replay

每次运行默认写入 `.mini-oh/traces/<run-id>.jsonl`，包括：

- model request/response 与流式 token delta；
- tool 参数、来源、耗时、输出与错误；
- resource wait/acquire/release，以及 `waited_ms`、`held_ms`；
- skill 加载、memory 命中/写入、MCP server（start/end 均保留归因）；
- permission 决策与人工审批结果；
- compaction 前后 token 估算；
- token usage、估算费用及最终成功/失败/取消原因。

Trace 默认递归遮盖 `api_key`、`Authorization`、password/token 等敏感字段，并识别常见 Bearer/OpenAI key 文本。只有明确使用 `--unsafe-trace-secrets` 才会关闭脱敏；这只是本地日志卫生措施，不能替代目录权限、加密和保留策略。

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

Provider boundary 同时实现 OpenAI Responses 与 Chat Completions-compatible 协议：

- SSE token streaming；
- Responses typed Items、`function_call_output.call_id` 与分片 arguments 重组；
- Chat Completions message/tool-call 兼容路径；
- 429、5xx、timeout、network error 指数退避；
- 认证、限流、超时、网络、无效响应和取消的统一错误类型；
- `Ctrl-C`/cancel event；
- usage 跨 step 累计和费用估算。

为了避免内容重复，如果连接已经输出 token 后才失败，本轮不会自动重试。

## 5. Effect-aware 工具调度与熔断

模型可在一轮返回多个 tool calls。Runtime 为调用解析层级 `ResourceAccess`，再通过 async read/write lock 调度：

- 同资源 read/read 可并发，read/write 与 write/write 串行；
- 不同文件的写操作可以并发；目录 tree lock 会与其所有子路径冲突；
- 未知或无法解析的 mutation 使用全局写锁，fail-closed；
- 无论实际完成顺序如何，observation 仍按模型 call 顺序回填；
- Trace 直接记录锁等待和持有时间，可证明冲突调用确实串行；
- 每次调用有统一超时（默认 30 秒），超时转换为模型可恢复的 error observation；
- 连续相同的 tool-call batch 默认最多真实执行 3 次，之后由 loop guard 熔断，但仍把错误回填给模型，让模型有机会换方案。

```bash
mini-oh --tool-timeout 20 --max-repeated-tool-batches 2 "检查并修改项目"
```

## 6. 自动 Eval

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
| `loop_guard` | 重复 tool batch 被熔断，模型收到 observation 后恢复 |

## Skills、MCP、Memory

- Skills：启动时只注入 name/description，模型调用 `load_skill` 后正文才进入 history。
- MCP：同时支持 stdio 与 Streamable HTTP。远程 input/output schema 被校验，structured content 被保留。HTTP OAuth 使用 SDK discovery、动态注册/Client Metadata URL、PKCE S256、state、RFC 8707 resource audience、refresh token 与 scope step-up；token 以 `0600` 原子文件保存。远端 `readOnlyHint` 默认不受信任。
- Memory：稳定事实存入 `.mini-oh/memory.json`；`remember` 写入，`search_memory` 查询，CLI 自动召回相关内容。
- Session：`--session` 保存逐字消息协议，用于继续同一次对话；Memory 用于跨新会话保存少量稳定事实。

真实 MCP 示例：

```bash
mini-oh --demo --yes --mcp-config examples/mcp.json
```

MCP 配置中的 `{python}` 会解析为运行 `mini-oh` 的 Python，避免写死虚拟环境路径。

Streamable HTTP + OAuth 示例：

```json
{
  "mcpServers": {
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "redirectUri": "http://127.0.0.1:8765/callback",
        "tokenFile": ".mini-oh/oauth/remote.json",
        "scopes": "tools:read tools:write"
      }
    }
  }
}
```

首次收到 401 时会启动 loopback callback 并打开授权 URL。Authorization server metadata 未声明 PKCE `S256` 时拒绝继续。CI 中已有 bearer token 可通过 `headersEnv` 从环境变量注入，避免把 secret 写进配置。

## Docker sandbox shell

Shell 默认不注册，只有显式启用且本地镜像已存在时才可用：

```bash
docker pull alpine:3.20
mini-oh --sandbox-shell --yes --workspace . \
  --sandbox-image alpine:3.20 "运行测试并修复失败"
```

每次调用创建 disposable container：只把 workspace 挂载为可写，root filesystem 只读，network 为 `none`，drop all Linux capabilities，启用 `no-new-privileges`，并限制 CPU、memory、PID 与 tmpfs。workspace 内的真实 `.env*`（保留 `.env.example`）会覆盖为 `/dev/null`，`.mini-oh/oauth` 会用不可读 tmpfs 遮蔽；普通 `read_file/list_files` 也执行同样的 secret deny。Docker 或镜像不可用时直接失败，绝不回退宿主机 shell。工具仍经过 mutation permission、resource lock、timeout、Trace 和 artifact 链路。

容器内 workspace 固定显示为 `/workspace`；后续传给 `read_file/write_file` 时必须转换为相对路径，例如把 `/workspace/report.txt` 写成 `report.txt`。

## 核心代码导航

| 文件 | 职责 | 面试重点 |
|---|---|---|
| `engine.py` | agent loop、事件与编排 | effect-aware 调度、熔断、终止/取消 |
| `provider.py` | Responses/Chat streaming provider | typed Items、SSE、重试、错误分类 |
| `tools.py` | schema、resource lock 与统一执行 | capability/effect boundary |
| `permissions.py` | allow/deny/ask 规则 | 最小权限、人工介入 |
| `trace.py` | JSONL trace/replay | 可观测、审计、无副作用回放 |
| `compaction.py` | summary 与 artifacts | 协议不变量、context 成本 |
| `evals.py` | 自动行为评测 | 可重复证据，而非只看最终文本 |
| `skills.py` | skill 渐进加载 | progressive disclosure |
| `mcp.py` / `mcp_auth.py` | stdio/HTTP MCP 与 OAuth | PKCE、audience、token storage |
| `memory.py` | 持久事实与检索 | session/memory 分层 |
| `sandbox.py` | Docker-only shell | OS 隔离、fail-closed、资源限制 |

## 安全边界与非目标

普通文件工具依赖 workspace containment；只有 `sandbox_shell` 运行在 OS/Docker 隔离中。Docker boundary 包含默认 seccomp、只读 rootfs、无网络、capability/资源限制，但仍不应把 Docker daemon socket 暴露给容器，也不能把它当作恶意多租户执行平台。HTTP MCP 会访问远端开放世界，必须结合权限规则、可信 server 清单和最小 OAuth scope。

项目有意不实现 TUI、插件市场和多 Agent，以保持面试主线集中在 runtime 的可靠性与可验证性。

实现依据和设计取舍见 [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md)，面试讲法见 [INTERVIEW.md](INTERVIEW.md)。
