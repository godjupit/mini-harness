<div align="center">

Mini Harness

一个紧凑、安全感知的 Coding Agent 运行时






Agent Loop · 权限引擎 · 自动审批 · 子 Agent · Hooks · MCP · Skills · 上下文压缩 · Trace/Replay · Docker Sandbox

</div>

Mini Harness 是一个小型但完整的 Coding Agent 运行时，重点实现那些通常被产品界面隐藏起来的核心能力：模型与工具编排、权限决策、安全执行边界、生命周期 Hooks、上下文管理、可观测性、可恢复会话，以及子 Agent 委派。

项目刻意保持运行时结构清晰、可理解。它不试图复刻一个完整的 IDE、终端产品或商业 Coding Agent，而是专注于构建 Agent 真正需要的控制平面，使其具备 可观测、可授权、可恢复、可扩展 的运行能力。

项目状态： 持续开发中。当前运行时适合学习、实验、面试展示以及本地 Agent 原型开发；它并不定位为经过强化的多租户生产级执行平台。

为什么是 Mini Harness？

一个真正可用的 Coding Agent，远不只是“循环调用 LLM”。

当工具开始读取文件、修改工作区、调用远程服务，甚至执行 Shell 命令时，运行时必须回答更多问题：

哪些操作可以直接执行，哪些必须拒绝，哪些需要审批？

相互独立的工具调用能否并发执行，同时避免对同一文件产生竞争？

工具超时，或者模型反复执行同一个动作时，该如何终止异常循环？

大型工具输出和长对话如何控制在上下文窗口之内？

如何在不重新执行副作用的前提下，事后查看一次完整运行过程？

如何让专门的 Agent 执行探索或规划任务，而不是给所有 Agent 相同的工具权限？

如何在稳定的生命周期边界执行测试、Lint、安全检查或组织自定义逻辑？

Mini Harness 将这些问题拆分为明确的运行时组件，而不是把所有逻辑隐藏在一个庞大的 Agent 函数中。

架构

                                   ┌──────────────────────┐
                                   │    Model Provider    │
                                   │ Responses / Chat API │
                                   └──────────┬───────────┘
                                              │
                                      text / tool calls
                                              │
┌──────────────┐     prompt hooks      ┌──────▼───────┐
│ User / CLI   ├──────────────────────►│  AgentLoop   │
└──────────────┘                       └──────┬───────┘
                                            │
                              ┌─────────────┼──────────────┐
                              │             │              │
                              ▼             ▼              ▼
                       Context/Artifacts  Sessions     Trace Sink
                              │                            │
                              │                       JSONL audit
                              │                       + safe replay
                              │
                              ▼
                     pre-tool hook
                              │
                              ▼
                     resource resolution
                       + async RW locks
                              │
                              ▼
                     ┌─────────────────┐
                     │  Tool Registry  │◄──────── Skills / MCP
                     │ schema validate │
                     └────────┬────────┘
                              │
                              ▼
                     ┌─────────────────┐
                     │ PermissionEngine│
                     │ safety → rules  │
                     └────────┬────────┘
                              │
                  ALLOW ──────┼────── ASK
                              │        │
                              │        ▼
                              │   Human approval
                              │        or
                              │   Auto-review agent
                              │
                              ▼
                       actual tool run
                              │
                              ▼
                         post-tool hook
                              │
                              └──────────────► observation → AgentLoop

内置 `agent` 工具可以将只读探索或规划任务委派给专门的子 Agent。
每个子 Agent 都拥有独立的 AgentLoop，并且只能访问受限制的工具集合。

核心能力

模块

运行时能力

Agent Loop

完整的 Model → Tool Calls → Observations → Model 循环，支持类型化事件、取消、最大步数限制，以及严格的 Tool Call / Tool Result 配对。

权限引擎

按 safety → deny → ask → allow → default 顺序执行权限决策，包含工作区路径约束和保守的 Shell 安全检查。

自动审批

ASK 请求可交给一个独立、无工具权限的 Reviewer 模型进行审核；异常、超时或无法解析的响应默认拒绝。

子 Agent

内置 Explore 与 Plan Agent，各自运行独立 AgentLoop 和受限工具集合；支持注册自定义 Agent。

工具系统

JSON Schema 校验、不可变安全元数据、结构化失败、超时控制，以及 source/effect 属性描述。

