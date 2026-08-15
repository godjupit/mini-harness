# Mini Harness

**English** | [简体中文](README_ZH.md)

> A compact, safety-aware runtime for building and studying coding agents.

Agent loop · permission engine · auto-review · subagents · hooks · MCP · skills · context compaction · trace/replay · bwrap sandbox

Mini Harness is a small but complete coding-agent runtime. It focuses on the control plane that is usually hidden behind a product UI: model/tool orchestration, permission decisions, safe execution boundaries, lifecycle hooks, context management, observability, resumable sessions, and delegated subagents.

Status: active development. Suitable for learning, experimentation, interviews, and local prototyping — not a hardened multi-tenant platform.

## Why Mini Harness?

A useful coding agent is more than a loop that repeatedly calls an LLM. Once tools can read files, modify a workspace, call remote services, or run shell commands, the runtime must answer:

- Which actions are allowed, denied, or require review?
- Can independent tool calls run concurrently without racing on the same files?
- What happens when a tool times out or the model repeats the same action forever?
- How do large tool outputs and long conversations fit inside the context window?
- How can a run be inspected later without replaying its side effects?

Mini Harness implements these concerns as explicit runtime components rather than hiding them inside one monolithic agent function.

## Architecture

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
                            │             (JSONL audit
                            │              + safe replay)
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
          human approval  or  auto-review agent
                       │
                       ▼
                 actual tool run
                       │
                       ▼
                 post-tool hook ──► observation ──► AgentLoop
```

The built-in `agent` tool can delegate read-only investigation or planning to specialized subagent `AgentLoop`s with a restricted tool subset.

## Core capabilities

| Area | What the runtime provides |
| --- | --- |
| Agent loop | Model → tools → observations → model, with typed events, cancellation, max-step limits, and strict tool-call/result pairing |
| Permission engine | Ordered `safety → deny → ask → allow → default` evaluation with workspace containment and conservative shell checks |
| Auto-review | ASK decisions can go to an independent tool-less reviewer model; failures and ambiguous responses fail closed |
| Subagents | Built-in Explore and Plan agents run their own `AgentLoop` with restricted tools; custom definitions can be registered |
| Tool system | JSON Schema validation, immutable security metadata, structured failures, timeouts, source/effect attribution |
| Concurrency | Effect-aware resource locking: independent work runs concurrently, conflicting file access is serialized |
| Hooks | `user_prompt_submit`, `pre_tool_use`, `post_tool_use`, `stop` lifecycle hooks with priority, matching, and fail-open/fail-closed behavior |
| Verification gate | A stop hook can block completion and return test/lint feedback so the model repairs before finishing |
| Context | Atomic tool-turn compaction, handoff summaries with deterministic fallback, reactive context recovery, artifact offloading |
| Tracing | Append-only JSONL traces with secret redaction, timing, permission/tool/provider events, cost, pruning, side-effect-free replay |
| Sessions | Persistent conversations with interruption detection and resume |
| MCP | stdio and Streamable HTTP transports, schema validation, conservative annotation trust, OAuth |
| Sandbox | Bubblewrap host shell: real host environment, read-only filesystem, writable workspace, fresh `/tmp` |

## Requirements

- Python 3.10+
- An OpenAI-compatible API key for real model runs
- `bwrap` (bubblewrap) on Linux if `sandbox_shell` is enabled

## Installation

```bash
git clone <your-repository-url>
cd mini-harness

python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Create a local environment file and fill in your provider configuration:

```bash
cp .env.example .env
```

```dotenv
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_MODE=responses
```

The CLI loads `.env` from the current directory without overriding variables already present in the shell.

## Quick start

Offline demo (no API key, exercises the real runtime path):

```bash
wqb --demo --workspace . "Inspect this project and summarize its architecture."
```

Real model run:

```bash
wqb --workspace . "Inspect the repository and explain the agent runtime."
```

The default provider path uses the Responses API. For a Chat Completions-compatible endpoint:

```bash
wqb \
  --api-mode chat \
  --base-url https://compatible.example/v1 \
  --workspace . \
  "Analyze the codebase."
```

## Permission model

Every tool execution is converted into a `PermissionRequest` carrying the tool's declared source, effect, destructive flag, path, and/or shell command. The decision order is:

1. Safety boundary
2. Explicit DENY rules
3. Explicit ASK rules
4. Explicit ALLOW rules
5. Runtime default

Runtime defaults by tool effect:

| Effect | Default |
| --- | --- |
| `read` / `compute` | ALLOW |
| `write` / `remote` / `unknown` | ASK |

Safety checks run before configurable rules:

- Workspace path escape → hard DENY
- Multi-line shell input → hard DENY
- Shell syntax that cannot be statically verified → ASK

### Human approval (DEFAULT mode)

ASK decisions ask for approval in an interactive terminal. Without an approval callback, ASK fails closed.

