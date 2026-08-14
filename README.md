<div align="center">

Mini Harness

A compact, safety-aware runtime for building and studying coding agents






Agent loop · permission engine · auto-review · subagents · hooks · MCP · skills · context compaction · trace/replay · Docker sandbox

</div>

Mini Harness is a small but complete coding-agent runtime focused on the parts that are usually hidden behind a product interface: model/tool orchestration, permission decisions, safe execution boundaries, lifecycle hooks, context management, observability, resumable sessions, and delegated subagents.

The project intentionally keeps the runtime understandable. Instead of reproducing a full IDE or terminal product, it concentrates on the control plane required to make an agent observable, permissioned, recoverable, and extensible.

Status: active development. The runtime is suitable for learning, experimentation, interviews, and local agent prototyping. It is not intended to be a hardened multi-tenant execution platform.

Why Mini Harness?

A useful coding agent is more than a loop that repeatedly calls an LLM. Once tools can read files, modify a workspace, call remote services, or execute shell commands, the runtime must answer harder questions:

Which actions are allowed, denied, or require review?

Can independent tool calls run concurrently without racing on the same files?

What happens when a tool times out or the model repeats the same action forever?

How do large tool outputs and long conversations fit inside the context window?

How can a run be inspected later without replaying its side effects?

How can specialized agents investigate or plan without giving every agent every tool?

How can organization-specific checks run at reliable lifecycle boundaries?

Mini Harness implements those concerns as explicit runtime components rather than burying them inside one monolithic agent function.

Architecture

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

The built-in `agent` tool can delegate read-only investigation or planning to
specialized subagent AgentLoops with a restricted tool subset.

Core capabilities

Area

What the runtime provides

Agent loop

Model → tool calls → observations → model, with typed events, cancellation, max-step limits, and strict tool-call/result pairing.

Permission engine

Ordered safety → deny → ask → allow → default evaluation with workspace containment and conservative shell checks.

Auto-review

ASK decisions can be sent to an independent tool-less reviewer model; failures and ambiguous responses fail closed.

Subagents

Built-in Explore and Plan agents run their own AgentLoop with restricted tools; custom agent definitions can be registered.

Tool system

JSON Schema validation, immutable security metadata, structured failures, timeouts, and source/effect attribution.

Concurrency

Effect-aware resource locking allows independent work to run concurrently while conflicting file access is serialized.

Hooks

user_prompt_submit, pre_tool_use, post_tool_use, and stop lifecycle hooks with priority, matching, timeouts, and fail-open/fail-closed behavior.

Verification gate

A stop hook can block completion, return test/lint feedback to the model, and let the agent repair before trying to finish again.

Context management

Atomic tool-turn compaction, model-generated handoff summaries with deterministic fallback, reactive context-window recovery, and large-output artifacts.

Tracing

Append-only JSONL traces with secret redaction, timing, permission/tool/provider events, cost accounting, pruning, and side-effect-free replay.

Sessions

Persistent conversations with interruption detection and resume / continue support.

MCP

stdio and Streamable HTTP transports, schema validation, conservative annotation trust, and OAuth support.

Sandbox

Optional Docker-only shell with no host fallback, no network, read-only root filesystem, dropped capabilities, and resource limits.

Requirements

Python 3.10+

An OpenAI-compatible API key for real model runs

Docker only if sandbox_shell is enabled

Installation

Clone the repository and install it in editable mode:

git clone <your-repository-url>
cd mini-harness

python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

On Windows PowerShell, activate the environment with:

.\.venv\Scripts\Activate.ps1

Create a local environment file:

cp .env.example .env

Then set your provider configuration:

OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_MODE=responses

The CLI loads .env from the current directory without overriding variables already present in the shell environment.

Quick start

Offline demo

The deterministic demo exercises the real runtime path without requiring an API key:

mini-oh --demo --workspace . "Inspect this project and summarize its architecture."

Real model run

mini-oh --workspace . "Inspect the repository and explain the agent runtime."

The default provider path uses the Responses API. A compatible Chat Completions endpoint can be selected explicitly:

