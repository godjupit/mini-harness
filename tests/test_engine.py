from __future__ import annotations

import asyncio

import pytest

from mini_openharness.engine import AgentLoop, MaxStepsExceeded, RunAlreadyActiveError
from mini_openharness.models import Message, ModelReply, ToolCall
from mini_openharness.provider import (
    ProviderAuthenticationError,
    ProviderComplete,
    ProviderContextWindowError,
    ProviderRetry,
    ProviderTextDelta,
)
from mini_openharness.compaction import ArtifactStore, ContextCompactor
from mini_openharness.tools import (
    ResourceAccess,
    ToolDescriptor,
    ToolContext,
    ToolRegistry,
    ToolResult,
    default_tools,
)
from mini_openharness.trace import TraceStore, TraceWriter


class ScriptedProvider:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append((list(messages), tools))
        return self.replies.pop(0)


def collect(loop: AgentLoop, prompt: str):
    async def run():
        return [event async for event in loop.run(prompt)]

    return asyncio.run(run())


def test_same_agent_loop_rejects_overlapping_runs_and_recovers_after_close(tmp_path):
    provider = ScriptedProvider([ModelReply(content="done")])
    loop = AgentLoop(provider=provider, tools=default_tools(), workspace=tmp_path)

    async def run():
        first = loop.run("first")
        assert (await anext(first)).kind == "model_start"

        second = loop.run("second")
        with pytest.raises(RunAlreadyActiveError):
            await anext(second)

        await first.aclose()
        return [event async for event in loop.run("third")]

    events = asyncio.run(run())

    assert events[-1].kind == "done"
    assert [message.content for message in loop.messages if message.role == "user"] == [
        "first",
        "third",
    ]


def test_run_guard_is_released_after_exception(tmp_path):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("x", "list_files", {}),)),
            ModelReply(content="recovered"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        max_steps=1,
    )

    with pytest.raises(MaxStepsExceeded):
        collect(loop, "first")

    events = collect(loop, "second")

    assert events[-1].kind == "done"


def test_cancel_without_active_run_is_a_noop(tmp_path):
    loop = AgentLoop(
        provider=ScriptedProvider([ModelReply(content="done")]),
        tools=default_tools(),
        workspace=tmp_path,
    )

    loop.cancel()

    assert collect(loop, "go")[-1].kind == "done"


def test_different_agent_loops_can_run_concurrently(tmp_path):
    active = 0
    max_active = 0

    class ConcurrentProvider:
        async def complete(self, messages, tools):
            nonlocal active, max_active
            del messages, tools
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ModelReply(content="done")

    first = AgentLoop(provider=ConcurrentProvider(), tools=default_tools(), workspace=tmp_path)
    second = AgentLoop(provider=ConcurrentProvider(), tools=default_tools(), workspace=tmp_path)

    async def run():
        async def consume(loop):
            return [event async for event in loop.run("go")]

        return await asyncio.gather(consume(first), consume(second))

    results = asyncio.run(run())

    assert max_active == 2
    assert all(events[-1].kind == "done" for events in results)


def test_model_tool_model_loop(tmp_path):
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("1", "read_file", {"path": "hello.txt"}),)),
            ModelReply(content="The file says hello."),
        ]
    )
    loop = AgentLoop(provider=provider, tools=default_tools(), workspace=tmp_path)

    events = collect(loop, "Read the file")

    assert [event.kind for event in events] == [
        "model_start",
        "tool_start",
        "tool_end",
        "model_start",
        "assistant",
        "done",
    ]
    assert provider.requests[1][0][-1].content == "hello"