```bash
wqb --workspace . "Create docs/design.md"
```

### Auto-review (AUTO_REVIEW mode)

`--auto-review` sends ASK decisions to an independent reviewer model call instead of a human:

```bash
wqb --auto-review --workspace . "Refactor the parser and update the tests."
```

The reviewer receives the tool, effect, path/command, workspace, arguments, and the permission reason, and can only return approve/reject for that specific ASK. Invariants:

- An explicit DENY is never sent to the reviewer and cannot be overridden
- Safety DENY remains final
- Reviewer errors, timeouts, or invalid responses reject the action
- The reviewer is invoked without tools, so it cannot execute the operation itself

### Rules file

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
wqb --permission-config examples/permissions.json --workspace . "Update the docs."
```

Rule patterns use `fnmatch`-style matching. Unmatched requests still fall through to the runtime default and non-bypassable safety checks.

## Subagent delegation

Mini Harness exposes an `agent` tool to the main model. A subagent gets its own `AgentLoop`, a specialized system prompt, a turn limit, and only the tools declared by its definition.

Two agents are registered by default:

| Agent | Purpose | Default tools |
| --- | --- | --- |
| `explore_agent` | Search and understand the codebase | `read_file`, `list_dir`, `find_files` |
| `plan_agent` | Produce an implementation plan from gathered context | `read_file`, `list_dir`, `find_files` |

Default subagents only receive read tools, so investigation and planning are separated from mutation by construction. Add a custom agent through `AgentRegistry`:

```python
from mini_openharness.multiagent import AgentDefinition, default_agents

agents = default_agents()
agents.register(
    AgentDefinition(
        type="reviewer",
        description="reviews implementation changes",
        system_prompt="You are a focused code review agent.",
        max_turns=20,
        tools=("read_file", "list_dir", "find_files"),
    )
)
```

Delegation is explicit and inspectable — no implicit shared memory, autonomous swarm, or background scheduler.

## Tool execution and concurrency

Built-in local tools:

| Tool | Purpose |
| --- | --- |
| `read_file` | Read a UTF-8 text file inside the workspace |
| `list_dir` | List the entries inside a directory (one level, `/` marks directories) |
| `find_files` | Recursively search for files by name pattern (`cli.py`, `*.py`) |
| `write_file` | Write a UTF-8 text file |
| `edit_file` | Snapshot-checked in-place edit |

Runtime-registered tools:

| Tool | When |
| --- | --- |
| `agent` | Subagent delegation |
| `load_skill` | When a skill catalog is configured |
| `sandbox_shell` | When the bwrap sandbox shell is enabled |
| `mcp__...` | Tools discovered from configured MCP servers |

Each tool declares a `ToolDescriptor` used by permission evaluation, resource scheduling, tracing, and attribution:

```python
ToolDescriptor(
    source="extension",
    effect="write",
    destructive=True,
    path_argument="path",
)
```

### Resource-aware scheduling

A model may return several tool calls in one response. Each call resolves to logical `ResourceAccess` entries and is scheduled with async read/write locks:

| Access pattern | Behavior |
| --- | --- |
| Same resource: read + read | Concurrent |
| Same resource: read + write | Serialized |
| Same resource: write + write | Serialized |
| Different files | May run concurrently |
| Unknown mutation | Global write lock |

Tool observations return in original call order even when execution finishes out of order:

```bash
wqb --max-concurrent-tools 4 --workspace . "Inspect several files."
```

### Safe editing

`edit_file` uses optimistic concurrency: `read_file` records a SHA-256 snapshot for the run, `edit_file` requires that snapshot or an explicit `expected_sha256`, a stale file is rejected instead of overwritten, ambiguous matches are rejected by default, and the final replacement uses a same-directory temporary file with `os.replace`.

## Hooks and verification gates

Hooks are trusted runtime extensions the model cannot opt out of.

| Event | Runs when | Typical use |
| --- | --- | --- |
| `user_prompt_submit` | Before the prompt enters history | Normalization, policy checks |
| `pre_tool_use` | Before resource resolution and execution | Argument rewriting, blocking |
| `post_tool_use` | After a tool finishes | Auditing, output filtering |
| `stop` | Before a final answer becomes done | Tests, lint, security verification |

A stop hook can act as a verification gate: if it blocks completion, its output returns to the model as feedback so the agent can fix the problem and try again.

```bash
wqb \
  --hooks-config examples/hooks-verification.json \
  --workspace . \
  "Implement the change and make the tests pass."