并发调度

基于资源 Effect 的读写锁允许互不冲突的工具并发执行，同时序列化存在文件冲突的操作。

Hooks

支持 user_prompt_submit、pre_tool_use、post_tool_use、stop 生命周期 Hook，包含优先级、匹配、超时以及 fail-open/fail-closed 策略。

验证门

stop Hook 可以阻止 Agent 结束，将测试或 Lint 结果重新反馈给模型，使 Agent 修复问题后再次尝试完成任务。

上下文管理

原子化 Tool Turn 压缩、模型生成交接摘要、确定性降级摘要、上下文窗口溢出恢复，以及大型输出 Artifact 化。

Tracing

追加式 JSONL Trace，支持敏感信息脱敏、耗时、权限/工具/Provider 事件、成本统计、清理以及无副作用 Replay。

Sessions

持久化会话、异常中断检测，以及 resume / continue 恢复。

MCP

支持 stdio 与 Streamable HTTP、Schema 校验、保守的 Annotation 信任策略以及 OAuth。

Sandbox

可选 Docker Shell Sandbox，无宿主机回退、无网络、只读根文件系统、Capabilities 全部移除，并限制 CPU、内存和 PID。

环境要求

Python 3.10+

真实模型运行需要 OpenAI 或 OpenAI-Compatible API Key

仅在启用 sandbox_shell 时需要 Docker

安装

克隆仓库并以 editable 模式安装：

git clone <your-repository-url>
cd mini-harness

python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

Windows PowerShell：

.\.venv\Scripts\Activate.ps1

创建本地环境配置：

cp .env.example .env

然后配置模型 Provider：

OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_MODE=responses

CLI 会从当前目录读取 .env，但不会覆盖 Shell 环境中已经存在的同名变量。

快速开始

离线 Demo

确定性 Demo 不需要 API Key，但仍会走真实的运行时路径：

mini-oh --demo --workspace . "Inspect this project and summarize its architecture."

使用真实模型

mini-oh --workspace . "Inspect the repository and explain the agent runtime."

默认 Provider 使用 Responses API。也可以显式切换到兼容 Chat Completions 的接口：

mini-oh \
  --api-mode chat \
  --base-url https://compatible.example/v1 \
  --workspace . \
  "Analyze the codebase."

权限模型

每一次工具执行都会先转换为 PermissionRequest，其中包含该工具声明的 source、effect、destructive 标记，以及 path 或 shell command 等安全相关信息。

权限决策顺序保持简单且明确：

1. Safety boundary
2. Explicit DENY rules
3. Explicit ASK rules
4. Explicit ALLOW rules
5. Runtime default

默认运行时策略：

read / compute           → ALLOW
write / remote / unknown → ASK

Safety 检查位于所有可配置规则之前，因此无法被普通权限规则绕过。

当前实现中：

工作区路径逃逸：硬 DENY；

多行 Shell 输入：硬 DENY；

无法静态确认安全性的 Shell 语法：强制进入 ASK。

默认人工审批

当请求最终得到 ASK 时，普通 CLI 模式会在交互式终端中请求用户确认：

mini-oh --workspace . "Create docs/design.md"

如果运行时不存在可用的审批回调，ASK 会默认失败关闭，而不是自动放行。

Auto Review 自动审批

--auto-review 会使用独立 Reviewer 模型替代人工处理 ASK：

mini-oh --auto-review --workspace . "Refactor the parser and update the tests."

Reviewer 会接收到当前请求对应的工具名、effect、path / command、workspace、arguments，以及触发审批的权限原因。

它只能针对这一次具体的 ASK 请求给出是否批准的判断。

核心约束：

显式 DENY 不会发送给 Reviewer，也无法被 Reviewer 覆盖；

Safety DENY 始终是最终决策；

Reviewer 报错、超时或返回非法结果时，请求默认拒绝；

Reviewer 本身不持有任何工具，因此不能自行执行被审核的操作。

权限规则

可以通过 JSON 文件声明显式的 deny、ask 与 allow 规则：

{
  "rules": [
    {
      "tool": "write_file",
      "path": "secrets/*",
      "action": "deny"
    },
    {
      "tool": "write_file",
      "path": "docs/*",
      "action": "allow"
    },
    {
      "tool": "sandbox_shell",
      "command": "npm publish*",
      "action": "ask"
    }
  ]
}

