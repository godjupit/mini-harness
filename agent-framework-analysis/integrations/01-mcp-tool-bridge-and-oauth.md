# MCP 工具桥接与 OAuth

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Adapter  
> 主要代码：`src/mini_openharness/mcp.py`、`mcp_auth.py`

## 1. 模块职责与边界

**已确认**：McpManager 管理 stdio/Streamable HTTP MCP transport 和 ClientSession 生命周期，将远端 Tool schema/调用结果适配为本地 Tool Protocol；HTTP 可选使用 OAuth 2.1 helper；`mcp.py:37-221`。

OAuth 与 MCP 合并分析，因为没有其他消费者。该模块不绕过 Registry：远端 input schema、权限、资源锁、timeout、Trace 与本地 Tool 走同一路径。

## 2. 对外接口

- `McpServerConfig`：command 或 URL 二选一、env/cwd/headers/OAuth/trust annotations。
- `McpManager.from_file/connect_and_register/close`。
- `McpTool`：一个远端 tool 的本地 adapter。
- `McpOAuthConfig`、`FileOAuthStorage`、`LoopbackOAuthFlow`、`build_oauth_provider`。

用户入口是 CLI `--mcp-config`；这些类未从包根导出。

## 3. 配置与连接生命周期

`from_file()` 接受顶层 `mcpServers` 或直接 server map；每个 server 必须恰好配置 command/url。相对 cwd 按配置文件目录解析；header 可从环境变量读取；OAuth token path 也按配置目录解析；`mcp.py:45-91,229-254`。

连接时每个 server 创建独立 AsyncExitStack：HTTP client+streamable transport 或 stdio process+streams，再创建 ClientSession、initialize/list_tools、注册 adapters。任一步失败都会关闭当前 stack；成功 stacks 在 `close()` 逆序释放；`mcp.py:93-155`。

## 4. Tool 适配

远端名被 sanitize 成 `mcp__<server>__<tool>`。inputSchema 成为 Tool.parameters，由 Registry 执行前验证；outputSchema 在调用返回后验证 structuredContent；`mcp.py:158-221`。

readOnlyHint 默认不可信。只有 server 配置 `trustToolAnnotations=true` 且明确 hint true 才映射 read_only；否则按 mutation。资源 key 按 server 聚类，因此同一 server 的 mutation 互斥，不同 server 可并行。

## 5. OAuth 与凭据存储

- 远端 OAuth URL 必须 HTTPS，或 HTTP loopback 且无 fragment；`mcp.py:257-266`。
- redirect URI 必须 HTTP loopback 且显式 port；`mcp_auth.py:105-120`。
- callback server 只接收目标 path 的 GET code，SDK负责 state 校验；完成后关闭 server。
- `StrictOAuthClientProvider` 要求 metadata 宣告 PKCE S256；`mcp_auth.py:185-195`。
- SDK负责 discovery、registration、resource audience、refresh 等；Mini提供 storage/callback/PKCE guard。
- token/client info 用 async lock、non-symlink检查、mkstemp、fsync、replace 和 0600 保存；`mcp_auth.py:30-102`。

## 6. 输入、输出与副作用

- 输入：JSON config、env headers、远端 schemas、Tool arguments。
- 输出：注册的本地 tool names 与 ToolResult。
- 状态：active sessions/stacks；OAuth token/client info file。
- 副作用：启动子进程、HTTP、浏览器、loopback listener、凭据文件。
- 生命周期：CLI 在 run 前连接、finally close。

## 7. 错误与边界情况

- 多 server 依次连接；后一个失败时当前 stack 会关，但此前成功 stack 保留到 CLI finally，设计成立。
- stdio env 继承完整宿主环境再叠加配置，可信 MCP server 可见 API keys；这与 CommandHook 的最小环境不同。
- `readOnlyHint` 仍只是远端声明；即使配置 trust 也不是副作用证明。
- OAuth token 文件没有 keychain 加密；0600 只提供本机权限边界。
- callback prints URL/opens browser，headless 环境需 `openBrowser=false` 并人工处理。
- output 非文本 MCP content 被 `model_dump_json()` 压成文本；内部消息不保留多模态类型。

## 8. 扩展方式

新增 server 只需配置；新增 transport 需在 Manager connection branch 中实现并保持 ExitStack lifecycle。新增 auth 模式应避免把凭据写入 Trace/config，并明确 trust/HTTPS/redirect policy。

## 9. 测试依据

- `tests/test_mcp.py::test_mcp_config_resolves_relative_cwd`
- `test_mcp_adapter_uses_same_permission_and_registry_path`
- `test_mcp_output_schema_is_validated_and_structured_content_is_preserved`
- `test_mcp_read_only_annotation_requires_explicit_server_trust`
- `test_http_mcp_oauth_config_and_env_headers`
- `test_oauth_token_storage_is_atomic_and_owner_only`
- `test_oauth_token_storage_rejects_symlink`
- `test_oauth_refuses_authorization_server_without_pkce_s256`
- `test_real_streamable_http_mcp_transport`

## 10. 设计评价与阅读建议

- 值得学习：远端能力统一进入本地 capability boundary；annotations 默认不可信；ExitStack 管理多层异步资源。
- 潜在问题：stdio 完整环境、静态文件凭据、server 级粗资源锁、名称前缀协议。
- 改进方向：stdio env allowlist、keyring adapter、结构化 source/effect metadata、MCP content typed mapping、server allowlist/egress policy。
- 精读：`McpManager.from_file/connect_and_register/close`、`McpTool.run/resources`、`FileOAuthStorage._update_sync`、`LoopbackOAuthFlow`、`StrictOAuthClientProvider`。
