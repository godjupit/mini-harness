# Mini OpenHarness 0.6 技术设计

本文记录精简分支的依据、实现和取舍。目标不是复制完整 OpenHarness，而是保留最能体现 Agent Runtime 能力、面试时可以白板推导、并能用测试验证的机制。

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
| Provider boundary | `src/openharness/api/` 的统一 stream events | `provider.py` 的 Responses/Chat adapters | 都把供应商协议收敛为内部消息；Mini 用协议级单元测试覆盖流式事件和 Tool Call |
| 工具输入与权限 | Pydantic input model + `PermissionChecker` | JSON Schema + `PermissionPolicy` | 都在执行副作用前重新验证模型参数，并把 ask/deny 结果转成可恢复 observation |
| 并行工具 | `gather(return_exceptions=True)` 保证每个 call 有 result | `gather` + hierarchical read/write resource locks | Mini 保留配对不变量，并进一步只串行有资源冲突的调用 |
| Hook | `hooks/` 中 command/http/prompt/agent adapters | `Hook` Protocol + callback/command adapters | 生命周期和 matcher/priority/timeout 对齐；Mini Executor 依赖 Protocol，新增 adapter 不需要修改类型分支，且 `stop` 可真正阻止 done |
| Context | microcompact + LLM summary + prompt-too-long reactive retry | deterministic atomic-unit summary + artifacts + typed reactive retry | 工具消息原子性和一次性恢复语义已对齐；Mini 有意保留确定性 summary，避免压缩本身再次调用模型 |
| MCP | stdio/HTTP、schema 和 OAuth | stdio/Streamable HTTP、input/output schema、OAuth | Mini 保留 transport、trust annotation、PKCE/state/resource audience 等核心边界 |
| Sandbox | Docker backend 与 shell policy | Docker-only disposable shell | 都 fail-closed；Mini 有意不提供 host fallback，并将 secret path 做双层遮蔽 |
| 可观测与验证 | runtime events、carryover、项目测试 | JSONL Trace/Replay、pytest、Verification Gate | Mini 只保留一套测试入口；Replay 保证不重新执行副作用 |

本轮审计发现并修复了四个 Provider 契约偏差：

1. 旧实现对所有 Responses function 固定发送 `strict: false`，等于主动退出官方当前的 schema 规范化；现在兼容 schema 发送 `strict: true`，不兼容 schema 省略字段，让 Responses 自动规范化或回退。
2. 旧实现依赖 `response.output_item.done` 收口函数参数，却没有消费官方独立定义的 `response.function_call_arguments.done`；现在 final arguments event 是权威值。
3. Chat `finish_reason=length` 与 Responses `response.incomplete` 表示输出没有完整结束；现在统一抛出 `ProviderOutputTruncatedError`，不会进入 Agent 的 `done` 路径。
4. 对明确的 context-window HTTP 错误归一化为 `ProviderContextWindowError`；Agent 强制压缩一次，并在同一个逻辑 step 重试，避免普通 400 被误重试或 max steps 被恢复动作虚耗。

保留的差异不是遗漏：主项目面向完整产品，包含 TUI、插件、多 Agent 和多 Provider；Mini 面向面试与教学，把复杂度集中在 runtime correctness。主项目用 LLM 生成高质量 summary，Mini 保留确定性 summary，使离线 Demo、测试和错误恢复都不需要嵌套模型调用；未来可以通过 Compactor Protocol 注入 LLM summarizer，而不改变 AgentLoop。

### Run 生命周期隔离

`AgentLoop` 是顺序多轮 conversation owner，但不再把每轮状态散落在实例字段中。
`ConversationState` 持有 messages 和累计 usage；每次 `run()` 新建 `RunState`，独占
cancel event、`ResourceLockManager` 和重复 tool-batch 计数。`_execute_timed`、
`_execute_all` 与 `_record_tool_batch` 显式接收该 state，因此这些 helper 不会读取“最近一次
run”覆盖的共享字段。

同一实例重叠运行没有可定义的 history 合并语义，Runtime 选择立即抛出
`RunAlreadyActiveError`，而不是静默交错消息。active guard 在 async generator 的 `finally`
中释放，覆盖成功、provider/tool 异常、max steps 和调用者 `aclose()`；不同 AgentLoop 实例仍可
并发。`cancel()` 只设置当前 RunState 的 event，没有 active run 时不预取消下一轮。