运行：

mini-oh --permission-config examples/permissions.json --workspace . "Update the docs."

规则使用 fnmatch 风格匹配。

当指定 Permission Config 后，其中的 rules 会成为当前显式规则集合；没有命中的请求仍然回落到运行时默认策略，同时继续受不可绕过的 Safety 检查约束。

子 Agent 委派

Mini Harness 向主模型暴露一个 agent 工具。

委派机制保持轻量：每个子 Agent 都拥有自己的 AgentLoop、专门的 System Prompt、最大 Turn 限制，以及 Agent Definition 中明确声明的工具集合。

默认注册两个 Agent：

Agent

用途

默认工具

explore_agent

搜索并理解代码库

read_file, list_files

plan_agent

根据已有上下文生成实现计划

read_file, list_files

由于默认子 Agent 只拥有只读工具，因此从结构上将“探索 / 规划”与“修改工作区”分离。

可以通过 AgentRegistry 注册自定义 Agent：

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

这里的委派层并不试图成为一个分布式 Multi-Agent Framework：不存在隐式共享记忆、自治 Agent Swarm 或后台任务调度器。

目标只是提供一种明确、受控、可检查的任务委派机制。

工具执行与并发

内置本地工具：

read_file
list_files
write_file
edit_file

运行时还可以按配置注册：

agent           专门的子 Agent 委派
load_skill      配置 Skill Catalog 后启用
sandbox_shell   显式启用 Docker Sandbox 后注册
mcp__...        从已配置 MCP Server 动态发现的工具

每个工具都可以通过 ToolDescriptor 声明自己的 source 和 effect：

ToolDescriptor(
    source="extension",
    effect="write",
    destructive=True,
    path_argument="path",
)

这些元数据会统一被权限系统、资源调度、Tracing 和 Attribution 使用，而不是在不同模块中反复根据工具名称猜测行为。

Resource-aware Scheduling

一次模型响应中可能包含多个 Tool Call。

Mini Harness 会先将每个调用解析为逻辑 ResourceAccess，再通过异步读写锁进行调度：

同一资源：read + read   → 并发
同一资源：read + write  → 串行
同一资源：write + write → 串行
不同文件                 → 可以并发
未知 mutation            → 全局写锁

即使多个工具实际完成顺序不同，返回给模型的 Observation 仍会保持原始 Tool Call 顺序。

最大工具并发数可以通过以下参数控制：

mini-oh --max-concurrent-tools 4 --workspace . "Inspect several files."

安全编辑

edit_file 使用乐观并发保护：

read_file 为当前 Run 记录文件 SHA-256 Snapshot；

edit_file 要求该 Snapshot，或者显式提供 expected_sha256；

文件发生变化后，旧 Snapshot 会被拒绝，而不是覆盖新内容；

Replacement 必须精确匹配，默认拒绝歧义匹配；

最终写入使用同目录临时文件和 os.replace 完成替换。

这能够避免 Agent 基于过期上下文覆盖文件，但并不声称提供跨进程数据库事务级别的并发一致性。

Hooks 与验证门

Hooks 是可信的运行时扩展，模型无法选择绕过。

支持的生命周期事件：

Event

执行时机

常见用途

user_prompt_submit

Prompt 写入 History 之前

输入规范化、策略检查

pre_tool_use

资源解析和 Tool Execution 之前

参数重写、阻止调用

post_tool_use

工具完成后、结果返回模型之前

审计、输出过滤

stop

Final Answer 转换为 done 之前

Tests、Lint、安全验证

stop Hook 可以充当 Verification Gate。

如果 Hook 阻止任务完成，其失败输出会作为反馈重新送回 Agent，使模型可以根据验证结果修复问题，然后再次尝试结束任务。

示例：

mini-oh \
  --hooks-config examples/hooks-verification.json \
  --workspace . \
  "Implement the change and make the tests pass."

Command Hook 直接使用 argv 执行，而不是经过 Shell；工作目录固定为 Workspace，并支持显式 Timeout 以及 fail-open / fail-closed 行为。

上下文压缩与 Artifacts

在每一次模型调用之前，运行时都会估算当前上下文大小。

达到设定阈值后，较早的 Conversation Unit 可以被压缩为 Handoff Summary，而最近的上下文仍保持原文。

