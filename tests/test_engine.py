from __future__ import annotations

import asyncio

import pytest

from mini_openharness.engine import AgentLoop, MaxStepsExceeded
from mini_openharness.models import Message, ModelReply, ToolCall
from mini_openharness.provider import (
    ProviderAuthenticationError,
    ProviderComplete,
    ProviderRetry,
    ProviderTextDelta,
)
from mini_openharness.compaction import ArtifactStore
from mini_openharness.tools import ToolContext, ToolRegistry, ToolResult, default_tools
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


def test_resumed_session_refreshes_runtime_context(tmp_path):
    loop = AgentLoop(
        provider=ScriptedProvider([ModelReply(content="done")]),
        tools=default_tools(),
        workspace=tmp_path,
        system_prompt="new skills and relevant memories",
        messages=[Message("system", "old context"), Message("user", "earlier turn")],
    )

    assert loop.messages[0].content == "new skills and relevant memories"


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