def test_agent_loop_read_then_edit_uses_per_run_snapshot(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("timeout = 10\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("read", "read_file", {"path": "app.py"}),)),
            ModelReply(
                tool_calls=(
                    ToolCall(
                        "edit",
                        "edit_file",
                        {"path": "app.py", "old_text": "10", "new_text": "30"},
                    ),
                )
            ),
            ModelReply(content="done"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        allow_write=True,
    )

    events = collect(loop, "update timeout")

    assert events[-1].kind == "done"
    assert path.read_text(encoding="utf-8") == "timeout = 30\n"


def test_file_snapshot_does_not_leak_into_next_user_run(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("read", "read_file", {"path": "app.py"}),)),
            ModelReply(content="first done"),
            ModelReply(
                tool_calls=(
                    ToolCall(
                        "edit",
                        "edit_file",
                        {"path": "app.py", "old_text": "old", "new_text": "new"},
                    ),
                )
            ),
            ModelReply(content="second done"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        allow_write=True,
    )

    collect(loop, "read only")
    second = collect(loop, "edit without reading this turn")

    edit_result = next(event for event in second if event.kind == "tool_end")
    assert edit_result.data["failure"]["code"] == "file_not_read"
    assert path.read_text(encoding="utf-8") == "old"


def test_parallel_tool_calls_preserve_result_order(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=(
                    ToolCall("a", "read_file", {"path": "a.txt"}),
                    ToolCall("b", "read_file", {"path": "b.txt"}),
                )
            ),
            ModelReply(content="done"),
        ]
    )
    loop = AgentLoop(provider=provider, tools=default_tools(), workspace=tmp_path)

    collect(loop, "read both")

    tool_messages = [message for message in loop.messages if message.role == "tool"]
    assert [(message.tool_call_id, message.content) for message in tool_messages] == [
        ("a", "A"),
        ("b", "B"),
    ]


def test_mutating_tool_batch_is_serialized(tmp_path):
    active = 0
    max_active = 0

    class MutationTool:
        name = "mutate"
        description = "mutate shared state"
        parameters = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        read_only = False

        async def run(self, arguments, context):
            nonlocal active, max_active
            del context
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(arguments["value"])

    tools = ToolRegistry()
    tools.register(MutationTool())
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=(
                    ToolCall("a", "mutate", {"value": "A"}),
                    ToolCall("b", "mutate", {"value": "B"}),
                )
            ),
            ModelReply(content="done"),
        ]
    )
    tracer = TraceWriter(tmp_path / "traces", run_id="locks")
    loop = AgentLoop(
        provider=provider,
        tools=tools,
        workspace=tmp_path,
        allow_write=True,
        tracer=tracer,
    )

    collect(loop, "mutate")

    assert max_active == 1
    trace = list(TraceStore(tmp_path / "traces").read("locks"))
    acquired = [event for event in trace if event.kind == "resource_acquired"]
    released = [event for event in trace if event.kind == "resource_released"]
    assert len(acquired) == len(released) == 2
    assert max(event.data["waited_ms"] for event in acquired) >= 1
    assert all(event.data["resources"][0]["key"] == "*" for event in acquired)
    assert all(event.data["held_ms"] >= 1 for event in released)


def test_non_conflicting_mutations_run_in_parallel(tmp_path):
    active = 0
    max_active = 0

    class PathMutationTool:
        name = "path_mutate"
        description = "mutate one path"
        parameters = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
        read_only = False

        def resources(self, arguments, context):
            return (ResourceAccess(f"fs:{context.workspace / arguments['path']}", "write"),)

        async def run(self, arguments, context):
            nonlocal active, max_active
            del arguments, context
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ToolResult("done")

    tools = ToolRegistry()
    tools.register(PathMutationTool())
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=(
                    ToolCall("a", "path_mutate", {"path": "a.txt"}),
                    ToolCall("b", "path_mutate", {"path": "b.txt"}),
                )
            ),
            ModelReply(content="done"),
        ]
    )
    loop = AgentLoop(provider=provider, tools=tools, workspace=tmp_path, allow_write=True)

    collect(loop, "mutate separate files")

    assert max_active == 2


