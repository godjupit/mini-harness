"""Deterministic end-to-end evaluations for harness behavior."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from mini_openharness.compaction import ContextCompactor
from mini_openharness.engine import AgentEvent, AgentLoop
from mini_openharness.mcp import McpManager
from mini_openharness.memory import MemoryStore
from mini_openharness.models import Message, ModelReply, ToolCall
from mini_openharness.permissions import PermissionPolicy
from mini_openharness.provider import OpenAICompatibleProvider
from mini_openharness.skills import LoadSkillTool, SkillCatalog
from mini_openharness.tools import ToolRegistry, ToolResult, default_tools


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    detail: str
    steps: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    tools: tuple[str, ...] = ()
    max_steps: int = 12

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScriptedProvider:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = list(replies)
        self.requests: list[list[Message]] = []

    async def complete(self, messages, tools):
        del tools
        self.requests.append(list(messages))
        return self.replies.pop(0)


async def run_evals() -> list[EvalResult]:
    scenarios: list[tuple[str, Callable[[], Awaitable[EvalResult]]]] = [
        ("tool_recovery", _tool_recovery),
        ("skill_loading", _skill_loading),
        ("memory_recall", _memory_recall),
        ("mcp_tool_call", _mcp_tool_call),
        ("permission_block", _permission_block),
        ("context_compaction", _context_compaction),
        ("provider_retry_stream", _provider_retry_stream),
        ("loop_guard", _loop_guard),
    ]
    results = []
    for name, scenario in scenarios:
        started = time.monotonic()
        try:
            result = await scenario()
        except Exception as exc:
            result = EvalResult(name, False, f"{type(exc).__name__}: {exc}", 0, 0, 0, 0)
        duration = int((time.monotonic() - started) * 1000)
        results.append(
            EvalResult(
                result.name,
                result.passed,
                result.detail,
                result.steps,
                result.input_tokens,
                result.output_tokens,
                duration,
                result.tools,
                result.max_steps,
            )
        )
    return results


async def _collect(loop: AgentLoop, prompt: str) -> list[AgentEvent]:
    return [event async for event in loop.run(prompt)]


def _result(
    name: str, passed: bool, detail: str, loop: AgentLoop, events: list[AgentEvent]
) -> EvalResult:
    tools = tuple(str(event.data["name"]) for event in events if event.kind == "tool_start")
    done = next((event for event in reversed(events) if event.kind == "done"), None)
    return EvalResult(
        name,
        passed,
        detail,
        int(done.data.get("steps", 0)) if done else 0,
        loop.input_tokens,
        loop.output_tokens,
        0,
        tools,
        loop.max_steps,
    )


async def _tool_recovery() -> EvalResult:
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        provider = ScriptedProvider(
            [
                ModelReply(tool_calls=(ToolCall("bad", "missing_tool", {}),), input_tokens=3),
                ModelReply(
                    tool_calls=(ToolCall("good", "list_files", {"path": "."}),),
                    input_tokens=4,
                ),
                ModelReply(content="Recovered after the tool error.", output_tokens=5),
            ]
        )
        loop = AgentLoop(provider=provider, tools=default_tools(), workspace=workspace)
        events = await _collect(loop, "Recover from an invalid tool call")
        tool_ends = [event for event in events if event.kind == "tool_end"]
        passed = (
            tool_ends[0].data["is_error"]
            and not tool_ends[1].data["is_error"]
            and any(event.kind == "assistant" and "Recovered" in event.message for event in events)
        )
        return _result("tool_recovery", passed, "unknown tool became an observation", loop, events)


async def _skill_loading() -> EvalResult:
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        skill_path = workspace / "skills" / "review" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: review\ndescription: Review code\n---\n\nREAD BEFORE EDITING",
            encoding="utf-8",
        )
        catalog = SkillCatalog(workspace / "skills")
        tools = default_tools()
        tools.register(LoadSkillTool(catalog))
        provider = ScriptedProvider(
            [
                ModelReply(tool_calls=(ToolCall("skill", "load_skill", {"name": "review"}),)),
                ModelReply(content="Skill followed."),
            ]
        )
        loop = AgentLoop(provider=provider, tools=tools, workspace=workspace)
        events = await _collect(loop, "Use the review skill")
        loaded = any(
            event.kind == "tool_end" and "READ BEFORE EDITING" in event.message for event in events
        ) and any(
            event.kind == "assistant" and "Skill followed" in event.message for event in events
        )
        return _result("skill_loading", loaded, "skill body loaded on demand", loop, events)


async def _memory_recall() -> EvalResult:
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        store = MemoryStore(workspace / "memory.json")
        store.add("The preferred database is PostgreSQL", ["database"])
        prompt = "Which database is preferred?"
        provider = ScriptedProvider([ModelReply(content="PostgreSQL", output_tokens=1)])
        loop = AgentLoop(
            provider=provider,
            tools=default_tools(),
            workspace=workspace,
            system_prompt=store.prompt(prompt),
        )
        events = await _collect(loop, prompt)
        injected = "PostgreSQL" in provider.requests[0][0].content and any(
            event.kind == "assistant" and event.message == "PostgreSQL" for event in events
        )
        return _result("memory_recall", injected, "relevant durable memory injected", loop, events)


async def _mcp_tool_call() -> EvalResult:
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        server = workspace / "server.py"
        server.write_text(
            "from mcp.server.fastmcp import FastMCP\n"
            "mcp = FastMCP('eval')\n"
            "@mcp.tool()\n"
            "def echo(text: str) -> str:\n    return 'mcp:' + text\n"
            "if __name__ == '__main__':\n    mcp.run(transport='stdio')\n",
            encoding="utf-8",
        )
        config = workspace / "mcp.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "eval": {
                            "command": sys.executable,
                            "args": ["server.py"],
                            "cwd": str(workspace),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        tools = default_tools()
        manager = McpManager.from_file(config)
        try:
            names = await manager.connect_and_register(tools)
            provider = ScriptedProvider(
                [
                    ModelReply(tool_calls=(ToolCall("mcp", "mcp__eval__echo", {"text": "works"}),)),
                    ModelReply(content="MCP completed."),
                ]
            )
            loop = AgentLoop(
                provider=provider,
                tools=tools,
                workspace=workspace,
                permission_policy=PermissionPolicy(default_mutation="allow"),
            )
            events = await _collect(loop, "Call the MCP echo tool")
            called = (
                names == ["mcp__eval__echo"]
                and any(
                    event.kind == "tool_end" and "mcp:works" in event.message for event in events
                )
                and any(
                    event.kind == "assistant" and "MCP completed" in event.message
                    for event in events
                )
            )
            return _result("mcp_tool_call", called, "real stdio MCP tool executed", loop, events)
        finally:
            await manager.close()


async def _permission_block() -> EvalResult:
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        provider = ScriptedProvider(
            [
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "write",
                            "write_file",
                            {"path": "blocked.txt", "content": "must not exist"},
                        ),
                    )
                ),
                ModelReply(content="Write was blocked."),
            ]
        )
        loop = AgentLoop(
            provider=provider,
            tools=default_tools(),
            workspace=workspace,
            permission_policy=PermissionPolicy(default_mutation="deny"),
        )
        events = await _collect(loop, "Try a forbidden write")
        passed = (
            not (workspace / "blocked.txt").exists()
            and any(event.kind == "tool_end" and event.data["is_error"] for event in events)
            and any(event.kind == "assistant" and "blocked" in event.message for event in events)
        )
        return _result(
            "permission_block", passed, "denied write caused no side effect", loop, events
        )


async def _context_compaction() -> EvalResult:
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        history = [Message("system", "system")]
        for index in range(8):
            history.extend(
                [
                    Message("user", f"old request {index} " + "x" * 400),
                    Message("assistant", f"old answer {index} " + "y" * 400),
                ]
            )
        provider = ScriptedProvider([ModelReply(content="compacted")])
        loop = AgentLoop(
            provider=provider,
            tools=default_tools(),
            workspace=workspace,
            messages=history,
            compactor=ContextCompactor(threshold_tokens=100, keep_recent_units=3),
        )
        events = await _collect(loop, "continue")
        compacted = any(event.kind == "compact" for event in events) and any(
            event.kind == "assistant" and event.message == "compacted" for event in events
        )
        return _result("context_compaction", compacted, "old context summarized", loop, events)


async def _provider_retry_stream() -> EvalResult:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="slow down")
        body = (
            'data: {"choices":[{"delta":{"content":"streamed"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(handler),
        retry_base_delay=0,
    )
    try:
        with tempfile.TemporaryDirectory() as raw:
            loop = AgentLoop(provider=provider, tools=default_tools(), workspace=raw)
            events = await _collect(loop, "stream")
            passed = (
                attempts == 2
                and any(event.kind == "provider_retry" for event in events)
                and any(
                    event.kind == "assistant_delta" and event.message == "streamed"
                    for event in events
                )
                and any(
                    event.kind == "assistant" and event.message == "streamed" for event in events
                )
            )
            return _result(
                "provider_retry_stream",
                passed,
                "429 retried and SSE delta emitted",
                loop,
                events,
            )
    finally:
        await provider.close()


async def _loop_guard() -> EvalResult:
    calls = 0

    class CountingTool:
        name = "count"
        description = "Count actual executions."
        parameters = {"type": "object", "additionalProperties": False}
        read_only = True

        async def run(self, arguments, context):
            nonlocal calls
            del arguments, context
            calls += 1
            return ToolResult(str(calls))

    with tempfile.TemporaryDirectory() as raw:
        tools = ToolRegistry()
        tools.register(CountingTool())
        repeated = ModelReply(tool_calls=(ToolCall("same", "count", {}),))
        provider = ScriptedProvider(
            [repeated, repeated, repeated, ModelReply(content="Recovered from loop.")]
        )
        loop = AgentLoop(
            provider=provider,
            tools=tools,
            workspace=raw,
            max_repeated_tool_batches=2,
        )
        events = await _collect(loop, "Attempt a repeated tool loop")
        passed = (
            calls == 2
            and any(event.kind == "loop_guard" for event in events)
            and any(event.kind == "assistant" and "Recovered" in event.message for event in events)
        )
        return _result(
            "loop_guard",
            passed,
            "identical tool batch was blocked and returned as an observation",
            loop,
            events,
        )