mini-oh \
  --api-mode chat \
  --base-url https://compatible.example/v1 \
  --workspace . \
  "Analyze the codebase."

Permission model

Every tool execution is converted into a PermissionRequest carrying the tool's declared source, effect, destructive flag, path, and/or shell command.

The decision order is intentionally simple and explicit:

1. Safety boundary
2. Explicit DENY rules
3. Explicit ASK rules
4. Explicit ALLOW rules
5. Runtime default

The runtime defaults are:

read / compute          → ALLOW
write / remote / unknown→ ASK

Safety checks happen before configurable rules. In the current implementation:

workspace path escape is a hard DENY;

multi-line shell input is a hard DENY;

shell syntax that cannot be statically verified is forced to ASK.

Default human approval

When a request resolves to ASK, the normal CLI mode asks the user for approval in an interactive terminal:

mini-oh --workspace . "Create docs/design.md"

If no approval callback is available, an ASK decision fails closed.

Auto-review mode

--auto-review replaces the human decision for ASK with an independent reviewer model call:

mini-oh --auto-review --workspace . "Refactor the parser and update the tests."

The reviewer receives the requested tool, effect, path/command, workspace, arguments, and the permission reason. It can only return an approval decision for that specific ASK request.

Important invariants:

an explicit DENY is never sent to the reviewer and cannot be overridden;

safety DENY remains final;

reviewer errors, timeouts, or invalid responses reject the action;

the reviewer is invoked without tools, so it cannot execute the requested operation itself.

Permission rules

A JSON rule file can provide explicit deny, ask, and allow behavior:

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

Run with:

mini-oh --permission-config examples/permissions.json --workspace . "Update the docs."

Rule patterns use fnmatch-style matching. When a permission config is supplied, its rules become the active explicit rule set; unmatched requests still fall through to the runtime default decision policy and non-bypassable safety checks.

Subagent delegation

Mini Harness exposes an agent tool to the main model. Delegation is intentionally lightweight: a subagent gets its own AgentLoop, a specialized system prompt, a turn limit, and only the tools declared by its definition.

Two agents are registered by default:

Agent

Purpose

Default tools

explore_agent

Search and understand the codebase

read_file, list_files

plan_agent

Produce an implementation plan from gathered context

read_file, list_files

Because the default subagents only receive read tools, investigation and planning are separated from mutation by construction.

A custom agent can be added through AgentRegistry:

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

The delegation layer is deliberately not a distributed multi-agent framework: there is no implicit shared memory, autonomous agent swarm, or background scheduler. The goal is explicit, inspectable task delegation.

Tool execution and concurrency

Built-in local tools include:

read_file
list_files
write_file
edit_file

Additional tools are registered at runtime:

agent           specialized subagent delegation
load_skill      when a skill catalog is configured
sandbox_shell   when Docker sandboxing is explicitly enabled
mcp__...        tools discovered from configured MCP servers

Each tool can declare a ToolDescriptor describing its source and effect:

ToolDescriptor(
    source="extension",
    effect="write",
    destructive=True,
    path_argument="path",
)

That metadata is reused by permission evaluation, resource scheduling, tracing, and attribution instead of repeatedly guessing behavior from tool names.

Resource-aware scheduling

A model may return several tool calls in one response. Mini Harness resolves each call to logical ResourceAccess entries and uses asynchronous read/write locks:

same resource: read + read   → concurrent
same resource: read + write  → serialized
same resource: write + write → serialized
different files              → may run concurrently
unknown mutation             → global write lock

Tool observations are returned to the model in the original call order even when execution finishes out of order.

The maximum number of concurrent tools can be controlled with:

mini-oh --max-concurrent-tools 4 --workspace . "Inspect several files."

Safe editing

edit_file uses optimistic concurrency protection:

read_file records a SHA-256 snapshot for the current run;

edit_file requires that snapshot or an explicit expected_sha256;

a stale file is rejected instead of overwritten;

replacement is exact and ambiguous matches are rejected by default;

the final replacement uses a same-directory temporary file and os.replace.

