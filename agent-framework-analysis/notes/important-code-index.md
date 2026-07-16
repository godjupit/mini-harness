# 重要代码索引

> 目标：给后续精读提供最短跳转路径。范围引用基于当前 0.6.0 源码。

## 1. 入口与装配

| 位置 | 重要性 | 证明什么 |
| -- | -- | -- |
| `pyproject.toml:23-24` | P0 | `mini-oh` console script 的真实入口 |
| `src/mini_openharness/__init__.py:3-24` | P0 | 包根公开 API 与当前版本边界 |
| `src/mini_openharness/cli.py:35-83` | P1 | agent run 的所有用户参数和默认值 |
| `src/mini_openharness/cli.py:95-214` | P0 | 完整 composition root、资源连接/关闭和 exit 状态 |
| `src/mini_openharness/cli.py:253-279` | P2 | trace list/show/replay 独立入口 |
| `src/mini_openharness/cli.py:323-337` | P0 | `.env`、子命令分发和 event loop 边界 |

## 2. 主状态机

| 位置 | 重要性 | 证明什么 |
| -- | -- | -- |
| `src/mini_openharness/engine.py:57-121` | P0 | AgentLoop 依赖、持有状态和构造不变量 |
| `src/mini_openharness/engine.py:133-170` | P0 | 每轮重置、prompt Hook、user Message、step 开始 |
| `src/mini_openharness/engine.py:172-282` | P0 | Provider stream/complete、错误恢复、reply 入 history |
| `src/mini_openharness/engine.py:284-316` | P0 | stop Verification Gate 与 done 终态 |
| `src/mini_openharness/engine.py:318-379` | P0 | Tool batch、loop guard、结果 offload/回填、max steps |
| `src/mini_openharness/engine.py:381-500` | P0 | pre Hook → resource lock → Registry → post Hook 的精确顺序 |
| `src/mini_openharness/engine.py:502-521` | P0 | 并发 gather、顺序保持和整批取消 |
| `src/mini_openharness/engine.py:523-574` | P1 | tool batch signature、compaction、cancel、tool Message |

## 3. 内部协议与 Provider

| 位置 | 重要性 | 证明什么 |
| -- | -- | -- |
| `src/mini_openharness/models.py:9-47` | P0 | provider-neutral role、ToolCall、Message、ModelReply |
| `src/mini_openharness/provider.py:15-91` | P0 | typed error family、stream events、两个 Provider Protocol |
| `src/mini_openharness/provider.py:94-162` | P0 | 重试规则、stream-to-complete wrapper、HTTP client lifecycle |
| `src/mini_openharness/provider.py:164-256` | P0 | Chat SSE 解析、finish reason、tool call 拼装与 payload |
| `src/mini_openharness/provider.py:262-401` | P0 | Responses typed event 解析、strict schema 与完成条件 |
| `src/mini_openharness/provider.py:404-475` | P1 | retry/error 分类、context detection、strict eligibility |
| `src/mini_openharness/provider.py:478-526` | P0 | 内部 Message 到 Chat/Responses wire format 的转换 |
| `src/mini_openharness/provider.py:529-552` | P1 | 覆盖真实 loop 的确定性 DemoProvider |

## 4. Tool capability、effect 与权限

| 位置 | 重要性 | 证明什么 |
| -- | -- | -- |
| `src/mini_openharness/tools.py:21-75` | P0 | hierarchical ResourceAccess 冲突规则与 async lock state |
| `src/mini_openharness/tools.py:78-103` | P0 | ToolContext、ToolResult 与 Tool Protocol |
| `src/mini_openharness/tools.py:105-157` | P0 | Registry、schema 暴露、source 与 fail-closed resource resolution |
| `src/mini_openharness/tools.py:158-215` | P0 | schema → permission/approval → timeout → error observation |
| `src/mini_openharness/tools.py:218-318` | P1 | workspace-safe 默认文件 tools 与注册集合 |
| `src/mini_openharness/tools.py:321-332` | P1 | 文件工具对 `.env` 与 OAuth token 的 secret deny |
| `src/mini_openharness/permissions.py:12-84` | P1 | ordered glob rule、read-only default、mutation/MCP default |
| `src/mini_openharness/permissions.py:87-92` | P2 | path 只从三个约定字段提取的现实边界 |

## 5. Hook、Context 与 Trace

