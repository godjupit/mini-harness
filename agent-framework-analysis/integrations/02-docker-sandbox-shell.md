# Docker-only sandbox shell

> 分析状态：已验证  
> 优先级：P1  
> 模块类型：Adapter  
> 主要代码：`src/mini_openharness/sandbox.py`

## 1. 模块职责与独立性

**已确认**：该模块以每次调用一个 disposable Docker container 的方式提供 opt-in `sandbox_shell` Tool，限制网络、rootfs、capabilities、CPU/memory/PID/tmpfs，并在 timeout/cancel 时清理；`sandbox.py:35-212`。

它是独立外部系统适配器，有自己的配置、可用性检查、进程/container lifecycle 与安全边界。它不是恶意多租户 sandbox，也没有 host shell fallback。

## 2. 对外接口

- `DockerSandboxConfig`：image、memory、cpus、pids、tmpfs。
- `DockerSandbox.ensure_available/build_argv/run`。
- `SandboxedShellTool`：实现统一 Tool Protocol。
- `SandboxUnavailableError`。

包根导出 Sandbox 与 Tool，CLI 通过 `--sandbox-shell` 和资源 flags 启用。

## 3. 隔离参数

`build_argv()` 构造：`--rm --init --network none --read-only --cap-drop ALL --security-opt no-new-privileges`，限制 CPU/memory/PID，`/tmp` noexec/nosuid/nodev，使用宿主 uid/gid，仅把 workspace bind 到 `/workspace`；`sandbox.py:60-117`。

workspace 中真实 `.env*` bind-over `/dev/null`，`.mini-oh/oauth` 用 mode 000 tmpfs 遮蔽。Docker socket、宿主配置和宿主 env 不挂载。

## 4. 执行与清理

启用时 CLI 先 `docker image inspect`，Docker/image 不可用即启动失败。每次 run 生成唯一 container name，执行 `/bin/sh -lc command`，捕获合并 stdout/stderr；timeout/cancel 时先 `docker rm -f` 再 stop process；`sandbox.py:119-167,215-222`。

输出超过 12,000 字在 adapter 内截断；之后仍可能经 ArtifactStore 再判断。退出码非 0 变成 `ToolResult.is_error`。

## 5. 与统一 Tool 链交互

Shell 声明 mutation，并对整个 workspace 申请 tree write resource，因此与任何 workspace file access 冲突。它仍经过 pre Hook、schema、Permission/approval、Registry timeout、post Hook、Trace 和 Artifact。

命令参数内 `timeout_seconds` 上限 600，并取该值与 ToolContext timeout 的最小值；Registry 外层还会再次 `wait_for`。

## 6. 扩展与边界情况

- 当前没有 Sandbox Protocol，替换 Podman/Kubernetes 需新 Tool 或抽接口。
- Docker daemon 与 image supply chain 是 trusted computing base；root/privileged daemon 可影响宿主。
- workspace 必须可写以完成 coding task，因此不隔离项目内容破坏；Permission/Hook 是上层控制。
- no network 会阻止依赖下载；这是安全选择而非缺陷。
- `sh -lc` 在容器内解释模型命令，宿主 argv 本身不经 shell。
- output adapter 截断会丢失尾部之外完整内容，ArtifactStore 无法恢复已经截掉的部分。
- `.env` 遮蔽通过遍历 workspace 构建 mounts，大仓库有启动成本；符号链接/非普通 secrets 的边界需进一步威胁建模。

## 7. 测试依据

- `tests/test_sandbox.py::test_docker_sandbox_argv_enforces_core_isolation`
- `test_sandbox_tool_explains_container_to_workspace_path_mapping`
- `test_real_docker_shell_writes_only_workspace_and_has_no_network`
- `test_docker_shell_timeout_removes_container`

## 8. 设计评价与阅读建议

- 值得学习：明确 opt-in/fail-closed、一次一容器、无 host fallback、双层 secret deny。
- 潜在问题：非多租户保证、Docker TCB、输出先截断、具体实现不可替换。
- 改进方向：SandboxExecutor Protocol、保留完整 stdout 到 artifact、镜像 digest allowlist、seccomp/apparmor 显式配置、威胁模型文档。
- 精读：`ensure_available`、`build_argv`、`run`、`_remove/_stop_process`、`SandboxedShellTool.resources/run`、`_workspace_secret_files`。