def test_tool_batch_respects_max_concurrency_and_traces_slots(tmp_path):
    active = 0
    max_active = 0

    class BoundedTool:
        name = "bounded"
        description = "exercise bounded concurrency"
        parameters = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
        descriptor = ToolDescriptor(effect="write", path_argument="path")

        def resources(self, arguments, context):
            return (ResourceAccess(f"fs:{context.workspace / arguments['path']}", "write"),)

        async def run(self, arguments, context):
            nonlocal active, max_active
            del arguments, context
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ToolResult("done")

    tools = ToolRegistry()
    tools.register(BoundedTool())
    calls = tuple(ToolCall(str(index), "bounded", {"path": f"{index}.txt"}) for index in range(5))
    tracer = TraceWriter(tmp_path / "traces", run_id="bounded")
    loop = AgentLoop(
        provider=ScriptedProvider([ModelReply(tool_calls=calls), ModelReply(content="done")]),
        tools=tools,
        workspace=tmp_path,
        allow_write=True,
        tracer=tracer,
        max_concurrent_tools=2,
    )

    collect(loop, "bounded batch")

    assert max_active == 2
    trace = list(TraceStore(tmp_path / "traces").read("bounded"))
    slots = [event for event in trace if event.kind == "tool_slot_acquired"]
    assert len(slots) == 5
    assert all(event.data["max_concurrent_tools"] == 2 for event in slots)
    assert max(event.data["waited_ms"] for event in slots) >= 10


def test_max_concurrent_tools_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="max_concurrent_tools"):
        AgentLoop(
            provider=ScriptedProvider([ModelReply(content="done")]),
            tools=default_tools(),
            workspace=tmp_path,
            max_concurrent_tools=0,
        )


def test_repeated_tool_batch_is_blocked_but_model_can_recover(tmp_path):
    calls = 0

    class CountingTool:
        name = "count"
        description = "count executions"
        parameters = {"type": "object", "additionalProperties": False}
        read_only = True

        async def run(self, arguments, context):
            nonlocal calls
            del arguments, context
            calls += 1
            return ToolResult(str(calls))

    tools = ToolRegistry()
    tools.register(CountingTool())
    repeated = ModelReply(tool_calls=(ToolCall("same", "count", {}),))
    provider = ScriptedProvider([repeated, repeated, repeated, ModelReply(content="recovered")])
    loop = AgentLoop(
        provider=provider,
        tools=tools,
        workspace=tmp_path,
        max_repeated_tool_batches=2,
    )

    events = collect(loop, "do not loop")

    assert calls == 2
    assert any(event.kind == "loop_guard" for event in events)
    assert "Repeated identical" in provider.requests[3][0][-1].content


def test_loop_guard_counter_resets_for_each_user_run(tmp_path):
    tools = default_tools()
    call = ModelReply(tool_calls=(ToolCall("same", "list_files", {}),))
    provider = ScriptedProvider(
        [call, ModelReply(content="first"), call, ModelReply(content="second")]
    )
    loop = AgentLoop(
        provider=provider,
        tools=tools,
        workspace=tmp_path,
        max_repeated_tool_batches=1,
    )

    first = collect(loop, "first turn")
    second = collect(loop, "second turn")

    assert not any(event.kind == "loop_guard" for event in first + second)


def test_unknown_tool_becomes_observation(tmp_path):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("x", "missing", {}),)),
            ModelReply(content="recovered"),
        ]
    )
    loop = AgentLoop(provider=provider, tools=default_tools(), workspace=tmp_path)

    collect(loop, "go")

    assert "Unknown tool" in provider.requests[1][0][-1].content


def test_max_steps_is_a_hard_guard(tmp_path):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("x", "list_files", {}),)),
        ]
    )
    loop = AgentLoop(provider=provider, tools=default_tools(), workspace=tmp_path, max_steps=1)

    with pytest.raises(MaxStepsExceeded):
        collect(loop, "loop forever")


def test_preloaded_history_refreshes_runtime_context(tmp_path):
    loop = AgentLoop(
        provider=ScriptedProvider([ModelReply(content="done")]),
        tools=default_tools(),
        workspace=tmp_path,
        system_prompt="new runtime context",
        messages=[Message("system", "old context"), Message("user", "earlier turn")],
    )

    assert loop.messages[0].content == "new runtime context"


