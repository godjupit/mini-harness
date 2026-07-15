# Mini OpenHarness 0.6 技术设计

本文记录 2026-07-16 这一轮维护的依据、实现和取舍。目标不是复制完整 OpenHarness，而是保留最能体现 Agent Runtime 能力、面试时可以白板推导、并能自动验证的机制。

## 1. 规范与项目依据

本轮对照三组一手资料：

- OpenAI [Responses API 迁移指南](https://developers.openai.com/api/docs/guides/migrate-to-responses)与[流式响应指南](https://developers.openai.com/api/docs/guides/streaming-responses)：Responses 是新项目推荐的 agentic API primitive。Mini 已实现 Responses typed Items/SSE Provider，同时保留 Chat Completions-compatible Provider。
- OpenAI [Function calling strict mode](https://developers.openai.com/api/docs/guides/function-calling#strict-mode)与[`response.function_call_arguments.done` 事件](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.function_call_arguments.done)：兼容 schema 应启用 strict，最终 arguments 事件应作为流式参数权威值。Mini 在 runtime 侧仍用原始 JSON Schema 再校验一次，因为兼容 endpoint 不一定支持 strict mode。
- MCP [2025-11-25 Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) 与 [Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)：客户端应校验结果、设置超时、审计敏感调用；HTTP OAuth 必须使用 PKCE、state 和 RFC 8707 resource audience。

同时参考主项目的 `engine/query.py`、`hooks/`、`BaseTool.is_read_only()`、Docker backend、权限边界、Bash/grep 超时和并行 tool result 配对逻辑。Mini 只提炼 runtime 主线，没有复制 TUI、插件和多 Agent。

## 2. 主项目实现对照与取舍

这份表是代码级对照，不以“有同名文件”作为完成证据：

| 能力 | 主项目核心实现 | Mini 对应实现 | 对齐结论 |
|---|---|---|---|
| Agent Loop | `src/openharness/engine/query.py::run_query` | `engine.py::AgentLoop.run` | 都维持 model → tools → observations → model，并保证 tool call/result 配对；Mini 用较小的事件模型保留主状态机 |
| Provider boundary | `src/openharness/api/` 的统一 stream events | `provider.py` 的 Responses/Chat adapters | 都把供应商协议收敛为内部消息；Mini 额外用真实 Provider Contract Matrix 验证协议而不是只 mock HTTP |
| 工具输入与权限 | Pydantic input model + `PermissionChecker` | JSON Schema + `PermissionPolicy` | 都在执行副作用前重新验证模型参数，并把 ask/deny 结果转成可恢复 observation |
| 并行工具 | `gather(return_exceptions=True)` 保证每个 call 有 result | `gather` + hierarchical read/write resource locks | Mini 保留配对不变量，并进一步只串行有资源冲突的调用 |
| Hook | `hooks/` 中 command/http/prompt/agent adapters | `Hook` Protocol + callback/command adapters | 生命周期和 matcher/priority/timeout 对齐；Mini Executor 依赖 Protocol，新增 adapter 不需要修改类型分支，且 `stop` 可真正阻止 done |
| Context | microcompact + LLM summary + prompt-too-long reactive retry | deterministic atomic-unit summary + artifacts + typed reactive retry | 工具消息原子性和一次性恢复语义已对齐；Mini 有意保留确定性 summary，避免压缩本身再次调用模型 |
| MCP | stdio/HTTP、schema 和 OAuth | stdio/Streamable HTTP、input/output schema、OAuth | Mini 保留 transport、trust annotation、PKCE/state/resource audience 等核心边界 |
| Sandbox | Docker backend 与 shell policy | Docker-only disposable shell | 都 fail-closed；Mini 有意不提供 host fallback，并将 secret path 做双层遮蔽 |
| 可观测与验证 | runtime events、carryover、项目测试 | JSONL Trace/Replay、Eval、Provider Matrix、Verification Gate | Mini 强调可重复证据；Replay 保证不重新执行副作用 |

本轮审计发现并修复了三个 Provider 契约偏差：

1. 旧实现对所有 Responses function 固定发送 `strict: false`，等于主动退出官方当前的 schema 规范化；现在兼容 schema 发送 `strict: true`，不兼容 schema 省略字段，让 Responses 自动规范化或回退。
2. 旧实现依赖 `response.output_item.done` 收口函数参数，却没有消费官方独立定义的 `response.function_call_arguments.done`；现在 final arguments event 是权威值。
3. Chat `finish_reason=length` 与 Responses `response.incomplete` 表示输出没有完整结束；现在统一抛出 `ProviderOutputTruncatedError`，不会进入 Agent 的 `done` 路径。
4. 对明确的 context-window HTTP 错误归一化为 `ProviderContextWindowError`；Agent 强制压缩一次，并在同一个逻辑 step 重试，避免普通 400 被误重试或 max steps 被恢复动作虚耗。

保留的差异不是遗漏：主项目面向完整产品，包含 TUI、插件、多 Agent 和多 Provider；Mini 面向面试与教学，把复杂度集中在 runtime correctness。主项目用 LLM 生成高质量 summary，Mini 保留确定性 summary，使离线 Demo、测试和错误恢复都不需要嵌套模型调用；未来可以通过 Compactor Protocol 注入 LLM summarizer，而不改变 AgentLoop。

## 3. Resource-aware 工具调度

### 问题

模型返回多个 tool calls 不代表这些调用都适合并发。两个 `write_file` 可能覆盖同一文件；一个 read 与一个 write 并发时，read 可能看到旧内容或半完成状态。审批回答的是“能不能做”，并不回答“能不能并行做”。

### 状态流

```text
tool calls ─→ resolve ResourceAccess(key, read/write, exact/tree)
                     │
                     ├─ non-conflicting ─→ run concurrently ─┐
                     └─ conflicting ─────→ wait on RW lock ──┤
                                                              ↓
                                              gather keeps call order
```

文件使用规范化绝对路径作为 `fs:` key；目录读取和 shell 使用 tree lock，能与所有子路径冲突。Memory 使用其 JSON 文件 key，MCP 使用 server key。锁规则为 read/read 兼容，read/write 和 write/write 冲突；两个不同文件的 mutation 可以并发。未知工具或 resource resolver 失败时申请全局 tree write lock，采用 fail-closed。

`asyncio.gather` 只负责保持 result 顺序；真正的并发安全由 `ResourceLockManager` 的 condition + active lock set 保证。多个资源先稳定排序，避免未来一个调用声明多资源时产生锁序死锁。

Trace 为每次调用记录 `resource_wait`、`resource_acquired` 和 `resource_released`；后两者分别包含 `waited_ms` 与 `held_ms`。因此可以区分模型是否并行提交、锁是否造成等待，以及慢点发生在排队还是工具执行阶段。

复杂度：当前 active lock 冲突检测为 O(r×a)，适合 Mini 的小 batch。生产规模可换成 trie + keyed RW lock，并加入 FIFO waiter 防止写饥饿。

## 4. Responses Provider

Responses 与 Chat Completions 的核心差异不是 URL，而是协议模型。Mini 将内部历史转换为 typed Items：assistant tool call 变成 `function_call`，tool result 变成以同一 `call_id` 关联的 `function_call_output`。流式解析监听 `response.output_text.delta`、`response.output_item.added/done`、`response.function_call_arguments.delta/done` 和 `response.completed`。

工具 schema 使用保守的 strict eligibility 检查：每层 object 都必须 `additionalProperties: false`，并将所有 properties 列入 required；array 递归检查 items。满足条件时显式发送 `strict: true`。含默认值或可选字段的 schema 不伪装成 strict-compatible，而是省略 strict，让 Responses 按官方行为尝试规范化并在必要时回退；无论 provider 是否 strict，`ToolRegistry` 仍用原始 JSON Schema 做 runtime 校验。Chat-compatible 路径不发送 strict，避免破坏 DeepSeek 等兼容端点。

流结束也属于协议契约。`response.function_call_arguments.done` 的 final arguments 覆盖 delta 累积值，但 `call_id` 必须继续来自 function-call item，不能误用 event 的 `item_id`。Chat `finish_reason=length` 和 Responses `response.incomplete` 都是非完整终态；即使已经收到部分文本也抛出 typed truncation error，防止 Agent 把部分答案当成功交付。

Provider 最终仍产出统一 `ModelReply`，因此 AgentLoop、compaction、Session、Trace 和 Eval 不感知 API 差异。官方 OpenAI 默认走 `--api-mode responses`；第三方兼容端点可显式使用 `--api-mode chat`。

Responses endpoint 返回 HTTP 404 时，CLI 不自动重放到另一协议，避免产生隐式重复请求；它会提示 `--api-mode chat` 并返回退出码 1。正常完成、失败和取消分别返回 0、1、130。

CI Provider Contract Matrix 不直接探测 HTTP endpoint，而是通过真实两轮 `AgentLoop` 验证完整行为契约：SSE delta、tool-call arguments 重组、tool result 回填、多轮历史和 usage。每个 case 输出版本化 JSON，最终聚合成 Markdown；缺少 secret 明确标为 skipped，协议或行为断言失败则 job 失败。

## 5. Timeout 与重复调用熔断

每个工具通过 `asyncio.wait_for` 受统一 wall-clock timeout 约束，默认 30 秒。超时不会破坏消息协议，而是生成：

```text
ToolResult(is_error=True, metadata={"timed_out": true})
```

这样模型可以缩小查询、换工具或解释失败。取消会继续沿 task cancellation 传播，工具实现应在 `CancelledError` 时清理子进程、socket 和临时文件。

重复熔断对每轮 tool batch 做稳定 JSON signature（tool name + 排序后的 arguments），只统计连续相同 batch。前 3 次正常执行；第 4 次不再触发真实工具，而为每个 call 生成带 `loop_guard` 元数据的错误 observation。它与 `max_steps` 分工如下：

- loop guard 限制重复副作用，并允许模型自我修正；
- max steps 限制整个运行的模型调用成本，是最终硬停止条件。

只检测连续 batch 是有意取舍：文件被其他工具修改后再次读取可能合理，不应该被全局计数误伤。

### Reactive context recovery

Responses 默认 truncation disabled 时，超出 context window 会返回 HTTP 400；Chat-compatible endpoint 也常用 `context_length_exceeded` 等错误码。Provider 只对明确 marker 产生 typed `ProviderContextWindowError`。AgentLoop 捕获后调用 `compact(force=True)`，忽略日常 threshold 但仍保持 tool-call atomic units，然后在同一个 step 发起一次重试。

该路径有三个硬边界：每次用户 run 最多一次 reactive retry；没有可压缩旧单元时直接失败；输出截断使用独立的 `ProviderOutputTruncatedError`，不会错误触发输入压缩。Trace 的 `context_retry` 同时保存压缩前后 token 估算、trigger 和原始错误，能够证明恢复确实发生。

## 6. MCP trust boundary、Streamable HTTP 与 OAuth

MCP adapter 现在同时保存 `inputSchema` 和 `outputSchema`。若 server 声明 output schema，返回的 `structuredContent` 必须通过 JSON Schema 校验；失败作为 tool execution error 回填模型，成功内容同时保留在 `ToolResult.metadata.structured_content`，而不只压平成文本。

`readOnlyHint` 影响并发和默认权限，因此属于安全相关事实。配置默认：

```json
{
  "mcpServers": {
    "internal": {
      "command": "...",
      "trustToolAnnotations": false
    }
  }
}
```

只有对受控 server 显式设为 `true`，其 `readOnlyHint: true` 才会映射为本地 `read_only`。即使信任 annotation，调用仍经过 JSON Schema、PermissionPolicy、timeout 和 Trace；trust 不等于绕过审批。

HTTP transport 使用 MCP SDK 的 `streamable_http_client`，兼容 JSON response 与 SSE、session lifecycle 和协议版本。OAuth 链路由 SDK 完成 protected-resource/authorization-server discovery、动态 client registration 或 Client ID Metadata Document、scope step-up、refresh 与 RFC 8707 `resource` 参数；Mini 增加三层宿主责任：

1. Authorization server metadata 不声明 PKCE `S256` 时拒绝授权；
2. callback 只允许带显式端口的 HTTP loopback URI，并由 SDK校验随机 `state`；
3. token/client info 原子写入 `0600` 文件，不进入 Trace，静态 header 推荐从 `headersEnv` 注入。

这实现的是 public client Authorization Code + PKCE。Mini 不接受配置文件里的 client secret，也不把上游 token 透传给其他 server。

## 7. Docker-only sandbox shell

`sandbox_shell` 没有 host fallback。启用时先执行 `docker image inspect`；Docker 或镜像缺失直接终止启动。每个调用使用独立 `docker run --rm`，并施加：

- `--network none`；
- `--read-only` root filesystem，仅 `/workspace` bind mount 为可写；
- `--cap-drop ALL` 与 `no-new-privileges`；
- CPU、memory、PID 与 tmpfs 限制；
- host uid/gid，避免容器在 workspace 生成 root-owned 文件；
- `.env*` bind-over `/dev/null`、OAuth token 目录不可读 tmpfs，并在文件工具层重复拒绝；
- timeout/cancel 时按 container name 强制清理。

Shell 被声明为 workspace tree mutation，因此与任意 workspace 文件访问冲突，并始终经过 ask/allow/deny 权限。容器不挂载 Docker socket、宿主配置目录或宿主环境变量。

容器路径 `/workspace/foo` 只存在于 sandbox 视角；宿主侧文件工具必须使用 workspace-relative 的 `foo`。该映射写入 tool description，避免模型把容器绝对路径误传给 `read_file`。

## 8. Trace 脱敏

Trace 是 append-only 审计证据，同时也是高敏感数据。`TraceWriter` 在序列化后递归处理：

- 整字段遮盖：`api_key`、`authorization`、`password`、`secret`、access/refresh token 等；
- 字符串 pattern 遮盖：Bearer credential 和常见 `sk-...` key；
- 不把 `input_tokens` 这类业务统计误判为 credential。

默认安全、显式降级：只有 CLI 参数 `--unsafe-trace-secrets` 才关闭。该实现是 best effort，不可能识别所有业务秘密，因此 Trace 仍需最小文件权限、加密、TTL 和访问审计。

## 9. 可扩展 Hook 与完成验证

Hook 位于模型不可绕过的 runtime control plane。Mini 选择结构化 `Hook` Protocol，而不是在 Executor 中按 callback/command 类型写分支：任意扩展只要提供 `name`、`priority`、`matcher`、`timeout_seconds`、`failure_mode` 和异步 `run(context)`，就能被 Registry 调度。这使 HTTP、消息队列或策略引擎 Hook 可以独立增加，而不修改 AgentLoop。

生命周期顺序如下：

```text
user_prompt_submit
        ↓
model → pre_tool_use → resource lock → permission → tool → post_tool_use → model
  │
  └─ no tool calls → stop ─ allow → done
                         └─ block → verification feedback → model
```

Registry 按 priority 降序稳定执行。每个 Hook 接收前一个 Hook 合并后的 payload，所以输入改写是确定性的；阻断后立即短路，避免低优先级 Hook 继续产生副作用。同一 tool batch 仍可并发，因此 Hook 实现若共享状态必须自行同步。

Command Hook 使用 argv + `create_subprocess_exec`，不解释 shell 元字符，cwd 固定为 workspace。结构化请求写入 stdin；退出码表达操作成功，严格 JSON 模式还能返回 allow/block 和 payload update。Executor 统一施加 wall-clock timeout，取消时杀死并回收子进程。默认子进程只获得 PATH、locale、临时目录和 Python 环境等最小变量，不继承 API key；显式 `inherit_environment` 属于信任边界升级。

异常与超时不是隐式吞掉：`failure_mode=block` 时 fail-closed，`continue` 时 fail-open，二者都会记录 `hook_start/hook_end`、failed、reason、elapsed 和 update。显式 `HookResult(blocked=True)` 永远阻断，不受 operational failure 策略影响。

`stop` 是 Verification Gate。Agent 产生无 tool-call 的最终回答后，Runtime 先执行测试/lint/扫描命令；失败不会伪造 `done`，而是把可信失败原因追加为新 observation，让模型修复并再次验证。它仍受 `max_steps` 硬上限约束，避免验证失败造成无限循环。

Hook 与 Permission 分层：Permission 是 capability/effect 授权，Hook 是组织级生命周期策略。`pre_tool_use` 在 resource resolution 和 Permission 之前运行，因此改写后的真实参数仍会重新计算资源锁、校验 schema 并接受权限判断，不会借参数改写绕过安全链路。

## 10. 可验证证据

```bash
pytest -q
mini-oh eval
```

测试覆盖：Hook priority/matcher、payload 改写、fail-open/fail-closed、命令协议、完成验证恢复；Responses strict eligibility、typed Items、arguments delta/done、非完整终态与 SSE contract；同资源串行/异资源 mutation 并发、loop guard、Trace redaction、真实 stdio 与真实 Streamable HTTP MCP、OAuth token mode/loopback/PKCE 拒绝路径，以及真实 Docker 容器的 workspace 写入、只读 rootfs、无网络和退出清理。

独立 `Mini OpenHarness CI` 在 Python 3.10/3.12 上执行 Ruff、全量 pytest 和行为 Eval。真实 Provider 因为需要 secrets 和会产生费用，继续由单独的定时/手动 Provider Contract Matrix 承担，避免普通 PR CI 隐式调用外部模型。

### 本轮验证快照（2026-07-16）

- Ruff check 与本轮 Python 文件 format check 通过；
- 90 个 pytest 全部通过；
- 10 个行为 Eval 全部通过，其中 `reactive_compaction` 证明一次强制压缩在同一 model step 内恢复；
- 在临时克隆的 `pallets/itsdangerous` 陌生仓库上，DeepSeek Chat 真实两轮契约通过：实际执行 `write_file`/`read_file`，8 项 contract checks 全为 true，观察到 18 个 streaming text delta，usage 为 2154 input / 250 output tokens；
- 本地没有 OpenAI Responses key，因此没有把 mock 结果伪装成 live evidence；OpenAI Responses 的 strict/done 契约由官方 schema、单元测试和 CI secret matrix 三层覆盖，最终 live 结论以 CI artifact 为准。

关键不变量：

1. 每个模型 tool call 最终恰好对应一个同 ID tool result。
2. 未知或不可信 effect 按 mutation 处理。
3. 超时、权限拒绝、schema 错误和熔断都留在 agent loop 内，成为可恢复 observation。
4. Provider failure、取消和 max steps 才终止 runtime。
5. Trace replay 永不执行模型或工具。

## 11. 仍然明确的边界

1. Docker shell 是单机开发隔离，不是恶意多租户平台；Docker daemon 与镜像供应链仍是 trusted computing base。
2. OAuth token 静态文件没有操作系统 keychain 加密，但权限为 `0600` 且不会记录进 Trace。
3. HTTP MCP 当前只有全开或全关的网络能力；域名级 egress policy 应由宿主防火墙/代理实现。
4. Resource lock 在单 AgentLoop 进程内生效；跨进程并发写需要文件锁或外部 lock service。
5. Python Hook 与 Command Hook 都是受信任代码，不提供多租户隔离；不可信 Hook 应使用独立容器、最小挂载和受控网络。
6. Context summary 当前是确定性截断式摘要，不具备主项目 LLM summarizer 的语义保真度；这是可替换策略，不影响 atomic-unit 与 reactive retry 不变量。
