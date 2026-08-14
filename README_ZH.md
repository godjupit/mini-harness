# Mini Harness

[English](README.md) | **简体中文**

> 一个紧凑、安全感知的 Coding Agent 运行时，用于构建和学习 Agent。

Agent Loop · 权限引擎 · 自动审批 · 子 Agent · Hooks · MCP · Skills · 上下文压缩 · Trace/Replay · Bwrap Sandbox

Mini Harness 是一个小型但完整的 Coding Agent 运行时，专注于通常被产品界面隐藏起来的控制平面：模型与工具编排、权限决策、安全执行边界、生命周期 Hooks、上下文管理、可观测性、可恢复会话，以及子 Agent 委派。

项目状态：持续开发中。适合学习、实验、面试展示和本地原型开发——不定位为多租户生产级平台。

## 为什么是 Mini Harness？

一个真正可用的 Coding Agent 远不只是“循环调用 LLM”。当工具开始读取文件、修改工作区、调用远程服务或执行 Shell 命令时，运行时必须回答：

- 哪些操作可以直接执行、必须拒绝，或需要审批？
- 相互独立的工具调用能否并发执行，同时避免对同一文件产生竞争？
- 工具超时、或模型反复执行同一个动作时，如何终止异常循环？
- 大型工具输出和长对话如何控制在上下文窗口之内？
- 如何在不重新执行副作用的前提下，事后查看一次完整运行过程？

Mini Harness 把这些关注点实现为明确的运行时组件，而不是隐藏在一个庞大的 Agent 函数里。

## 架构

```text
                 ┌──────────────────────┐
                 │    Model Provider    │
                 │ Responses / Chat API │
                 └──────────┬───────────┘
                            │  text / tool calls
                            ▼
┌──────────────┐   ┌────────┴────────┐
│  User / CLI  ├──►│    AgentLoop    │
└──────────────┘   └────────┬────────┘
                            │
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                  ▼
   Context/Artifacts   Sessions          Trace Sink
                            │             (JSONL 审计
                            │              + 安全回放)
          pre-tool hook     ▼
          ─────────────► resource resolution + RW locks
                            │
                            ▼
          ┌──────────────────────────┐
          │      Tool Registry       │◄── Skills / MCP
          │  schema validate / authz  │
          └────────────┬─────────────┘
                       ▼
          ┌──────────────────────────┐
          │    PermissionEngine      │
          │  safety → deny/ask/allow │
          └────────────┬─────────────┘
                       │ ALLOW / ASK
                       ▼
          人工审批  或  自动审批 Agent
                       │
                       ▼
                 actual tool run
                       │
                       ▼
                 post-tool hook ──► observation ──► AgentLoop
```

内置的 `agent` 工具可以把只读的调查或规划任务委派给专用子 Agent `AgentLoop`，子 Agent 只拥有受限的工具子集。

## 核心能力

| 领域 | 运行时提供的功能 |
| --- | --- |
| Agent loop | 模型 → 工具 → 观察 → 模型，带类型化事件、取消、最大步数限制、严格的工具调用/结果配对 |
| 权限引擎 | 按 `safety → deny → ask → allow → default` 顺序评估，含 workspace 包含性检查和保守 Shell 检查 |
| 自动审批 | ASK 决策可交给独立的无工具 reviewer 模型；失败与歧义输出一律拒绝 |
| 子 Agent | 内置 Explore / Plan Agent 用受限工具运行自己的 `AgentLoop`；支持注册自定义定义 |
| 工具系统 | JSON Schema 校验、不可变安全元数据、结构化失败、超时、来源/效应标注 |
| 并发 | 感知效应的资源锁：独立工作并发执行，冲突文件访问串行化 |
| Hooks | `user_prompt_submit` / `pre_tool_use` / `post_tool_use` / `stop`，带优先级、匹配、fail-open/fail-closed |
| 验证闸门 | stop hook 可阻止完成，把测试/lint 反馈返回给模型修复后重试 |
| 上下文 | 工具回合原子压缩、带确定性兜底的交接摘要、上下文窗口恢复、大输出归档 |
| 追踪 | 追加式 JSONL trace，默认脱敏，覆盖权限/工具/provider/成本，支持剪枝与无副作用回放 |
| 会话 | 持久化对话，支持中断检测与 resume |
| MCP | stdio 与 Streamable HTTP、schema 校验、保守注解信任、OAuth |
| Sandbox | Bubblewrap 宿主 shell：真实宿主环境、只读文件系统、可写 workspace、全新 `/tmp` |