def test_agent_loop_trace_covers_model_tool_permission_usage_and_finish(tmp_path):
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=(ToolCall("1", "read_file", {"path": "hello.txt"}),),
                input_tokens=10,
                output_tokens=2,
            ),
            ModelReply(content="done", input_tokens=5, output_tokens=1),
        ]
    )
    tracer = TraceWriter(tmp_path / "traces", run_id="engine", metadata={"prompt": "read"})
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        tracer=tracer,
        input_cost_per_million=1.0,
        output_cost_per_million=2.0,
    )

    collect(loop, "read")

    events = list(TraceStore(tmp_path / "traces").read("engine"))
    kinds = [event.kind for event in events]
    assert kinds.count("model_request") == 2
    assert kinds.count("model_response") == 2
    assert "permission_decision" in kinds
    assert "tool_start" in kinds and "tool_end" in kinds
    assert events[-1].kind == "run_end"
    assert events[-1].data["status"] == "completed"
    assert events[-1].data["estimated_cost"] == 0.000021


def test_mcp_server_is_attributed_on_tool_start_and_end(tmp_path):
    class McpEchoTool:
        name = "mcp__remote__echo"
        description = "echo"
        parameters = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        read_only = True

        async def run(self, arguments, context):
            del context
            return ToolResult(arguments["text"])

    tools = ToolRegistry()
    tools.register(McpEchoTool())
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("mcp-1", "mcp__remote__echo", {"text": "ok"}),)),
            ModelReply(content="done"),
        ]
    )
    tracer = TraceWriter(tmp_path / "traces", run_id="mcp-attribution")
    loop = AgentLoop(provider=provider, tools=tools, workspace=tmp_path, tracer=tracer)

    collect(loop, "call mcp")

    trace = list(TraceStore(tmp_path / "traces").read("mcp-attribution"))
    tool_events = [event for event in trace if event.kind in {"tool_start", "tool_end"}]
    assert [event.data["mcp_server"] for event in tool_events] == ["remote", "remote"]


def test_tool_failure_is_exposed_on_agent_and_trace_events(tmp_path):
    class FailingTool:
        name = "failing"
        description = "fail"
        parameters = {"type": "object", "additionalProperties": False}
        descriptor = ToolDescriptor(source="extension", effect="read")

        async def run(self, arguments, context):
            del arguments, context
            raise RuntimeError("boom")

    tools = ToolRegistry()
    tools.register(FailingTool())
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("failed", "failing", {}),)),
            ModelReply(content="recovered"),
        ]
    )
    tracer = TraceWriter(tmp_path / "traces", run_id="structured-failure")
    loop = AgentLoop(provider=provider, tools=tools, workspace=tmp_path, tracer=tracer)

    events = collect(loop, "fail safely")

    tool_end = next(event for event in events if event.kind == "tool_end")
    assert tool_end.data["failure"]["code"] == "tool_error"
    assert tool_end.data["failure"]["stage"] == "execute"
    trace_end = next(
        event
        for event in TraceStore(tmp_path / "traces").read("structured-failure")
        if event.kind == "tool_end"
    )
    assert trace_end.data["failure"] == tool_end.data["failure"]


def test_agent_loop_offloads_large_tool_output_before_next_model_call(tmp_path):
    payload = "x" * 2_000
    (tmp_path / "large.txt").write_text(payload, encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("large", "read_file", {"path": "large.txt"}),)),
            ModelReply(content="done"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        artifact_store=ArtifactStore(tmp_path / "artifacts", max_inline_chars=100),
    )

    collect(loop, "read large")

    observation = provider.requests[1][0][-1].content
    artifact = next((tmp_path / "artifacts" / "untraced").glob("*.txt"))
    assert "offloaded" in observation
    assert artifact.read_text(encoding="utf-8") == payload