| 位置 | 重要性 | 证明什么 |
| -- | -- | -- |
| `src/mini_openharness/hooks.py:22-93` | P1 | 四个稳定 lifecycle event 与 Hook Protocol |
| `src/mini_openharness/hooks.py:96-190` | P1 | callback/command adapters、stdin/stdout 协议、无 shell 执行 |
| `src/mini_openharness/hooks.py:193-293` | P1 | priority registry、sequential payload chaining、failure mode |
| `src/mini_openharness/hooks.py:296-346` | P1 | JSON command hook 配置 loader |
| `src/mini_openharness/hooks.py:360-382` | P2 | matcher subject 和最小子进程环境 |
| `src/mini_openharness/compaction.py:15-58` | P1 | token 估算、threshold/force compaction 状态转换 |
| `src/mini_openharness/compaction.py:61-85` | P1 | 大输出 artifact 原子 offload 与 inline head/tail |
| `src/mini_openharness/compaction.py:88-120` | P0 | tool call/result atomic unit 与确定性 summary |
| `src/mini_openharness/trace.py:25-68` | P1 | concurrent-safe append-only writer 与 idempotent finish |
| `src/mini_openharness/trace.py:82-144` | P1 | trace store、run-id 防穿越与无副作用 replay |
| `src/mini_openharness/trace.py:152-202` | P1 | JSON safety 与 secret redaction 边界 |

## 6. 扩展与外部集成

| 位置 | 重要性 | 证明什么 |
| -- | -- | -- |
| `src/mini_openharness/skills.py:13-61` | P2 | Skill discovery、metadata prompt 与按名读取 |
| `src/mini_openharness/skills.py:64-101` | P2 | Skill 作为只读 Tool 进入统一执行链 |
| `src/mini_openharness/mcp.py:25-91` | P1 | MCP stdio/HTTP/OAuth 配置与相对 cwd/path 解析 |
| `src/mini_openharness/mcp.py:93-155` | P1 | transport/session lifecycle 和动态 Tool 注册 |
| `src/mini_openharness/mcp.py:158-221` | P1 | MCP Tool adapter、annotation trust 与 output schema |
| `src/mini_openharness/mcp.py:229-266` | P1 | OAuth config 和远端 URL policy |
| `src/mini_openharness/mcp_auth.py:30-102` | P1 | 0600、non-symlink、atomic OAuth storage |
| `src/mini_openharness/mcp_auth.py:105-182` | P1 | loopback callback server lifecycle |
| `src/mini_openharness/mcp_auth.py:185-215` | P1 | PKCE S256 enforcement 与 SDK provider construction |
| `src/mini_openharness/sandbox.py:35-150` | P1 | Docker availability、argv isolation、timeout/cancel cleanup |
| `src/mini_openharness/sandbox.py:170-212` | P1 | sandbox_shell Tool contract 与 workspace tree write effect |

## 7. 最有证明力的测试

| 测试 | 证明重点 |
| -- | -- |
| `tests/test_engine.py::test_model_tool_model_loop` | 最小 model→tool→model 闭环 |
| `tests/test_engine.py::test_parallel_tool_calls_preserve_result_order` | 并发完成不破坏回填顺序 |
| `tests/test_engine.py::test_non_conflicting_mutations_run_in_parallel` | effect-aware 而非 mutation 全串行 |
| `tests/test_engine.py::test_context_error_forces_one_compaction_and_retries_same_model_step` | typed reactive recovery |
| `tests/test_hooks.py::test_stop_hook_blocks_completion_then_agent_recovers_and_trace_proves_it` | Verification Gate 真正阻断 done |
| `tests/test_hooks.py::test_pre_and_post_tool_hooks_transform_the_real_execution` | Hook 改写进入真实 tool path |
| `tests/test_provider.py::test_responses_stream_uses_typed_items_call_id_and_usage` | Responses typed contract |
| `tests/test_provider.py::test_retryable_failures_retry_before_any_content` | retry 的重复内容安全边界 |
| `tests/test_tools.py::test_tree_read_lock_blocks_child_write_until_release` | hierarchical resource lock |
| `tests/test_permissions.py::test_explicit_deny_overrides_allow_write` | CLI allow-write 不覆盖显式 deny |
| `tests/test_compaction.py::test_compaction_keeps_recent_tool_call_and_all_results_together` | context 协议原子性 |
| `tests/test_mcp.py::test_mcp_adapter_uses_same_permission_and_registry_path` | MCP 不绕过统一 Tool boundary |
| `tests/test_mcp.py::test_oauth_refuses_authorization_server_without_pkce_s256` | OAuth fail-closed |
| `tests/test_trace.py::test_trace_jsonl_list_show_and_safe_replay` | Trace 查询与安全 replay |
| `tests/test_sandbox.py::test_docker_sandbox_argv_enforces_core_isolation` | Docker argv 中的核心隔离参数 |

## 8. 文档与示例证据

- `README.md:21-48`：最小 demo、真实 Provider 模式与 exit code（文档声称；CLI/Provider 可验证）。
- `README.md:113-180`：Hook 用户模型（文档声称；hooks/engine tests 可验证）。
- `README.md:182-229`：compaction 与 effect-aware 调度（文档声称；实现/测试可验证）。
- `README.md:231-275`：Skills、MCP、Docker 操作入口（文档声称；部分依赖外部环境）。
- `TECHNICAL_DESIGN.md:190-196`：五条设计不变量（文档声称；阶段零已逐项对照代码）。
