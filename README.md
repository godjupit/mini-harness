# Mini OpenHarness

一个可以在面试中讲清楚、现场跑通的精简 coding-agent runtime。它保留 Agent Loop、Hooks、Skills、MCP、权限审批、effect-aware 工具调度、上下文压缩、流式 Provider、Trace/Replay 和 Docker 自测。

```text
Skills catalog ─────→ AgentLoop ───→ Streaming ModelProvider
                         ↑                    │
HookRegistry ─────────────┤ prompt / pre-tool / post-tool / stop
                         │              text / tool calls
                         │                    ↓
                  observations ← PermissionPolicy
                         ↑          allow / deny / ask
                         │                    ↓
                  ToolRegistry ← local / skill / MCP
                         │
                  artifacts + compaction

TraceWriter observes model, hooks, permission, tool, MCP, cost and final state.
```

## 30 秒运行

要求 Python 3.10+：

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY
.venv/bin/mini-oh --demo --workspace . "解释这个项目"
```

CLI 会自动读取当前目录的 `.env`，但不会覆盖 shell 中已经设置的环境变量。真实 `.env` 已被 Git 忽略。

`--demo` 不需要 API Key，但真实经过 Agent Loop、skill 渐进加载、文件工具调用、结果回填、Trace 和最终回答。

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
- skill 加载和 MCP server（start/end 均保留归因）；
- permission 决策与人工审批结果；
- hook start/end、阻断、失败、耗时和 payload 改写；
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
- 文件写入默认询问；
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

## 3. 可扩展 Hook 与 Verification Gate

Hook 是 Agent 生命周期上的受信任扩展点，和模型工具不同：模型不能选择跳过 Hook。Mini 暴露四个稳定事件：

| 事件 | 时机 | 典型用途 |
|---|---|---|
| `user_prompt_submit` | prompt 进入 history 前 | 输入规范化、任务策略拒绝 |
| `pre_tool_use` | 权限判断和真实工具执行前 | 参数改写、敏感操作拦截 |
| `post_tool_use` | 工具完成后、结果回填模型前 | 输出审计、脱敏、结果拒绝 |
| `stop` | Agent 准备返回成功前 | 强制测试、lint、安全扫描 |

命令 Hook 通过 JSON 配置启用，`matcher` 使用 glob，`priority` 越大越先运行，同优先级保持注册顺序：

```bash
mini-oh --hooks-config examples/hooks-verification.json \
  --yes --workspace . "实现功能并运行测试"
```

示例 `stop` Hook 会执行当前 Python 环境中的 `pytest -q`。退出码为 0 才允许 `done`；失败时 Agent 收到测试输出，继续修复并再次申请完成：

```json
{
  "hooks": {
    "stop": [
      {
        "name": "pytest-verification-gate",
        "type": "command",
        "command": ["{python}", "-m", "pytest", "-q"],
        "timeout_seconds": 120,
        "failure_mode": "block"
      }
    ]
  }
}
```

命令不经过 shell，工作目录固定为 workspace。Runtime 将 `{"event": ..., "payload": ...}` 写入 stdin；普通命令只需以退出码表达成功/失败。需要改写 payload 时设置 `expect_json: true`，stdout 返回：

```json
{
  "decision": "allow",
  "updated_payload": {"tool_input": {"path": "safe.txt"}},
  "output": "optional audit detail"
}
```

`decision` 也可为 `block` 并带 `reason`。Hook 异常、无效 JSON 或超时由 `failure_mode` 决定：`block` 是 fail-closed，`continue` 是 fail-open。默认不会继承 API key 等完整宿主环境；确实需要时可显式设置 `inherit_environment: true`，因此 Hook 配置和脚本必须按受信任代码管理。

Python 扩展只需实现 `Hook` Protocol；最常用方式是注册同步或异步 callback，不需要修改 Executor 的类型分支：

```python
from mini_openharness.hooks import CallbackHook, HookEvent, HookRegistry, HookResult

hooks = HookRegistry()
hooks.register(
    HookEvent.PRE_TOOL_USE,
    CallbackHook(
        "protect-secrets",
        lambda ctx: HookResult(blocked=True, reason="protected path"),
        matcher="write_file",
        priority=100,
    ),
)