## 环境要求

- Python 3.10+
- 真实模型运行需要 OpenAI 兼容 API key
- 启用 `sandbox_shell` 时 Linux 需要 `bwrap`（bubblewrap）

## 安装

```bash
git clone <your-repository-url>
cd mini-harness

python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Windows PowerShell 激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

创建本地环境文件并填入 provider 配置：

```bash
cp .env.example .env
```

```dotenv
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_MODE=responses
```

CLI 会从当前目录加载 `.env`，且不覆盖 Shell 里已有的环境变量。

## 快速开始

离线演示（无需 API key，走真实运行时路径）：

```bash
mini-oh --demo --workspace . "Inspect this project and summarize its architecture."
```

真实模型运行：

```bash
mini-oh --workspace . "Inspect the repository and explain the agent runtime."
```

默认使用 Responses API；Chat Completions 兼容端点：

```bash
mini-oh \
  --api-mode chat \
  --base-url https://compatible.example/v1 \
  --workspace . \
  "Analyze the codebase."
```

## 权限模型

每次工具执行都会被转换为一个 `PermissionRequest`，携带工具声明的 source、effect、destructive 标记、path 和/或 shell command。决策顺序为：

1. 安全边界
2. 显式 DENY 规则
3. 显式 ASK 规则
4. 显式 ALLOW 规则
5. 运行时默认

按工具效应的运行时默认：

| 效应 | 默认 |
| --- | --- |
| `read` / `compute` | ALLOW |
| `write` / `remote` / `unknown` | ASK |

安全检查先于规则执行：

- workspace 路径逃逸 → 硬 DENY
- 多行 Shell 输入 → 硬 DENY
- 静态无法验证的 Shell 语法 → ASK

### 人工审批（DEFAULT 模式）

ASK 决策会在交互终端向用户确认；没有审批回调时 ASK 一律拒绝（fail closed）。

```bash
mini-oh --workspace . "Create docs/design.md"
```

### 自动审批（AUTO_REVIEW 模式）

`--auto-review` 把 ASK 决策交给一个独立的 reviewer 模型调用，而不是人工：

```bash
mini-oh --auto-review --workspace . "Refactor the parser and update the tests."
```

Reviewer 只会收到工具、效应、path/command、workspace、参数和权限原因，只能对该 ASK 返回 approve/reject。不变量：

- 显式 DENY 不会发给 reviewer，也不可被覆盖
- safety 的 DENY 永远是最终决定
- reviewer 出错、超时或输出无法解析 → 拒绝
- reviewer 无工具，无法自行执行操作

### 规则文件

```json
{
  "rules": [
    { "tool": "write_file", "path": "secrets/*", "action": "deny" },
    { "tool": "write_file", "path": "docs/*", "action": "allow" },
    { "tool": "sandbox_shell", "command": "npm publish*", "action": "ask" }
  ]
}
```

```bash
mini-oh --permission-config examples/permissions.json --workspace . "Update the docs."
```

规则模式使用 `fnmatch` 风格匹配；未命中的请求仍会落入运行时默认策略和不可绕过的安全检查。

## 子 Agent 委派

Mini Harness 向主模型暴露 `agent` 工具。子 Agent 拥有自己的 `AgentLoop`、专用 system prompt、回合上限，以及定义里声明的工具子集。

默认注册两个 Agent：

| Agent | 用途 | 默认工具 |
| --- | --- | --- |
| `explore_agent` | 搜索并理解代码库 | `read_file`, `list_files` |
| `plan_agent` | 基于已有上下文产出实施方案 | `read_file`, `list_files` |

默认子 Agent 只拿到只读工具，从结构上把“调查/规划”与“变更”分开。通过 `AgentRegistry` 注册自定义 Agent：

```python
from mini_openharness.multiagent import AgentDefinition, default_agents

