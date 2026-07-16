# 信任边界与 secret hygiene

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Cross-cutting  
> 主要代码：`tools.py`、`permissions.py`、`hooks.py`、`trace.py`、`mcp.py`、`mcp_auth.py`、`sandbox.py`

## 1. 横切职责

**已确认**：项目没有单一 Security 模块。安全行为由 workspace containment、effect permission、trusted Hook、MCP annotation trust、OAuth storage、Trace redaction 和 Docker isolation 叠加形成。

因此任何“安全”结论必须指明威胁模型和层级，不能把某一层推广成生产多租户保证。

## 2. 信任边界地图

| 主体/数据 | 默认信任 | 控制 |
| -- | -- | -- |
| 模型输出 | 不可信 | Tool JSON Schema、Permission、Hook、workspace/resource boundary |
| 本地 Tool 实现 | 代码级可信 | Registry timeout/error wrapping；不能防恶意实现 |
| Python/Command Hook | 可信组织代码 | timeout、最小 command env；不是 sandbox |
| MCP server/tool annotations | 默认不可信 | mutation default、显式 trustToolAnnotations、schema/timeout |
| OAuth authorization server | 外部 | HTTPS、PKCE S256、state 由 SDK、resource audience |
| Docker image/daemon | TCB | opt-in、预拉取、container flags；不防 daemon/image 恶意 |
| Trace/artifact 内容 | 高敏 | best-effort redaction（仅 Trace）、本地路径；无统一 TTL/加密 |
| Workspace | 用户授权范围 | resolve containment；shell 可修改整个 workspace |

## 3. 分层控制

### 模型到副作用

pre Hook → resource resolution → JSON Schema → Permission/approval → Tool timeout → post Hook。未知 Tool/effect fail-closed。模型无法直接调用 Python 函数或跳过 Registry。

### 文件边界

默认文件工具通过 `resolve/relative_to` 限制 workspace，并拒绝真实 `.env*` 与 `.mini-oh/oauth`。List 过滤 VCS/cache/runtime 目录；`tools.py:218-225,266-280,321-332`。

### Hook 边界

CommandHook 不经宿主 shell且默认最小环境，但仍能在 workspace 中运行任意可信 argv。`inherit_environment` 是显式信任升级。Python callback 拥有进程权限。

### MCP/OAuth 边界

远端 readOnlyHint 默认无效；HTTP OAuth 要求安全 URL/loopback redirect/PKCE S256；token non-symlink、atomic、0600。stdio server 当前继承完整宿主 env，是较大的信任面。

### Docker 边界

无网络、只读 rootfs、drop capabilities、no-new-privileges、资源限制、secret mounts遮蔽。但 workspace 必须可写，daemon/image 属于 TCB，不能视为恶意多租户平台。

### 观测与持久化

Trace 默认递归脱敏；OAuth 文件显式 0600。Artifact、Trace 目录、Hook output 和 history 没有统一分类/retention/encryption。

## 4. Secrets 的流向

- OPENAI_API_KEY 从 shell/.env 到 CLI argparse，再进入 Provider Authorization header；正常不写 config。
- Trace model/tool/Hook payload 经过 redaction，但业务内容中的未知秘密可能保留。
- MCP static header 推荐 `headersEnv`，但会进入 HTTP client；MCP config本身不记录值。
- OAuth tokens 只由 SDK storage 使用，file tools/sandbox 对默认 workspace token path做遮蔽。
- stdio MCP 子进程继承完整 `os.environ`，可能看到 Provider key。

## 5. 安全不变量与非保证

### 已确认的不变量

1. 显式 Permission deny 不被 `--allow-write/--yes` 绕过。
2. Tool pre Hook 改参数后会重新做 resource/schema/permission。
3. 不可信 MCP effect 按 mutation。
4. Trace replay 不执行副作用。
5. Docker shell 不回退宿主，并遮蔽默认 secret paths。

### 明确不保证

- 恶意多租户代码隔离；
- Docker daemon/image supply-chain 安全；
- 所有 secrets 自动识别；
- 跨进程文件写协调；
- MCP 域名级 egress policy；
- Trace/artifact encryption/TTL；
- 不可信 Hook 隔离。

## 6. 扩展审查清单

新增 Provider/Tool/Hook/MCP server 时检查：数据会发送到哪里、effect 是否准确、资源 key 是否完整、默认权限、timeout/cancel、secret 是否进入 Trace/history、凭据存储权限、网络 egress、失败时是否 fail-closed、清理是否可靠。

## 7. 风险与改进方向

- stdio MCP 改为环境 allowlist + 显式透传。
- Trace/artifact root 设置权限、TTL、可选加密；artifact 也做分类/脱敏。
- Tool metadata 提供结构化 effect/source/permission resources。
- OAuth storage 支持 OS keyring。
- Sandbox 使用镜像 digest allowlist、明确 seccomp/AppArmor，保留完整输出到受控 artifact。
- Hook 引入可选容器执行器；typed payload 避免意外数据扩散。
- 为 threat model 建一份回归矩阵，而不只依赖功能测试。

## 8. 测试依据

- `tests/test_tools.py::test_read_cannot_escape_workspace`
- `test_runtime_secrets_are_hidden_from_file_tools`
- `tests/test_permissions.py::test_explicit_deny_overrides_allow_write`
- `tests/test_hooks.py::test_command_failure_blocks_without_inheriting_api_key`
- `tests/test_trace.py::test_trace_redacts_secret_keys_and_common_credentials_by_default`
- `tests/test_mcp.py::test_mcp_read_only_annotation_requires_explicit_server_trust`
- `test_oauth_token_storage_rejects_symlink`
- `test_oauth_refuses_authorization_server_without_pkce_s256`
- `tests/test_sandbox.py::test_docker_sandbox_argv_enforces_core_isolation`

## 9. 设计评价与阅读建议

最值得学习的是多层 fail-closed：effect、approval、schema、Hook、transport 和 OS isolation 不互相冒充。最大风险是“可信扩展”边界容易被误解，以及 Trace/artifact/stdin MCP environment 的数据面仍需生产级治理。

阅读顺序：`ToolRegistry.execute` → `_safe_path/_is_runtime_secret` → PermissionPolicy → CommandHook env → Trace redaction → MCP trust/OAuth storage → Docker argv。