### Tool Descriptor 与结构化失败

Tool 的 Python 执行 Protocol 仍只要求 name、description、parameters 和 `run()`；安全与归因
元数据由不可变 `ToolDescriptor` 描述：source/source_id、effect、destructive、path_argument。
Registry 在注册时解析一次，权限、read-only 判断、资源 fallback 和 Trace attribution 共用同一
结果。内置文件工具、LoadSkill、MCP 和 Docker shell 都已显式声明；MCP 即使不使用
`mcp__server__tool` 名称也能保留 server 归因。

旧 Tool 在一个兼容周期内仍可注册。Registry 把旧 `read_only` 和名称惯例收敛到单一 legacy
adapter，标记 `descriptor_inferred=true`；未知 effect 按 mutation 处理，动态 resource resolver
抛错或返回无效结果时使用全局 tree write lock。核心权限与 AgentLoop 不再自行解析 MCP/Skill
名称或猜 path/file_path/root，新工具通过 `path_argument` 明确权限路径。

`ToolResult` 保留 output/is_error/metadata 三参数兼容，并新增可选 `ToolFailure`。Registry 的
lookup、schema validate、authorize、execute timeout/exception 和 postprocess 都返回稳定的
code/stage/retryable；旧工具返回 `is_error=True` 时归一化为 `tool_reported_error`。AgentEvent
和 Trace 保存 failure 字典，模型 observation 仍是简洁自然语言，避免把内部错误类型耦合到
prompt。

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

文件使用规范化绝对路径作为 `fs:` key；目录读取和 shell 使用 tree lock，能与所有子路径冲突，MCP 使用 server key。锁规则为 read/read 兼容，read/write 和 write/write 冲突；两个不同文件的 mutation 可以并发。未知工具或 resource resolver 失败时申请全局 tree write lock，采用 fail-closed。

`asyncio.gather` 只负责保持 result 顺序；真正的并发安全由 `ResourceLockManager` 的 condition + active lock set 保证。多个资源先稳定排序，避免未来一个调用声明多资源时产生锁序死锁。

总量边界由每轮 `RunState` 的 semaphore 单独承担，默认最大并发 8。每个调用先获取 slot，
再运行 pre-hook、解析并等待 resource lock，退出时按 `finally` 释放；取消等待中的 batch 会取消
所有 task，不泄漏 slot。`tool_slot_wait/acquired/released` 与
`resource_wait/acquired/released` 分离记录，因此能区分总容量排队和资源冲突排队。值 1 提供
确定性串行模式，但不改变 observation 的模型 call 顺序。

Trace 为每次调用记录 `resource_wait`、`resource_acquired` 和 `resource_released`；后两者分别包含 `waited_ms` 与 `held_ms`。因此可以区分模型是否并行提交、锁是否造成等待，以及慢点发生在排队还是工具执行阶段。

复杂度：当前 active lock 冲突检测为 O(r×a)，适合 Mini 的小 batch。生产规模可换成 trie + keyed RW lock，并加入 FIFO waiter 防止写饥饿。

### 文件快照与原子 EditFile

每轮 RunState 拥有独立 `FileSnapshotStore`。`read_file` 成功读取 bytes 后记录 resolved path、
SHA-256、size 和 mtime；`write_file` 成功后刷新快照。`edit_file` 要求本轮快照或调用者显式
提供 expected_sha256，当前内容 hash 不同即返回 `file_changed`。

编辑只支持 exact old_text/new_text：零匹配返回 `match_not_found`，多匹配默认返回
`ambiguous_match`，显式 replace_all 才批量替换。它不做模糊匹配、quote normalization 或完整
unified-diff 解析，避免模型在不确定位置修改。

写入在目标同目录创建临时文件，写 bytes、flush/fsync、复制原 mode，并在 `os.replace` 前再次
读取目标 hash 缩小 TOCTOU 窗口；成功后 fsync 目录（平台不支持时 best effort）。任何 replace
错误都会清理 temp 并返回 `atomic_replace_failed`，目标原内容保持。该设计降低半写和旧内容
覆盖风险，但不声称提供数据库事务或恶意跨进程隔离。

## 4. Responses Provider

