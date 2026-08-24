"""The observable, permissioned, tool-aware agent loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from mini_openharness.compaction import ArtifactStore, ContextCompactor
from mini_openharness.errors.engine import MaxStepsExceeded, RunAlreadyActiveError
from mini_openharness.errors.provider import (
    ProviderCancelledError,
    ProviderContextWindowError,
    ProviderError,
)
from mini_openharness.hooks import HookEvent, HookExecutor, HookRegistry
from mini_openharness.models import Message, ModelReply, ToolCall
from mini_openharness.permissions import ApprovalHandler, PermissionEngine
from mini_openharness.session import SessionLog
from mini_openharness.provider import (
    ModelProvider,
    ProviderComplete,
    ProviderReasoningDelta,
    ProviderRetry,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallStart,
)
from mini_openharness.tools import (
    FileSnapshotStore,
    ResourceLockManager,
    ToolContext,
    ToolFailure,
    ToolRegistry,
    ToolResult,
)
from mini_openharness.trace import TraceSink
from mini_openharness.utils.model_trace import ModelTraceRecorder
from mini_openharness.utils.model_timing import ModelAttemptTiming


EventKind = Literal[
    "model_start",
    "first_token",
    "model_response_end",
    "assistant_delta",
    "reasoning_delta",
    "tool_call_start",
    "tool_call_delta",
    "assistant",
    "provider_retry",
    "tool_start",
    "tool_end",
    "compact",
    "hook_blocked",
    "loop_guard",
    "error",
    "cancelled",
    "done",
]


@dataclass(frozen=True)
class AgentEvent:
    kind: EventKind
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationState:
    """State that intentionally survives across sequential user turns."""

    messages: list[Message]
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunState:
    """State owned by exactly one active AgentLoop.run invocation."""

    run_id: str
    cancel_event: asyncio.Event
    resource_locks: ResourceLockManager
    tool_slots: asyncio.Semaphore
    file_snapshots: FileSnapshotStore
    # Tools exposed to the model during this run. MCP tools are added here only
    # after tool_search returns a matching registered tool.
    active_tool_names: set[str] = field(default_factory=set)
    last_tool_batch: str | None = None
    repeated_tool_batches: int = 0


class AgentLoop:
    """Own state and repeat model -> permission -> tools -> model until done."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        workspace: str | Path,
        memory_dir: str | Path | None = None,
        system_prompt: str = "You are a concise coding assistant. Inspect before editing.",
        max_steps: int = 12,
        permission_engine: PermissionEngine | None = None,
        approval_handler: ApprovalHandler | None = None,
        tracer: TraceSink | None = None,
        compactor: ContextCompactor | None = None,
        artifact_store: ArtifactStore | None = None,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        tool_timeout_seconds: float = 30.0,
        max_repeated_tool_batches: int = 3,
        max_concurrent_tools: int = 8,
        hooks: HookRegistry | None = None,
        messages: list[Message] | None = None,
        session: SessionLog | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")
        if max_repeated_tool_batches < 1:
            raise ValueError("max_repeated_tool_batches must be at least 1")
        if max_concurrent_tools < 1:
            raise ValueError("max_concurrent_tools must be at least 1")
        self.provider = provider
        self.tools = tools
        self.workspace = Path(workspace).resolve()
        self.memory_dir = (
            Path(memory_dir).resolve()
            if memory_dir is not None
            else self.workspace / "memdir"
        )
        self.max_steps = max_steps
        self.permission_engine = permission_engine
        self.approval_handler = approval_handler
        self.tracer = tracer
        self._model_trace = ModelTraceRecorder(tracer)
        self.compactor = compactor
        self.artifact_store = artifact_store
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.tool_timeout_seconds = tool_timeout_seconds
        self.max_repeated_tool_batches = max_repeated_tool_batches
        self.max_concurrent_tools = max_concurrent_tools
        self.hooks = hooks if hooks is not None else HookRegistry()
        self.hook_executor = HookExecutor(
            self.hooks,
            workspace=self.workspace,
            tracer=self.tracer,
        )
        self.session = session
        if messages:
            conversation_messages = list(messages)
            current_system = Message("system", system_prompt)
            if conversation_messages[0].role == "system":
                conversation_messages[0] = current_system
            else:
                conversation_messages.insert(0, current_system)
        else:
            conversation_messages = [Message("system", system_prompt)]
        self._conversation = ConversationState(conversation_messages)
        self._active_run: RunState | None = None

    @property
    def messages(self) -> list[Message]:
        return self._conversation.messages

    @property
    def input_tokens(self) -> int:
        return self._conversation.input_tokens

    @property
    def output_tokens(self) -> int:
        return self._conversation.output_tokens

    @property
    def estimated_cost(self) -> float:
        return (
            self.input_tokens * self.input_cost_per_million
            + self.output_tokens * self.output_cost_per_million
        ) / 1_000_000

    def cancel(self) -> None:
        if self._active_run is not None:
            self._active_run.cancel_event.set()

    def _persist(self, message: Message) -> None:
        if self.session is not None:
            self.session.append_message(message)

    async def run(self, prompt: str) -> AsyncIterator[AgentEvent]:
        execution = self._manage_run(prompt)
        try:
            async for event in execution:
                yield event
        finally:
            await execution.aclose()

    async def resume(self) -> AsyncIterator[AgentEvent]:
        """Continue an interrupted session without a new user prompt.

        The conversation must be preloaded through AgentLoop(messages=...); the
        loop resumes from wherever the transcript stopped.
        """
        execution = self._manage_run()
        try:
            async for event in execution:
                yield event
        finally:
            await execution.aclose()

    # manage loop state
    async def _manage_run(
        self, prompt: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Create per-run state and start a new or resumed agent run."""
        if self._active_run is not None:
            raise RunAlreadyActiveError(
                "This AgentLoop already has an active run; use a separate instance for concurrency"
            )

        # asyncio synchronization primitives bind to the active loop. A reusable
        # AgentLoop may be called from a new loop (for example two asyncio.run turns).
        state = RunState(
            run_id=uuid4().hex,
            cancel_event=asyncio.Event(),
            resource_locks=ResourceLockManager(),
            tool_slots=asyncio.Semaphore(self.max_concurrent_tools),
            file_snapshots=FileSnapshotStore(),
            active_tool_names=self.tools.default_exposed_names(),
        )
        self._active_run = state
        execution = (
            self._resume_turn(state)
            if prompt is None
            else self._start_new_turn(prompt, state)
        )
        try:
            async for event in execution:
                yield event
        finally:
            try:
                await execution.aclose()
            finally:
                if self._active_run is state:
                    self._active_run = None

    # exec user message
    async def _start_new_turn(
        self, prompt: str, state: RunState
    ) -> AsyncIterator[AgentEvent]:
        prompt_hooks = await self.hook_executor.execute(
            HookEvent.USER_PROMPT_SUBMIT,
            {"prompt": prompt},
        )
        if prompt_hooks.blocked:
            reason = prompt_hooks.reason or "user prompt rejected by hook"
            data = {"event": HookEvent.USER_PROMPT_SUBMIT.value, "reason": reason}
            if self.tracer:
                self.tracer.finish(status="failed", data=data)
            yield AgentEvent("hook_blocked", reason, data)
            yield AgentEvent("error", f"Hook blocked prompt: {reason}", data)
            return
        prompt = str(prompt_hooks.payload.get("prompt", prompt))
        user_message = Message("user", prompt)
        self.messages.append(user_message)
        self._persist(user_message)
        context = self._make_context(state)
        async for event in self._run_agent_loop(
            context, state, max_steps=self.max_steps
        ):
            yield event

    def _make_context(self, state: RunState) -> ToolContext:
        return ToolContext(
            self.workspace,
            memory_dir=self.memory_dir,
            permission_engine=self.permission_engine,
            approval_handler=self.approval_handler,
            tracer=self.tracer,
            tool_timeout_seconds=self.tool_timeout_seconds,
            file_snapshots=state.file_snapshots,
        )

    async def _resume_turn(self, state: RunState) -> AsyncIterator[AgentEvent]:
        used_steps = sum(1 for message in self.messages if message.role == "assistant")
        remaining_steps = max(1, self.max_steps - used_steps)
        context = self._make_context(state)
        async for event in self._run_agent_loop(
            context, state, max_steps=remaining_steps
        ):
            yield event

    async def _run_agent_loop(
        self,
        context: ToolContext,
        state: RunState,
        *,
        max_steps: int,
    ) -> AsyncIterator[AgentEvent]:
        reactive_context_retry_attempted = False

        for step in range(1, max_steps + 1):
            if state.cancel_event.is_set():
                yield self._cancelled_event()
                return
            compact_event = await self._compact_if_needed()
            if compact_event is not None:
                yield compact_event

            model_attempt = 0
            while True:
                model_attempt += 1
                timing = ModelAttemptTiming()
                # send model start event for cli ans sessions
                yield AgentEvent("model_start", data={"step": step, "attempt": model_attempt})
                self._model_trace.record_request(
                    step=step,
                    attempt=model_attempt,
                    messages=self.messages,
                    tools=self._visible_tool_schemas(state),
                )

                reply: ModelReply | None = None
                streamed = False
                try:

                    # get value of stream method
                    stream_method = getattr(self.provider, "stream", None)
                    # “Parse the model’s streaming response into different event types and yield each event downstream as it arrives.”
                    if callable(stream_method):
                        async for provider_event in stream_method(
                            self.messages,
                            self._visible_tool_schemas(state),
                            cancel_event=state.cancel_event,
                        ):
                            if isinstance(provider_event, ProviderReasoningDelta):
                                timing.mark_reasoning()
                                yield AgentEvent(
                                    "reasoning_delta",
                                    provider_event.delta,
                                    {"step": step},
                                )
                            elif isinstance(provider_event, ProviderTextDelta):
                                ttft_data = timing.mark_text(
                                    step=step,
                                    attempt=model_attempt,
                                )
                                if ttft_data is not None:
                                    self._model_trace.record_first_token(ttft_data)
                                    yield AgentEvent("first_token", data=ttft_data)
                                streamed = True
                                self._model_trace.record_assistant_delta(
                                    provider_event.text
                                )
                                yield AgentEvent(
                                    "assistant_delta", provider_event.text, {"step": step}
                                )
                            elif isinstance(provider_event, ProviderToolCallStart):
                                timing.mark_tool_call()
                                yield AgentEvent(
                                    "tool_call_start",
                                    data={
                                        "step": step,
                                        "index": provider_event.index,
                                        "name": provider_event.name,
                                        "call_id": provider_event.call_id,
                                    },
                                )
                            elif isinstance(provider_event, ProviderToolCallDelta):
                                timing.mark_activity()
                                yield AgentEvent(
                                    "tool_call_delta",
                                    data={
                                        "step": step,
                                        "index": provider_event.index,
                                        "arguments_delta": provider_event.arguments_delta,
                                    },
                                )
                            elif isinstance(provider_event, ProviderRetry):
                                data = {
                                    "attempt": provider_event.attempt,
                                    "delay_seconds": provider_event.delay_seconds,
                                    "error": provider_event.error,
                                }
                                if self.tracer:
                                    self.tracer.emit("provider_retry", data)
                                yield AgentEvent("provider_retry", provider_event.error, data)
                            elif isinstance(provider_event, ProviderComplete):
                                reply = provider_event.reply
                    else:
                        reply = await self.provider.complete(
                            self.messages,
                            self._visible_tool_schemas(state),
                        )
                except ProviderCancelledError as exc:
                    yield self._cancelled_event(str(exc))
                    return
                except ProviderContextWindowError as exc:
                    compact_event = None
                    if not reactive_context_retry_attempted:
                        reactive_context_retry_attempted = True
                        compact_event = await self._compact_if_needed(
                            force=True,
                            trigger="reactive",
                        )
                    if compact_event is not None:
                        data = {
                            "step": step,
                            "attempt": model_attempt,
                            "reason": str(exc),
                            **compact_event.data,
                        }
                        if self.tracer:
                            self.tracer.emit("context_retry", data)
                        yield compact_event
                        continue
                    data = {"type": type(exc).__name__, "message": str(exc), "step": step}
                    if self.tracer:
                        self.tracer.emit("provider_error", data)
                        self.tracer.finish(status="failed", data={"reason": str(exc)})
                    yield AgentEvent("error", str(exc), data)
                    return
                except ProviderError as exc:
                    data = {"type": type(exc).__name__, "message": str(exc), "step": step}
                    if self.tracer:
                        self.tracer.emit("provider_error", data)
                        self.tracer.finish(status="failed", data={"reason": str(exc)})
                    yield AgentEvent("error", str(exc), data)
                    return
                break

            if reply is None:
                error = "Provider finished without a response"
                if self.tracer:
                    self.tracer.finish(status="failed", data={"reason": error})
                yield AgentEvent("error", error, {"step": step})
                return

            response_data = {
                "step": step,
                "attempt": model_attempt,
                "model": getattr(self.provider, "model", None),
                **timing.response_data(),
                "visible_output_chars": len(reply.content),
                "reported_output_tokens": reply.output_tokens,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
            }
            if self.tracer:
                self.tracer.emit("model_response_end", response_data)
            yield AgentEvent("model_response_end", data=response_data)

            self._conversation.input_tokens += reply.input_tokens
            self._conversation.output_tokens += reply.output_tokens
            assistant_message = Message(
                "assistant", reply.content, tool_calls=reply.tool_calls
            )
            self.messages.append(assistant_message)
            self._persist(assistant_message)
            if self.tracer:
                self.tracer.emit(
                    "model_response",
                    {
                        "step": step,
                        "content": reply.content,
                        "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in reply.tool_calls
                        ],
                        "input_tokens": reply.input_tokens,
                        "output_tokens": reply.output_tokens,
                        "estimated_cost": self.estimated_cost,
                    },
                )
            if reply.content:
                yield AgentEvent("assistant", reply.content, {"step": step, "streamed": streamed})

            if not reply.tool_calls:
                data = {
                    "steps": step,
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "estimated_cost": self.estimated_cost,
                }
                stop_hooks = await self.hook_executor.execute(
                    HookEvent.STOP,
                    {"response": reply.content, **data},
                )
                if stop_hooks.blocked:
                    reason = stop_hooks.reason or "completion rejected by hook"
                    hook_data = {
                        "event": HookEvent.STOP.value,
                        "reason": reason,
                        "step": step,
                    }
                    if self.tracer:
                        self.tracer.emit("hook_blocked", hook_data)
                    yield AgentEvent("hook_blocked", reason, hook_data)
                    retry_message = Message(
                        "user",
                        "Completion was rejected by a trusted verification hook: "
                        f"{reason}\nFix the issue, verify it, and try to finish again.",
                    )
                    self.messages.append(retry_message)
                    self._persist(retry_message)
                    continue
                if self.tracer:
                    self.tracer.finish(status="completed", data=data)
                yield AgentEvent("done", data=data)
                return

            for call in reply.tool_calls:
                data = {
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                    **self.tools.attribution(call.name),
                }
                if self.tracer:
                    self.tracer.emit("tool_start", data)
                yield AgentEvent("tool_start", data=data)

            repeated = self._record_tool_batch(reply.tool_calls, state)
            if repeated > self.max_repeated_tool_batches:
                reason = (
                    "Repeated identical tool batch blocked after "
                    f"{self.max_repeated_tool_batches} executions"
                )
                guard_data = {"reason": reason, "repeat_count": repeated, "step": step}
                if self.tracer:
                    self.tracer.emit("loop_guard", guard_data)
                yield AgentEvent("loop_guard", reason, guard_data)
                tool_results_with_timings = [
                    (
                        ToolResult.fail(
                            reason,
                            code="loop_guard",
                            stage="execute",
                            metadata={"loop_guard": True},
                        ),
                        0,
                    )
                    for _ in reply.tool_calls
                ]
            else:
                tool_results_with_timings = await self._execute_all(
                    reply.tool_calls, context, state
                )

            if tool_results_with_timings is None:
                yield self._cancelled_event()
                return
            for call, (result, elapsed_ms) in zip(
                reply.tool_calls, tool_results_with_timings
            ):
                model_visible_output, full_output_path = (
                    self._condense_tool_output_if_needed(call, result.output)
                )
                failure_for_model = result.failure
                if (
                    failure_for_model is not None
                    and model_visible_output != result.output
                ):
                    failure_for_model = ToolFailure(
                        code=failure_for_model.code,
                        stage=failure_for_model.stage,
                        message=model_visible_output,
                        retryable=failure_for_model.retryable,
                        detail=dict(failure_for_model.detail),
                    )
                result_for_model = ToolResult(
                    model_visible_output,
                    result.is_error,
                    dict(result.metadata),
                    failure_for_model,
                )
                if full_output_path is not None:
                    result_for_model.metadata["artifact_path"] = str(full_output_path)
                    result_for_model.metadata["original_chars"] = len(result.output)

                self._apply_tool_runtime_effect(call, result_for_model, state)
                self._append_tool_result(call.id, call.name, result_for_model)
                data = {
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                    **self.tools.attribution(call.name),
                    "is_error": result_for_model.is_error,
                    "elapsed_ms": elapsed_ms,
                    "output": result_for_model.output,
                    **result_for_model.metadata,
                }
                if result_for_model.failure is not None:
                    data["failure"] = result_for_model.failure.to_dict()
                if self.tracer:
                    self.tracer.emit("tool_end", data)
                    if (
                        self.tools.descriptor(call.name).source == "skill"
                        and not result_for_model.is_error
                    ):
                        self.tracer.emit(
                            "skill_loaded", {"name": call.arguments.get("name")}
                        )
                yield AgentEvent("tool_end", result_for_model.output, data)

        error = f"Agent did not finish within {self.max_steps} steps"
        if self.tracer:
            self.tracer.finish(status="failed", data={"reason": error})
        raise MaxStepsExceeded(error)

    def _visible_tool_schemas(self, state: RunState) -> list[dict[str, Any]]:
        """Return only the tools currently exposed to the model."""
        return self.tools.schemas(state.active_tool_names)

    def _apply_tool_runtime_effect(
        self,
        call: ToolCall,
        result: ToolResult,
        state: RunState,
    ) -> None:
        """Expose matching MCP tools after a successful tool_search call."""
        if result.is_error or call.name != "tool_search":
            return

        matched_tools = result.metadata.get("matched_tools", [])
        if not isinstance(matched_tools, list):
            return

        for name in matched_tools:
            if not isinstance(name, str) or name not in dict(self.tools.items()):
                continue
            if self.tools.descriptor(name).source == "mcp":
                state.active_tool_names.add(name)

    async def _execute_timed(
        self,
        call: ToolCall,
        context: ToolContext,
        state: RunState,
    ) -> tuple[ToolResult, int]:
        started = time.monotonic()
        slot_data = {
            "tool": call.name,
            "tool_call_id": call.id,
            "max_concurrent_tools": self.max_concurrent_tools,
        }
        if self.tracer:
            self.tracer.emit("tool_slot_wait", slot_data)
        async with state.tool_slots:
            acquired = time.monotonic()
            if self.tracer:
                self.tracer.emit(
                    "tool_slot_acquired",
                    {
                        **slot_data,
                        "waited_ms": int((acquired - started) * 1000),
                    },
                )
            try:
                return await self._execute_with_slot(call, context, state, started)
            finally:
                if self.tracer:
                    self.tracer.emit(
                        "tool_slot_released",
                        {
                            **slot_data,
                            "held_ms": int((time.monotonic() - acquired) * 1000),
                        },
                    )

    async def _execute_with_slot(
        self,
        call: ToolCall,
        context: ToolContext,
        state: RunState,
        started: float,
    ) -> tuple[ToolResult, int]:
        name = call.name
        arguments = call.arguments
        pre_hooks = await self.hook_executor.execute(
            HookEvent.PRE_TOOL_USE,
            {
                "tool_call_id": call.id,
                "tool_name": name,
                "tool_input": arguments,
                "tool_source": self.tools.source(name),
            },
        )
        if pre_hooks.blocked:
            reason = pre_hooks.reason or "tool call rejected by hook"
            return (
                ToolResult.fail(
                    f"Hook blocked {name}: {reason}",
                    code="hook_blocked",
                    stage="authorize",
                    metadata={"hook_blocked": True, "hook_event": HookEvent.PRE_TOOL_USE.value},
                ),
                int((time.monotonic() - started) * 1000),
            )
        arguments = pre_hooks.payload.get("tool_input", arguments)
        if not isinstance(arguments, dict):
            return (
                ToolResult.fail(
                    f"Hook produced invalid tool_input for {name}: expected an object",
                    code="invalid_hook_output",
                    stage="validate",
                    metadata={"hook_invalid_payload": True},
                ),
                int((time.monotonic() - started) * 1000),
            )
        lock_started = time.monotonic()
        resources = self.tools.resources(name, arguments, context)
        resource_data = [
            {"key": item.key, "mode": item.mode, "tree": item.tree} for item in resources
        ]
        if self.tracer:
            self.tracer.emit(
                "resource_wait",
                {
                    "tool": name,
                    "resources": resource_data,
                },
            )
        async with state.resource_locks.acquire(resources):
            acquired = time.monotonic()
            if self.tracer:
                self.tracer.emit(
                    "resource_acquired",
                    {
                        "tool": name,
                        "resources": resource_data,
                        "waited_ms": int((acquired - lock_started) * 1000),
                    },
                )
            try:
                result = await self.tools.execute(name, arguments, context)
            finally:
                if self.tracer:
                    self.tracer.emit(
                        "resource_released",
                        {
                            "tool": name,
                            "resources": resource_data,
                            "held_ms": int((time.monotonic() - acquired) * 1000),
                        },
                    )
        if arguments != call.arguments:
            result = ToolResult(
                result.output,
                result.is_error,
                {**result.metadata, "hook_modified_input": True, "executed_input": arguments},
                result.failure,
            )
        post_hooks = await self.hook_executor.execute(
            HookEvent.POST_TOOL_USE,
            {
                "tool_call_id": call.id,
                "tool_name": name,
                "tool_input": arguments,
                "tool_source": self.tools.source(name),
                "tool_output": result.output,
                "is_error": result.is_error,
                "metadata": result.metadata,
            },
        )
        if post_hooks.blocked:
            reason = post_hooks.reason or "tool result rejected by hook"
            result = ToolResult.fail(
                f"Hook blocked result from {name}: {reason}",
                code="hook_blocked",
                stage="postprocess",
                metadata={
                    **result.metadata,
                    "hook_blocked": True,
                    "hook_event": HookEvent.POST_TOOL_USE.value,
                },
            )
        else:
            payload = post_hooks.payload
            output = payload.get("tool_output", result.output)
            metadata = payload.get("metadata", result.metadata)
            is_error = payload.get("is_error", result.is_error)
            if (
                not isinstance(output, str)
                or not isinstance(metadata, dict)
                or not isinstance(is_error, bool)
            ):
                result = ToolResult.fail(
                    f"Hook produced an invalid post_tool_use payload for {name}",
                    code="invalid_hook_output",
                    stage="postprocess",
                    metadata={"hook_invalid_payload": True},
                )
            else:
                failure = result.failure if is_error else None
                if failure is not None and output != result.output:
                    failure = ToolFailure(
                        code=failure.code,
                        stage=failure.stage,
                        message=output,
                        retryable=failure.retryable,
                        detail=dict(failure.detail),
                    )
                if is_error and failure is None:
                    failure = ToolFailure(
                        code="hook_reported_error",
                        stage="postprocess",
                        message=output,
                    )
                result = ToolResult(output, is_error, dict(metadata), failure)
        return result, int((time.monotonic() - started) * 1000)

    async def _execute_all(self, calls, context, state: RunState):
        async def execute_batch():
            # Every call starts concurrently; hierarchical read/write resource locks
            # serialize only conflicting effects while gather preserves result order.
            tasks = []
            for call in calls:
                tasks.append(self._execute_timed(call, context, state))
            
            return await asyncio.gather(*tasks)

        gather_task = asyncio.create_task(execute_batch())
        cancel_task = asyncio.create_task(state.cancel_event.wait())
        done, _ = await asyncio.wait(
            {gather_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task in done and cancel_task.result():
            gather_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gather_task
            return None
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        return await gather_task

    def _record_tool_batch(self, calls, state: RunState) -> int:
        signature = json.dumps(
            [{"name": call.name, "arguments": call.arguments} for call in calls],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if signature == state.last_tool_batch:
            state.repeated_tool_batches += 1
        else:
            state.last_tool_batch = signature
            state.repeated_tool_batches = 1
        return state.repeated_tool_batches

    def _condense_tool_output_if_needed(
        self, call: ToolCall, output: str
    ) -> tuple[str, Path | None]:
        """Keep small outputs inline and offload oversized outputs to an artifact."""
        if self.artifact_store is None:
            return output, None
        if self._is_artifact_path(call):
            limit = self.artifact_store.max_inline_chars
            model_visible_output = (
                output[:limit]
                + "\n...[artifact file; content stays in the original artifact]"
            )
            return model_visible_output, None
        if call.name == "read_file":
            # read_file paginates its own output; source reads are never offloaded.
            return output, None
        if not self.artifact_store.exceeds_inline_limit(output):
            return output, None

        run_id = self.tracer.run_id if self.tracer else "untraced"
        return self.artifact_store.offload(run_id=run_id, tool_call_id=call.id, output=output)

    def _is_artifact_path(self, call: ToolCall) -> bool:
        """True when the tool call reads a file inside the artifact store."""
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            return False
        try:
            candidate = (self.workspace / raw_path).resolve()
        except OSError:
            return False
        try:
            candidate.relative_to(self.artifact_store.root)
        except ValueError:
            return False
        return True

    async def _compact_if_needed(
        self,
        *,
        force: bool = False,
        trigger: str = "threshold",
    ) -> AgentEvent | None:
        if self.compactor is None:
            return None
        result = await self.compactor.compact_with_provider(
            self.messages,
            self.provider,
            force=force,
        )
        if not result.compacted:
            return None
        self._conversation.messages = result.messages
        self._conversation.input_tokens += result.summary_input_tokens
        self._conversation.output_tokens += result.summary_output_tokens
        if self.session is not None:
            self.session.append_compaction(
                summary=result.summary_text,
                replaced_messages=result.summarized_messages,
                before_tokens=result.before_tokens,
                after_tokens=result.after_tokens,
                summary_source=result.summary_source,
            )
        data = {
            "before_tokens": result.before_tokens,
            "after_tokens": result.after_tokens,
            "summarized_messages": result.summarized_messages,
            "summary_source": result.summary_source,
            "trigger": trigger,
        }
        if self.tracer:
            self.tracer.emit("context_compacted", data)
        return AgentEvent("compact", data=data)

    def _cancelled_event(self, reason: str = "Agent run cancelled") -> AgentEvent:
        if self.tracer:
            self.tracer.finish(status="cancelled", data={"reason": reason})
        return AgentEvent("cancelled", reason)

    def _append_tool_result(self, call_id: str, name: str, result: ToolResult) -> None:
        prefix = "ERROR: " if result.is_error else ""
        message = Message(
            "tool", prefix + result.output, tool_call_id=call_id, name=name
        )
        self.messages.append(message)
        self._persist(message)