This protects against accidental stale edits without pretending to provide a cross-process database transaction.

Hooks and verification gates

Hooks are trusted runtime extensions that the model cannot opt out of.

Supported lifecycle events:

Event

Runs when

Typical use

user_prompt_submit

before the prompt enters history

normalization, policy checks

pre_tool_use

before resource resolution and tool execution

argument rewriting, blocking

post_tool_use

after a tool finishes, before the result returns to the model

auditing, output filtering

stop

before a final answer becomes done

tests, lint, security verification

A stop hook can act as a verification gate. If the hook blocks completion, its failure output is returned to the agent as feedback so the model can fix the problem and attempt completion again.

Example:

mini-oh \
  --hooks-config examples/hooks-verification.json \
  --workspace . \
  "Implement the change and make the tests pass."

Command hooks execute with argv directly rather than through a shell, use the workspace as their working directory, and support explicit timeout and fail-open/fail-closed behavior.

Context compaction and artifacts

Before model calls, the runtime estimates context size. Once the configured threshold is exceeded, older conversation units can be replaced by a handoff summary while recent units remain verbatim.

Tool turns are treated atomically: an assistant tool-call message and its tool results are kept together so compaction does not create dangling protocol state.

The normal compaction path requests a no-tools summary from the configured model. If that secondary request fails or returns unusable output, a deterministic summary is used as the fallback.

Large tool outputs are offloaded to:

.mini-oh/artifacts/<run-id>/

The conversation keeps a head/tail preview and the artifact path instead of carrying the entire output inline.

Useful controls:

mini-oh \
  --context-threshold 12000 \
  --keep-recent 6 \
  --max-inline-output 8000 \
  "Work through a long repository task."

If the provider explicitly reports a context-window error, the runtime can force one compaction and retry the same logical model step once.

Trace and replay

Unless disabled, each run writes an append-only JSONL trace under:

.mini-oh/traces/<run-id>.jsonl

Trace events cover model requests/responses, streaming deltas, tool lifecycle, permission decisions, resource waits, hooks, compaction, MCP attribution, usage, estimated cost, and the final run state.

Sensitive field names and common credential patterns are redacted by default. Local trace files are created with owner-only permissions where supported.

Inspect traces with:

mini-oh trace list
mini-oh trace show <run-id>
mini-oh trace replay <run-id>

trace replay only renders the recorded timeline. It does not call the model again and does not execute tools again.

Pruning is dry-run by default:

mini-oh trace prune --older-than 30
mini-oh trace prune --max-runs 100 --apply

Sessions and resume

Conversation history can be persisted independently from traces. This lets an interrupted run be continued without pretending that an unfinished tool call completed successfully.

List sessions:

mini-oh sessions

Resume one:

mini-oh resume <session-id>

Or resume the latest session:

mini-oh resume --latest

continue is accepted as an alias for resume.

MCP and Skills

MCP

MCP servers can be configured through JSON and exposed to the same tool registry, permission, timeout, and trace boundaries as local tools.

mini-oh --mcp-config examples/mcp.json --workspace . "Inspect available MCP tools."

The implementation supports:

stdio MCP servers;

Streamable HTTP transport;

input and output schema validation;

structured content preservation;

conservative handling of readOnlyHint annotations;

OAuth flows including PKCE-oriented safety checks and local token persistence.

Remote tools are not implicitly trusted merely because they are reachable.

Skills

A skill directory follows the form:

skills/
└── my-skill/
    └── SKILL.md

When a skill catalog is configured, only lightweight metadata is exposed initially. The model loads the full skill body through load_skill when it is actually needed.

mini-oh --skills-dir ./skills --workspace . "Use the available project skills."

Docker sandbox shell

Shell execution is disabled by default.

Enable it explicitly:

docker pull alpine:3.20

mini-oh \
  --sandbox-shell \
  --sandbox-image alpine:3.20 \
  --workspace . \
  "Run the project checks."

The sandbox has no host-shell fallback. If Docker or the configured image is unavailable, startup fails instead of silently executing on the host.

Each invocation uses a disposable container with controls including:

--network none;