Responses 与 Chat Completions 的核心差异不是 URL，而是协议模型。Mini 将内部历史转换为 typed Items：assistant tool call 变成 `function_call`，tool result 变成以同一 `call_id` 关联的 `function_call_output`。流式解析监听 `response.output_text.delta`、`response.output_item.added/done`、`response.function_call_arguments.delta/done` 和 `response.completed`。

工具 schema 使用保守的 strict eligibility 检查：每层 object 都必须 `additionalProperties: false`，并将所有 properties 列入 required；array 递归检查 items。满足条件时显式发送 `strict: true`。含默认值或可选字段的 schema 不伪装成 strict-compatible，而是省略 strict，让 Responses 按官方行为尝试规范化并在必要时回退；无论 provider 是否 strict，`ToolRegistry` 仍用原始 JSON Schema 做 runtime 校验。Chat-compatible 路径不发送 strict，避免破坏 DeepSeek 等兼容端点。

流结束也属于协议契约。`response.function_call_arguments.done` 的 final arguments 覆盖 delta 累积值，但 `call_id` 必须继续来自 function-call item，不能误用 event 的 `item_id`。Chat `finish_reason=length` 和 Responses `response.incomplete` 都是非完整终态；即使已经收到部分文本也抛出 typed truncation error，防止 Agent 把部分答案当成功交付。

Provider 最终仍产出统一 `ModelReply`，因此 AgentLoop、compaction 和 Trace 不感知 API 差异。官方 OpenAI 默认走 `--api-mode responses`；第三方兼容端点可显式使用 `--api-mode chat`。

Responses endpoint 返回 HTTP 404 时，CLI 不自动重放到另一协议，避免产生隐式重复请求；它会提示 `--api-mode chat` 并返回退出码 1。正常完成、失败和取消分别返回 0、1、130。

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

## 8. Trace Sink、本地治理与脱敏

Trace 是 append-only 审计证据，同时也是高敏感数据。AgentLoop、HookExecutor 和 ToolContext
只依赖极小 `TraceSink` Protocol；默认实现是 `LocalJsonlTraceSink`，旧名称 `TraceWriter`
保留为兼容 alias，测试和嵌入场景可使用 `MemoryTraceSink`。本地实现在线程锁内分配 sequence
并写完整 JSONL 行，因此并发工具不会造成行交错，`finish()` 也保持幂等。

本地文件通过 `os.open` 以 `0600` 创建并在每次 append 时重新收紧 mode；序列化前递归处理：

- 整字段遮盖：`api_key`、`authorization`、`password`、`secret`、access/refresh token 等；
- 字符串 pattern 遮盖：Bearer credential 和常见 `sk-...` key；
- 不把 `input_tokens` 这类业务统计误判为 credential。

默认安全、显式降级：只有 CLI 参数 `--unsafe-trace-secrets` 才关闭脱敏。写入策略默认
best-effort，第一次 I/O 失败经 callback 或 RuntimeWarning 报告并禁用 sink；`--strict-trace`
改为抛出 typed `TraceWriteError`，适合 CI/compliance。`TraceStore.prune()` 支持按天数或最多保留
运行数清理完成态记录，CLI 默认 dry-run，必须加 `--apply` 才删除，并始终跳过 active run。

这些机制仍不可能识别所有业务秘密，也不提供 at-rest 加密；部署方仍需管理 Trace 目录访问、
磁盘加密和保留周期。

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
```

测试覆盖：Hook priority/matcher、payload 改写、fail-open/fail-closed、命令协议、完成验证恢复；Responses strict eligibility、typed Items、arguments delta/done、非完整终态与 SSE contract；同资源串行/异资源 mutation 并发、loop guard、Trace redaction、真实 stdio 与真实 Streamable HTTP MCP、OAuth token mode/loopback/PKCE 拒绝路径，以及真实 Docker 容器的 workspace 写入、只读 rootfs、无网络和退出清理。

独立 `Mini OpenHarness CI` 只在 Python 3.12 上执行 Ruff 和全量 pytest，不包含版本矩阵，也不会隐式调用收费的真实 Provider。需要联调时直接用 `mini-oh` 和本地 `.env` 手动 smoke test。

CI 与本地验证共用同一个 pytest 入口。Provider 单元测试覆盖 Responses strict/done 和 Chat-compatible 流式契约；真实 API 只在明确需要时手动执行，避免把 secret、外部网络和费用引入普通提交检查。

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
