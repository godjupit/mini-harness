# CLI 组合根与 Trace 子命令

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Infrastructure  
> 主要代码：`pyproject.toml:23-24`；`src/mini_openharness/cli.py`

## 1. 模块职责与独立性

**已确认**：CLI 是主要用户入口和唯一完整 composition root：它解析配置、选择 Provider、创建所有控制组件、连接 MCP、消费 AgentEvent、映射退出码并关闭外部资源；`cli.py:35-214,323-337`。

它不实现 Agent 状态机或各组件算法。之所以独立成文，是因为它决定默认安全姿态与组件是否可达，而不仅是薄参数包装。

## 2. 对外接口

- `mini-oh [prompt] [options]`：运行 Agent。
- `mini-oh trace list/show/replay`：离线检查 Trace。
- `main(argv=None)`：console script target。
- `build_run_parser/build_trace_parser`：参数结构。

主要默认值包括 Responses API、12 steps、30 秒工具 timeout、3 次重复 batch、默认 Trace 开启、Docker shell 关闭、mutation ask；`cli.py:35-83`。

## 3. 装配顺序

```text
workspace + prompt
→ SkillCatalog + system prompt
→ Demo 或 OpenAI Provider
→ optional TraceWriter
→ default ToolRegistry
→ optional Docker/Skill tool
→ optional MCP connect/register
→ PermissionPolicy + approval callback + Hooks
→ ContextCompactor + ArtifactStore
→ AgentLoop.run
→ print events / exit code
→ finally close MCP and Provider
```

MCP 在 AgentLoop 构造前连接，因此远端 schema 会进入首个 model request。Provider 与 MCP 都在 `finally` 清理；`cli.py:150-211`。

## 4. 配置来源与优先级

- `.env`：只加载 cwd `.env`，`override=False`，因此 shell 已有变量优先；`cli.py:335-337`。
- argparse defaults：部分读取 `OPENAI_MODEL/API_MODE/BASE_URL/API_KEY`。
- CLI flag：覆盖 argparse default。
- JSON 文件：Permission、Hook、MCP 各由 owner loader 解析，没有统一 Config 对象。

**已确认**：这不是独立配置系统；配置 precedence 分散但清晰，暂不创建 Configuration 模块。

## 5. 输入、输出与退出语义

- 正常 `done`：0。
- Provider error、max steps：1。
- `cancelled`/KeyboardInterrupt：130。
- `assistant_delta` 直接无换行输出；完整 assistant、tool、compact、retry 等有各自渲染；`cli.py:282-320`。
- Responses 404 只提示显式切换 Chat，不自动重放；`cli.py:217-223`。

## 6. Trace 子命令

`trace list` 生成 summary；`show` 输出完整 JSON；`replay` 明确提示只渲染记录，并调用 `TraceStore.replay()`。它不进入 AgentLoop，也不实例化 Provider/Tool；`cli.py:253-279`。

## 7. 扩展方式

增加 CLI 可选组件通常需要同时修改 parser 与 `_run()` 装配。若只是库扩展 Provider/Tool/Hook，无需改 CLI；若要让终端用户选择，则应新增 flag/config 和资源 close 逻辑。

## 8. 错误与边界情况

- API key 缺失且非 demo 时 `SystemExit`。
- MCP connect、Hook/config parse、Docker availability 在 AgentLoop 前失败；已创建的 Trace 可能没有 `run_end`。
- `KeyboardInterrupt` 在 `asyncio.run()` 内外有两层处理；loop.cancel 后 event loop teardown 仍负责 task cancellation。
- 非交互 stdin 且没有 `--yes` 时 ask 没有 callback，最终安全拒绝。
- CLI 不支持从文件恢复 messages/session。

## 9. 测试依据

- `tests/test_cli.py::test_local_dotenv_is_loaded_without_overriding_shell`
- `test_cli_defaults_to_responses_and_sandbox_shell_is_opt_in`
- `test_cli_accepts_hook_configuration`
- `test_cli_provider_error_returns_nonzero_and_hints_on_responses_404`

## 10. 设计评价与阅读建议

- 值得学习：组合与算法分离；外部资源集中 close；退出状态适合 CI。
- 潜在问题：`_run()` 高扇出、配置验证分散、初始化失败 Trace 可能悬空。
- 改进方向：引入只属于应用层的 `RunConfig`/resource stack，而非框架级 DI container；集中初始化失败的 Trace finish。
- 精读：`build_run_parser`、`_run`、`_permission_policy`、`_approval_callback`、`_trace_command`、`_print_event`、`main`。