read-only container root filesystem;

writable workspace bind mount;

all Linux capabilities dropped;

no-new-privileges;

CPU, memory, PID, and tmpfs limits;

host UID/GID mapping where applicable;

masking of selected runtime credential locations.

This is a local development safety boundary, not a malicious multi-tenant sandbox.

Reliability controls

Mini Harness includes several small mechanisms that prevent common agent-runtime failure modes:

per-tool wall-clock timeout;

configurable model retry policy;

repeated identical tool-batch circuit breaker;

maximum model-step limit;

maximum concurrent tool limit;

fail-closed permission and reviewer failures;

typed provider errors for truncation and context overflow;

cancellation propagation;

one active run per AgentLoop instance;

structured ToolFailure(code, stage, retryable) metadata.

Example:

mini-oh \
  --tool-timeout 20 \
  --max-repeated-tool-batches 2 \
  --max-concurrent-tools 4 \
  --max-steps 16 \
  "Diagnose and repair the project."

Project layout

mini-harness/
├── src/mini_openharness/
│   ├── engine.py              # agent state machine and tool orchestration
│   ├── provider.py            # Responses + Chat-compatible providers
│   ├── tools.py               # registry, descriptors, locks, file tools
│   ├── permissions/           # safety, rules, engine, approval handlers
│   ├── multiagent.py          # subagent definitions, registry, delegation tool
│   ├── hooks.py               # lifecycle hook registry and executor
│   ├── compaction.py          # summaries and artifact offloading
│   ├── trace.py               # JSONL tracing, replay, pruning
│   ├── session.py             # persistent sessions and resume logic
│   ├── skills.py              # progressive skill loading
│   ├── mcp.py                 # MCP integration
│   ├── mcp_auth.py            # HTTP MCP OAuth support
│   ├── sandbox.py             # Docker-only shell execution
│   ├── models.py              # messages and tool-call data structures
│   └── cli.py                 # `mini-oh` command-line interface
├── tests/                     # runtime and protocol tests
├── examples/                  # permissions, hooks, MCP, reviewer demos
├── docs/                      # guided code-reading notes
├── TECHNICAL_DESIGN.md        # design rationale and implementation details
├── pyproject.toml
└── LICENSE

Development

Install development dependencies:

pip install -e '.[dev]'

Run the test suite:

pytest -q

Run Ruff:

ruff check .

The current repository contains tests covering the agent loop, tools, permissions, auto-review behavior, hooks, provider contracts, compaction, tracing, sessions, skills, MCP, sandboxing, and subagent delegation.

Design principles

Mini Harness is built around a few explicit rules:

Side effects need a control plane. Validation, permission checks, hooks, and resource scheduling happen before execution.

Unknown behavior fails conservatively. Unknown mutations receive restrictive defaults rather than optimistic concurrency or implicit permission.

The model receives recoverable failures. Tool errors are observations whenever possible, so the agent can change strategy instead of crashing the runtime.

Tool protocol state must remain valid. Tool calls and tool results stay paired through execution, compaction, interruption, and resume.

Observability must not replay side effects. Trace replay renders evidence; it does not re-run the agent.

Security claims should match the implementation. Docker isolation, permission policy, and OAuth boundaries are documented with their limitations.

Complexity should earn its place. The project prefers small, inspectable mechanisms over hidden framework behavior.

Current scope and limitations

The project intentionally does not try to provide every feature of a commercial coding-agent product.

Current boundaries include:

resource locking is process-local;

Docker remains part of the trusted computing base;

local OAuth token files are permission-protected but not OS-keychain encrypted;

hooks are trusted extensions, not sandboxed plugins;

default subagents are lightweight delegated loops rather than autonomous distributed workers;

there is no TUI, plugin marketplace, or remote multi-user execution service;

permission rules are intentionally simple glob-based rules rather than a full policy language.

For deeper implementation notes and trade-offs, see TECHNICAL_DESIGN.md.

License

Mini Harness is released under the MIT License.

<div align="center">

Small enough to understand. Complete enough to expose the real problems in agent runtimes.

</div>