Tool Turn 按原子单元处理：Assistant 的 Tool Call 与对应 Tool Result 会一起保留，从而避免压缩后产生悬空的协议状态。

正常压缩路径会调用当前模型生成一次 no-tools Summary。

如果该辅助请求失败或返回不可用结果，则回退到确定性的本地 Summary。

大型 Tool Output 会被转存至：

.mini-oh/artifacts/<run-id>/

Conversation 中只保留头尾预览与 Artifact 路径，而不是把完整输出长期塞入上下文。

常用参数：

mini-oh \
  --context-threshold 12000 \
  --keep-recent 6 \
  --max-inline-output 8000 \
  "Work through a long repository task."

如果 Provider 明确报告 Context Window Overflow，运行时可以强制执行一次压缩，并对同一个逻辑模型步骤重试一次。

Trace 与 Replay

除非显式关闭，每次运行都会将 Append-only JSONL Trace 写入：

.mini-oh/traces/<run-id>.jsonl

Trace 事件覆盖：

模型请求与响应；

Streaming Delta；

Tool 生命周期；

Permission Decision；

Resource Wait；

Hooks；

Context Compaction；

MCP Attribution；

Token Usage 与估算成本；

最终 Run State。

敏感字段名称和常见 Credential Pattern 默认会被脱敏。

在系统支持的情况下，本地 Trace 文件会使用仅 Owner 可访问的权限创建。

查看 Trace：

mini-oh trace list
mini-oh trace show <run-id>
mini-oh trace replay <run-id>

trace replay 只会渲染已经记录的运行时间线。

它不会重新调用模型，也不会重新执行任何工具。

Trace 清理默认为 Dry Run：

mini-oh trace prune --older-than 30
mini-oh trace prune --max-runs 100 --apply

Sessions 与恢复

Conversation History 可以独立于 Trace 进行持久化。

这样，即使一次 Run 在中途被打断，也可以继续恢复，而不需要假装某个未完成的 Tool Call 已经成功执行。

列出 Session：

mini-oh sessions

恢复指定 Session：

mini-oh resume <session-id>

恢复最近一次 Session：

mini-oh resume --latest

continue 同样可以作为 resume 的别名。

MCP 与 Skills

MCP

可以通过 JSON 配置 MCP Server，并将远程工具接入与本地 Tool 相同的 Registry、Permission、Timeout 和 Trace 边界。

mini-oh --mcp-config examples/mcp.json --workspace . "Inspect available MCP tools."

当前实现支持：

stdio MCP Server；

Streamable HTTP Transport；

Input / Output Schema Validation；

Structured Content 保留；

对 readOnlyHint Annotation 的保守处理；

OAuth，包括面向 PKCE 的安全检查以及本地 Token 持久化。

远程工具不会因为“能够连接”就被默认视为可信工具。

Skills

Skill 目录结构：

skills/
└── my-skill/
    └── SKILL.md

配置 Skill Catalog 后，初始阶段只会暴露轻量级 Metadata。

模型只有在真正需要该 Skill 时，才通过 load_skill 加载完整 Skill 内容。

mini-oh --skills-dir ./skills --workspace . "Use the available project skills."

Docker Sandbox Shell

Shell Execution 默认关闭。

显式启用：

docker pull alpine:3.20

mini-oh \
  --sandbox-shell \
  --sandbox-image alpine:3.20 \
  --workspace . \
  "Run the project checks."

Sandbox 不存在 Host Shell Fallback。

如果 Docker 或指定镜像不可用，运行时会直接启动失败，而不是静默回退到宿主机 Shell 执行命令。

每次调用都会创建 Disposable Container，并应用以下限制：

--network none；

Container Root Filesystem 只读；

Workspace 以可写 Bind Mount 挂载；

移除全部 Linux Capabilities；

no-new-privileges；

CPU、Memory、PID 与 tmpfs 限制；

在适用平台上映射 Host UID/GID；

屏蔽部分运行时 Credential 路径。

这是用于本地开发的安全边界，而不是针对恶意多租户环境设计的 Sandbox。

可靠性控制

Mini Harness 包含一组小而明确的机制，用来避免常见 Agent Runtime 故障：

单工具 Wall-clock Timeout；

可配置 Model Retry Policy；

重复相同 Tool Batch 的 Circuit Breaker；