agents = default_agents()
agents.register(
    AgentDefinition(
        type="reviewer",
        description="reviews implementation changes",
        system_prompt="You are a focused code review agent.",
        max_turns=20,
        tools=("read_file", "list_files"),
    )
)
```

委派是显式、可检查的——没有隐式共享内存、自主 swarm 或后台调度器。

## 工具执行与并发

内置本地工具：

| 工具 | 用途 |
| --- | --- |
| `read_file` | 读取 workspace 内的 UTF-8 文本文件 |
| `list_files` | 列出目录下的文件 |
| `write_file` | 写入 UTF-8 文本文件 |
| `edit_file` | 带快照校验的就地编辑 |

运行时注册的工具：

| 工具 | 何时出现 |
| --- | --- |
| `agent` | 子 Agent 委派 |
| `load_skill` | 配置了 skill catalog 时 |
| `sandbox_shell` | 启用 bwrap 沙箱 shell 时 |
| `mcp__...` | 从配置的 MCP server 发现 |

每个工具声明一个 `ToolDescriptor`，供权限评估、资源调度、追踪和归因复用：

```python
ToolDescriptor(
    source="extension",
    effect="write",
    destructive=True,
    path_argument="path",
)
```

### 资源感知调度

模型一次可能返回多个工具调用。每个调用解析为逻辑 `ResourceAccess`，用异步读写锁调度：

| 访问模式 | 行为 |
| --- | --- |
| 同一资源：read + read | 并发 |
| 同一资源：read + write | 串行 |
| 同一资源：write + write | 串行 |
| 不同文件 | 可并发 |
| 未知变更 | 全局写锁 |

即使执行乱序完成，工具观察仍按原始调用顺序返回给模型。

```bash
mini-oh --max-concurrent-tools 4 --workspace . "Inspect several files."
```

### 安全编辑

`edit_file` 使用乐观并发：`read_file` 记录本次运行的 SHA-256 快照，`edit_file` 要求该快照或显式 `expected_sha256`；文件过期则拒绝而非覆盖，歧义匹配默认拒绝，最终替换使用同目录临时文件 + `os.replace`。

## Hooks 与验证闸门

Hooks 是模型无法关闭的可信运行时扩展。

| 事件 | 触发时机 | 典型用途 |
| --- | --- | --- |
| `user_prompt_submit` | prompt 进入历史之前 | 规范化、策略检查 |
| `pre_tool_use` | 资源解析与执行之前 | 参数改写、拦截 |
| `post_tool_use` | 工具结束后 | 审计、输出过滤 |
| `stop` | 最终答案变成 done 之前 | 测试、lint、安全检查 |

stop hook 可以作为验证闸门：如果它阻止完成，输出会作为反馈返回给模型，让模型修复后再次尝试。

```bash
mini-oh \
  --hooks-config examples/hooks-verification.json \
  --workspace . \
  "Implement the change and make the tests pass."
```

命令式 hooks 直接用 `argv` 执行（不经 Shell），以 workspace 为工作目录，支持显式超时与 fail-open/fail-closed。

## 上下文压缩与归档

- 模型调用前估算上下文大小；超过阈值后，较旧的会话单元被交接摘要替换，近期单元保持原样
- 工具回合按原子单元处理，压缩不会产生悬空的协议状态
- 正常路径用配置的模型做一次无工具摘要请求；失败时回退到确定性摘要
- 大型工具输出归档到 `.mini-oh/artifacts/<run-id>/`，对话里只保留头尾预览

```bash
mini-oh \
  --context-threshold 12000 \
  --keep-recent 6 \
  --max-inline-output 8000 \
  "Work through a long repository task."
```

若 provider 显式报告上下文窗口错误，运行时可以强制压缩一次并重试同一个逻辑模型步骤。

## Trace 与回放

每次运行会在 `.mini-oh/traces/<run-id>.jsonl` 写追加式 JSONL trace，覆盖模型请求/响应、流式增量、工具生命周期、权限决策、资源等待、hooks、压缩、MCP 归因、用量、成本与最终状态。敏感字段默认脱敏。

```bash
mini-oh trace list
mini-oh trace show <run-id>
mini-oh trace replay <run-id>
mini-oh trace prune --older-than 30          # 默认 dry-run
mini-oh trace prune --max-runs 100 --apply
```

`trace replay` 只渲染记录的时间线——不会再次调用模型或执行工具。

## 会话与恢复

```bash
mini-oh sessions
mini-oh resume <session-id>
mini-oh resume --latest
```

`continue` 是 `resume` 的别名。

## MCP 与 Skills

### MCP

```bash
mini-oh --mcp-config examples/mcp.json --workspace . "Inspect available MCP tools."
```

支持 stdio 与 Streamable HTTP 传输、输入/输出 schema 校验、结构化内容保留、保守的 `readOnlyHint` 处理，以及带本地 token 持久化的 OAuth 流程。远程工具不会因为“可达”就被隐式信任。

### Skills

```text
skills/
└── my-skill/
    └── SKILL.md