def test_cancel_stops_in_flight_tool_task(tmp_path):
    cancelled = False

    class SlowTool:
        name = "slow"
        description = "wait"
        parameters = {"type": "object", "additionalProperties": False}
        read_only = True

        async def run(self, arguments: dict, context: ToolContext) -> ToolResult:
            nonlocal cancelled
            del arguments, context
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return ToolResult("late")

    tools = ToolRegistry()
    tools.register(SlowTool())
    loop = AgentLoop(
        provider=ScriptedProvider([ModelReply(tool_calls=(ToolCall("slow", "slow", {}),))]),
        tools=tools,
        workspace=tmp_path,
    )

    async def run():
        events = []
        async for event in loop.run("wait"):
            events.append(event)
            if event.kind == "tool_start":
                loop.cancel()
        return events

    events = asyncio.run(run())
    assert events[-1].kind == "cancelled"
    assert cancelled


def test_stream_deltas_retries_and_provider_failure_are_traced(tmp_path):
    class StreamingProvider:
        async def stream(self, messages, tools, *, cancel_event=None):
            del messages, tools, cancel_event
            yield ProviderRetry(1, 0, "rate limited")
            yield ProviderTextDelta("hello")
            yield ProviderComplete(ModelReply(content="hello", input_tokens=2, output_tokens=1))

    successful_trace = TraceWriter(tmp_path / "traces", run_id="stream")
    successful = AgentLoop(
        provider=StreamingProvider(),
        tools=default_tools(),
        workspace=tmp_path,
        tracer=successful_trace,
    )
    collect(successful, "stream")
    successful_kinds = [event.kind for event in TraceStore(tmp_path / "traces").read("stream")]
    assert "provider_retry" in successful_kinds
    assert "assistant_delta" in successful_kinds

    class FailingProvider:
        async def complete(self, messages, tools):
            del messages, tools
            raise ProviderAuthenticationError("bad key")

    failed_trace = TraceWriter(tmp_path / "traces", run_id="failed")
    failed = AgentLoop(
        provider=FailingProvider(),
        tools=default_tools(),
        workspace=tmp_path,
        tracer=failed_trace,
    )
    events = collect(failed, "fail")
    trace_events = list(TraceStore(tmp_path / "traces").read("failed"))
    assert events[-1].kind == "error"
    assert trace_events[-1].data["status"] == "failed"


def test_context_error_forces_one_compaction_and_retries_same_model_step(tmp_path):
    class ContextLimitedProvider:
        def __init__(self):
            self.requests = []

        async def complete(self, messages, tools):
            self.requests.append(list(messages))
            if not tools:
                return ModelReply(content="Earlier work: old requests were explored.")
            if len(self.requests) == 1:
                raise ProviderContextWindowError("maximum context length exceeded")
            return ModelReply(content="recovered")

    history = [Message("system", "system")]
    for index in range(6):
        history.extend(
            [
                Message("user", f"old request {index}"),
                Message("assistant", f"old answer {index}"),
            ]
        )
    provider = ContextLimitedProvider()
    tracer = TraceWriter(tmp_path / "traces", run_id="reactive-compact")
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        messages=history,
        compactor=ContextCompactor(threshold_tokens=1_000_000, keep_recent_units=2),
        tracer=tracer,
    )

    events = collect(loop, "continue")

    compact = next(event for event in events if event.kind == "compact")
    done = next(event for event in events if event.kind == "done")
    assert compact.data["trigger"] == "reactive"
    assert len(provider.requests) == 3
    assert len(provider.requests[2]) < len(provider.requests[0])
    assert done.data["steps"] == 1
    trace = list(TraceStore(tmp_path / "traces").read("reactive-compact"))
    assert any(event.kind == "context_retry" for event in trace)


def test_context_error_without_compactable_history_fails_without_looping(tmp_path):
    class AlwaysTooLarge:
        calls = 0

        async def complete(self, messages, tools):
            del messages, tools
            self.calls += 1
            raise ProviderContextWindowError("prompt is too long")

    provider = AlwaysTooLarge()
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        compactor=ContextCompactor(threshold_tokens=1_000_000),
    )

    events = collect(loop, "short prompt")

    assert provider.calls == 1
    assert events[-1].kind == "error"