最大 Model Step 限制；

最大 Tool Concurrency 限制；

Permission 与 Reviewer 异常默认 Fail Closed；

Provider Truncation / Context Overflow 类型化错误；

Cancellation Propagation；

每个 AgentLoop Instance 同时只允许一个 Active Run；

结构化 ToolFailure(code, stage, retryable) 元数据。

示例：

mini-oh \
  --tool-timeout 20 \
  --max-repeated-tool-batches 2 \
  --max-concurrent-tools 4 \
  --max-steps 16 \
  "Diagnose and repair the project."

项目结构

mini-harness/
├── src/mini_openharness/
│   ├── engine.py              # Agent 状态机与 Tool 编排
│   ├── provider.py            # Responses + Chat-compatible Provider
│   ├── tools.py               # Registry、Descriptor、Locks、File Tools
│   ├── permissions/           # Safety、Rules、Engine、Approval Handlers
│   ├── multiagent.py          # Subagent Definition、Registry、Delegation Tool
│   ├── hooks.py               # 生命周期 Hook Registry 与 Executor
│   ├── compaction.py          # Summary 与 Artifact Offloading
│   ├── trace.py               # JSONL Trace、Replay、Pruning
│   ├── session.py             # Persistent Session 与 Resume Logic
│   ├── skills.py              # Progressive Skill Loading
│   ├── mcp.py                 # MCP Integration
│   ├── mcp_auth.py            # HTTP MCP OAuth Support
│   ├── sandbox.py             # Docker-only Shell Execution
│   ├── models.py              # Message 与 Tool Call 数据结构
│   └── cli.py                 # `mini-oh` CLI
├── tests/                     # Runtime 与 Protocol Tests
├── examples/                  # Permissions、Hooks、MCP、Reviewer Demos
├── docs/                      # Guided Code-reading Notes
├── TECHNICAL_DESIGN.md        # 设计理由与实现细节
├── pyproject.toml
└── LICENSE

开发

安装开发依赖：

pip install -e '.[dev]'

运行测试：

pytest -q

运行 Ruff：

ruff check .

当前仓库中的测试覆盖 Agent Loop、Tools、Permissions、Auto Review、Hooks、Provider Contract、Context Compaction、Tracing、Sessions、Skills、MCP、Sandbox 以及 Subagent Delegation 等核心模块。

设计原则

Mini Harness 遵循几条明确的设计原则：

任何副作用都需要控制平面。 Validation、Permission、Hooks 与 Resource Scheduling 必须在真正执行操作之前完成。

未知行为采用保守策略。 对无法确认的 Mutation 使用更严格的默认权限与资源锁，而不是乐观放行。

模型应当能够从失败中恢复。 Tool Error 尽可能作为 Observation 返回给 Agent，使模型能够调整策略，而不是直接导致 Runtime 崩溃。

Tool Protocol State 必须始终合法。 Tool Call 与 Tool Result 在执行、压缩、中断和 Resume 过程中必须保持正确配对。

可观测性不能重新制造副作用。 Trace Replay 只展示历史证据，不重新执行 Agent。

安全声明必须与真实实现一致。 Docker Isolation、Permission Policy 与 OAuth Boundary 的描述必须同时写清能力和限制。

任何复杂性都必须有存在价值。 项目优先选择小型、透明、可检查的机制，而不是隐藏在 Framework 内部的魔法行为。

当前范围与限制

Mini Harness 并不试图覆盖商业 Coding Agent 产品中的全部能力。

当前边界包括：

Resource Lock 仅在当前进程内生效；

Docker 仍属于 Trusted Computing Base 的一部分；

本地 OAuth Token 文件受文件权限保护，但没有使用 OS Keychain 加密；

Hooks 是可信扩展，而不是运行在独立 Sandbox 中的插件；

默认 Subagent 是轻量级 Delegated Loop，而不是自治分布式 Worker；

当前没有 TUI、Plugin Marketplace 或远程多用户执行服务；

Permission Rule 使用刻意保持简单的 Glob Pattern，而不是完整 Policy Language。

更深入的实现细节和设计权衡请参阅 TECHNICAL_DESIGN.md。

License

Mini Harness 基于 MIT License 开源。

<div align="center">

足够小，小到可以真正读懂；足够完整，完整到能够暴露 Agent Runtime 的真实问题。

</div>