loop = AgentLoop(..., hooks=hooks)
```

Permission 回答“这个 capability 是否允许执行”，Hook 回答“在这个业务生命周期点还要执行什么组织策略”；两者都不能互相替代。Hook 的每次开始、结束、耗时、失败和改写都会进入 Trace。

## 4. Context Compaction 与 Artifacts

每次模型调用前估算 context token。超过阈值后：

1. 把旧消息折叠成可读 summary；
2. 保留最近若干 atomic units；
3. assistant tool calls 与其所有 tool results 永远作为同一个 unit，避免产生孤儿消息；
4. 大型工具输出保存到 `.mini-oh/artifacts/<run-id>/`，history 只保留头尾和 artifact 路径。

```bash
mini-oh --context-threshold 12000 --keep-recent 6 --max-inline-output 8000 "长任务"
```

当前 summary 是确定性、低成本实现。生产版可以替换为 LLM summarizer，而无需改变 AgentLoop 接口。

若 Provider 明确返回 context-window 超限，Runtime 会忽略普通阈值强制压缩一次，并在同一个逻辑 model step 重试；无法压缩或第二次仍失败则终止。该恢复只接受 typed `ProviderContextWindowError`，不会把任意 HTTP 400 当成可重试错误。

## 5. Provider 可靠性

Provider boundary 同时实现 OpenAI Responses 与 Chat Completions-compatible 协议：

- SSE token streaming；
- Responses typed Items、`function_call_output.call_id`，以及 arguments delta/done 重组；
- 对符合官方严格子集的 function schema 启用 `strict: true`；不兼容 schema 省略该字段，由 Responses 自动规范化或回退，runtime 仍再次校验；
- Chat Completions message/tool-call 兼容路径；
- Chat `finish_reason=length` 和 Responses `response.incomplete` 不会被误报为成功；
- 429、5xx、timeout、network error 指数退避；
- 认证、限流、超时、网络、无效响应和取消的统一错误类型；
- `Ctrl-C`/cancel event；
- usage 跨 step 累计和费用估算。

为了避免内容重复，如果连接已经输出 token 后才失败，本轮不会自动重试。

## 6. Effect-aware 工具调度与熔断

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

## 7. Skills 与 MCP

- Skills：启动时只注入 name/description，模型调用 `load_skill` 后正文才进入 history。
- MCP：同时支持 stdio 与 Streamable HTTP。远程 input/output schema 被校验，structured content 被保留。HTTP OAuth 使用 SDK discovery、动态注册/Client Metadata URL、PKCE S256、state、RFC 8707 resource audience、refresh token 与 scope step-up；token 以 `0600` 原子文件保存。远端 `readOnlyHint` 默认不受信任。

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

首次收到 401 时会启动 loopback callback 并打开授权 URL。Authorization server metadata 未声明 PKCE `S256` 时拒绝继续。已有 bearer token 可通过 `headersEnv` 从环境变量注入，避免把 secret 写进配置。

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
| `hooks.py` | Hook Protocol、Registry、Executor 与命令配置 | 生命周期扩展、fail-open/closed、Verification Gate |
| `trace.py` | JSONL trace/replay | 可观测、审计、无副作用回放 |
| `compaction.py` | summary 与 artifacts | 协议不变量、context 成本 |
| `skills.py` | skill 渐进加载 | progressive disclosure |
| `mcp.py` / `mcp_auth.py` | stdio/HTTP MCP 与 OAuth | PKCE、audience、token storage |
| `sandbox.py` | Docker-only shell | OS 隔离、fail-closed、资源限制 |

## 安全边界与非目标

普通文件工具依赖 workspace containment；只有 `sandbox_shell` 运行在 OS/Docker 隔离中。Docker boundary 包含默认 seccomp、只读 rootfs、无网络、capability/资源限制，但仍不应把 Docker daemon socket 暴露给容器，也不能把它当作恶意多租户执行平台。HTTP MCP 会访问远端开放世界，必须结合权限规则、可信 server 清单和最小 OAuth scope。

Python callback 与命令 Hook 都是受信任扩展代码：前者与 Agent 同进程，后者可在 workspace 中启动进程；它们不是安全沙箱。命令默认使用最小环境且不经过 shell，但若 Hook 本身不可信，仍应放入 Docker 或更强的隔离执行器。

项目有意不实现 TUI、插件市场和多 Agent，以保持面试主线集中在 runtime 的可靠性与可验证性。

实现依据、设计取舍和验证方法统一记录在 [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md)。