```

初始只暴露轻量元数据；模型在真正需要时通过 `load_skill` 加载完整内容。

```bash
mini-oh --skills-dir ./skills --workspace . "Use the available project skills."
```

## 沙箱 Shell（bubblewrap）

`sandbox_shell` 在 bubblewrap 沙箱中直接运行宿主 bash。宿主环境（python、pytest、git、node、`.venv`、PATH）开箱可用；只有 workspace 可写，其余文件系统只读，`/tmp` 是全新的临时目录。工作目录跨命令保持。

```bash
mini-oh --workspace . "Run the project checks."
```

没有无沙箱兜底：如果未安装 `bwrap`，shell 工具直接不可用，而不会在无沙箱状态下运行于宿主。

这是本地开发的安全边界，不是面向恶意多租户的沙箱。

## 可靠性控制

- 每个工具独立墙钟超时
- 可配置模型重试策略
- 重复相同工具批次的熔断器
- 最大模型步数与最大并发工具数
- 权限与 reviewer 失败一律 fail closed
- 面向截断与上下文溢出的类型化 provider 错误
- 取消传播；每个 `AgentLoop` 同时只有一个 active run
- 结构化 `ToolFailure(code, stage, retryable)` 元数据

```bash
mini-oh \
  --tool-timeout 20 \
  --max-repeated-tool-batches 2 \
  --max-concurrent-tools 4 \
  --max-steps 16 \
  "Diagnose and repair the project."
```

## 项目结构

```text
mini-harness/
├── src/mini_openharness/
│   ├── engine.py              # agent 状态机与工具编排
│   ├── provider.py            # Responses + Chat 兼容 provider
│   ├── tools.py               # 注册表、描述符、锁、文件工具
│   ├── permissions/           # safety、rules、engine、approval handlers
│   ├── multiagent.py          # 子 Agent 定义、注册表、委派工具
│   ├── hooks.py               # 生命周期 hook 注册表与执行器
│   ├── compaction.py          # 摘要与归档
│   ├── trace.py               # JSONL trace、回放、剪枝
│   ├── session.py             # 持久会话与 resume 逻辑
│   ├── skills.py              # 渐进式 skill 加载
│   ├── mcp.py                 # MCP 集成
│   ├── mcp_auth.py            # HTTP MCP OAuth 支持
│   ├── sandbox.py             # bwrap 沙箱宿主 shell
│   ├── models.py              # 消息与工具调用数据结构
│   └── cli.py                 # `mini-oh` 命令行入口
├── tests/                     # 运行时与协议测试
├── examples/                  # permissions、hooks、MCP、reviewer 演示
├── docs/                      # 引导式代码阅读笔记
├── TECHNICAL_DESIGN.md        # 设计原理与实现细节
├── pyproject.toml
└── LICENSE
```

## 开发

```bash
pip install -e '.[dev]'
pytest -q
ruff check .
```

测试覆盖 agent loop、工具、权限、自动审批、hooks、provider、压缩、追踪、会话、skills、MCP、沙箱和子 Agent 委派。

## 设计原则

- 副作用需要控制平面：校验、权限、hooks 与资源调度都在执行之前完成
- 未知行为保守失败
- 模型收到可恢复的失败：工具错误尽可能作为 observation 返回
- 工具协议状态保持有效：调用与结果在执行、压缩、中断、恢复过程中始终配对
- 可观测性不得重放副作用
- 安全声明与实现一致：bwrap 隔离、权限策略、OAuth 边界均注明局限
- 复杂度要配得上价值：偏好小而可检查的机制，而非隐藏的框架行为

## 局限

- 资源锁是进程内的
- 宿主文件系统只读绑定属于可信计算基
- 本地 OAuth token 文件有权限保护，但未做 OS 钥匙串加密
- Hooks 是可信扩展，不是沙箱插件
- 默认子 Agent 是轻量委派循环，不是自主分布式 worker
- 没有 TUI、插件市场或远程多用户执行服务
- 权限规则刻意保持简单的 glob 规则，而非完整策略语言

更深入的设计说明见 [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md)。

## License

Mini Harness 基于 [MIT License](LICENSE) 开源。

---

*小到可以理解，完整到足以暴露 Agent 运行时的真实问题。*