```

Command hooks execute with `argv` directly (no shell), use the workspace as the working directory, and support explicit timeout and fail-open/fail-closed behavior.

## Context compaction and artifacts

- Before model calls, the runtime estimates context size; once the threshold is exceeded, older conversation units are replaced by a handoff summary while recent units stay verbatim
- Tool turns are treated atomically so compaction never creates dangling protocol state
- The normal path requests a no-tools summary from the configured model; on failure a deterministic summary is used
- Large tool outputs are offloaded to `.mini-oh/artifacts/<run-id>/`, keeping only a head/tail preview inline

```bash
wqb \
  --context-threshold 12000 \
  --keep-recent 6 \
  --max-inline-output 8000 \
  "Work through a long repository task."
```

If the provider reports a context-window error, the runtime can force one compaction and retry the same logical model step.

## Trace and replay

Each run writes an append-only JSONL trace under `.mini-oh/traces/<run-id>.jsonl`, covering model requests/responses, streaming deltas, tool lifecycle, permission decisions, resource waits, hooks, compaction, MCP attribution, usage, cost, and final run state. Sensitive fields are redacted by default.

```bash
wqb trace list
wqb trace show <run-id>
wqb trace replay <run-id>
wqb trace prune --older-than 30          # dry-run
wqb trace prune --max-runs 100 --apply
```

`trace replay` only renders the recorded timeline — it does not call the model or execute tools again.

## Sessions and resume

```bash
wqb sessions
wqb resume <session-id>
wqb resume --latest
```

`continue` is accepted as an alias for `resume`.

## MCP and Skills

### MCP

```bash
wqb --mcp-config examples/mcp.json --workspace . "Inspect available MCP tools."
```

Supports stdio and Streamable HTTP transports, input/output schema validation, structured content preservation, conservative `readOnlyHint` handling, and OAuth flows with local token persistence. Remote tools are not implicitly trusted merely because they are reachable.

### Skills

```text
skills/
└── my-skill/
    └── SKILL.md
```

Only lightweight metadata is exposed initially; the model loads the full skill body through `load_skill` when needed.

```bash
wqb --skills-dir ./skills --workspace . "Use the available project skills."
```

## Sandbox shell (bubblewrap)

`sandbox_shell` runs host bash inside a bubblewrap sandbox. The host environment
(python, pytest, git, node, `.venv`, PATH) is available directly; only the
workspace is writable, the rest of the filesystem is read-only, and `/tmp` is a
fresh temporary directory. The working directory persists across calls.

```bash
wqb --workspace . "Run the project checks."
```

There is no unrestricted fallback: if `bwrap` is not installed, the shell tool
is unavailable instead of running on the host without a sandbox.

This is a local development safety boundary, not a malicious multi-tenant sandbox.

## Reliability controls

- Per-tool wall-clock timeout
- Configurable model retry policy
- Repeated identical tool-batch circuit breaker
- Maximum model-step and concurrent-tool limits
- Fail-closed permission and reviewer failures
- Typed provider errors for truncation and context overflow
- Cancellation propagation; one active run per `AgentLoop`
- Structured `ToolFailure(code, stage, retryable)` metadata

```bash
wqb \
  --tool-timeout 20 \
  --max-repeated-tool-batches 2 \
  --max-concurrent-tools 4 \
  --max-steps 16 \
  "Diagnose and repair the project."
```

## Project layout

```text
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
│   ├── sandbox.py             # bwrap-sandboxed host shell
│   ├── models.py              # messages and tool-call data structures
│   └── cli.py                 # `wqb` command-line interface
├── tests/                     # runtime and protocol tests
├── examples/                  # permissions, hooks, MCP, reviewer demos
├── docs/                      # guided code-reading notes
├── TECHNICAL_DESIGN.md        # design rationale and implementation details
├── pyproject.toml
└── LICENSE
```

## Development

```bash
pip install -e '.[dev]'
pytest -q
ruff check .
```

Tests cover the agent loop, tools, permissions, auto-review, hooks, providers, compaction, tracing, sessions, skills, MCP, sandboxing, and subagent delegation.

## Design principles

- Side effects need a control plane: validation, permissions, hooks, and resource scheduling happen before execution
- Unknown behavior fails conservatively
- The model receives recoverable failures: tool errors are observations whenever possible
- Tool protocol state stays valid: calls and results remain paired through execution, compaction, interruption, and resume
- Observability must not replay side effects
- Security claims match the implementation: bwrap isolation, permission policy, and OAuth boundaries are documented with their limits
- Complexity earns its place: small, inspectable mechanisms over hidden framework behavior

## Limitations

- Resource locking is process-local
- The host filesystem read-only bind remains part of the trusted computing base
- Local OAuth token files are permission-protected but not OS-keychain encrypted
- Hooks are trusted extensions, not sandboxed plugins
- Default subagents are lightweight delegated loops, not autonomous distributed workers
- No TUI, plugin marketplace, or remote multi-user execution service
- Permission rules are intentionally simple glob-based rules, not a full policy language

For deeper implementation notes, see [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md).

## License

Mini Harness is released under the [MIT License](LICENSE).

---

*Small enough to understand. Complete enough to expose the real problems in agent runtimes.*
