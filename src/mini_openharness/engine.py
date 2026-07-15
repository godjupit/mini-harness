"""The observable, permissioned, tool-aware agent loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from mini_openharness.compaction import ArtifactStore, ContextCompactor
from mini_openharness.hooks import HookEvent, HookExecutor, HookRegistry
from mini_openharness.models import Message, ModelReply, ToolCall
from mini_openharness.permissions import ApprovalCallback, PermissionPolicy
from mini_openharness.provider import (
    ModelProvider,
    ProviderCancelledError,
    ProviderComplete,
    ProviderContextWindowError,
    ProviderError,
    ProviderRetry,
    ProviderTextDelta,
)
from mini_openharness.tools import ResourceLockManager, ToolContext, ToolRegistry, ToolResult
from mini_openharness.trace import TraceWriter


EventKind = Literal[
    "model_start",
    "assistant_delta",
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


class MaxStepsExceeded(RuntimeError):
    pass


class AgentLoop:
    """Own state and repeat model -> permission -> tools -> model until done."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        workspace: str | Path,
        system_prompt: str = "You are a concise coding assistant. Inspect before editing.",
        max_steps: int = 12,
        allow_write: bool = False,
        permission_policy: PermissionPolicy | None = None,
        approval_callback: ApprovalCallback | None = None,
        tracer: TraceWriter | None = None,
        compactor: ContextCompactor | None = None,
        artifact_store: ArtifactStore | None = None,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        tool_timeout_seconds: float = 30.0,
        max_repeated_tool_batches: int = 3,
        hooks: HookRegistry | None = None,
        messages: list[Message] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")
        if max_repeated_tool_batches < 1:
            raise ValueError("max_repeated_tool_batches must be at least 1")
        self.provider = provider
        self.tools = tools
        self.workspace = Path(workspace).resolve()
        self.max_steps = max_steps
        self.allow_write = allow_write
        self.permission_policy = permission_policy
        self.approval_callback = approval_callback
        self.tracer = tracer
        self.compactor = compactor
        self.artifact_store = artifact_store
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.tool_timeout_seconds = tool_timeout_seconds
        self.max_repeated_tool_batches = max_repeated_tool_batches
        self.hooks = hooks if hooks is not None else HookRegistry()
        self.hook_executor = HookExecutor(
            self.hooks,
            workspace=self.workspace,
            tracer=self.tracer,
        )
        self.cancel_event = asyncio.Event()
        if messages:
            self.messages = list(messages)
            current_system = Message("system", system_prompt)
            if self.messages[0].role == "system":
                self.messages[0] = current_system
            else:
                self.messages.insert(0, current_system)
        else:
            self.messages = [Message("system", system_prompt)]
        self.input_tokens = 0
        self.output_tokens = 0
        self._last_tool_batch: str | None = None
        self._repeated_tool_batches = 0
        self._resource_locks = ResourceLockManager()

    @property
    def estimated_cost(self) -> float:
        return (
            self.input_tokens * self.input_cost_per_million
            + self.output_tokens * self.output_cost_per_million
        ) / 1_000_000

    def cancel(self) -> None:
        self.cancel_event.set()

    async def run(self, prompt: str) -> AsyncIterator[AgentEvent]:
        # asyncio synchronization primitives bind to the active loop. A reusable
        # AgentLoop may be called from a new loop (for example two asyncio.run turns).
        self.cancel_event = asyncio.Event()
        self._resource_locks = ResourceLockManager()
        self._last_tool_batch = None
        self._repeated_tool_batches = 0
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
        self.messages.append(Message("user", prompt))
        context = ToolContext(
            self.workspace,
            allow_write=self.allow_write,
            permission_policy=self.permission_policy,
            approval_callback=self.approval_callback,
            tracer=self.tracer,
            tool_timeout_seconds=self.tool_timeout_seconds,
        )
        reactive_context_retry_attempted = False

        for step in range(1, self.max_steps + 1):
            if self.cancel_event.is_set():
                yield self._cancelled_event()
                return
            compact_event = self._compact_if_needed()
            if compact_event is not None:
                yield compact_event

            model_attempt = 0
            while True:
                model_attempt += 1
                yield AgentEvent("model_start", data={"step": step, "attempt": model_attempt})
                if self.tracer:
                    self.tracer.emit(
                        "model_request",
                        {
                            "step": step,
                            "attempt": model_attempt,
                            "messages": [message.to_dict() for message in self.messages],
                            "tools": self.tools.schemas(),
                        },
                    )

                reply: ModelReply | None = None
                streamed = False
                try:
                    stream_method = getattr(self.provider, "stream", None)
                    if callable(stream_method):
                        async for provider_event in stream_method(
                            self.messages,
                            self.tools.schemas(),
                            cancel_event=self.cancel_event,
                        ):
                            if isinstance(provider_event, ProviderTextDelta):
                                streamed = True
                                if self.tracer:
                                    self.tracer.emit(
                                        "assistant_delta", {"text": provider_event.text}
                                    )
                                yield AgentEvent(
                                    "assistant_delta", provider_event.text, {"step": step}
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
                        reply = await self.provider.complete(self.messages, self.tools.schemas())
                except ProviderCancelledError as exc:
                    yield self._cancelled_event(str(exc))
                    return
                except ProviderContextWindowError as exc:
                    compact_event = None
                    if not reactive_context_retry_attempted:
                        reactive_context_retry_attempted = True
                        compact_event = self._compact_if_needed(
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

            self.input_tokens += reply.input_tokens
            self.output_tokens += reply.output_tokens
            self.messages.append(Message("assistant", reply.content, tool_calls=reply.tool_calls))
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
                    self.messages.append(
                        Message(
                            "user",
                            "Completion was rejected by a trusted verification hook: "
                            f"{reason}\nFix the issue, verify it, and try to finish again.",
                        )
                    )
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
                    "source": self.tools.source(call.name),
                }
                if call.name.startswith("mcp__"):
                    segments = call.name.split("__", 2)
                    data["mcp_server"] = segments[1] if len(segments) > 2 else "unknown"
                if self.tracer:
                    self.tracer.emit("tool_start", data)
                yield AgentEvent("tool_start", data=data)

            repeated = self._record_tool_batch(reply.tool_calls)
            if repeated > self.max_repeated_tool_batches:
                reason = (
                    "Repeated identical tool batch blocked after "
                    f"{self.max_repeated_tool_batches} executions"
                )
                guard_data = {"reason": reason, "repeat_count": repeated, "step": step}
                if self.tracer:
                    self.tracer.emit("loop_guard", guard_data)
                yield AgentEvent("loop_guard", reason, guard_data)
                timed_results = [
                    (ToolResult(reason, is_error=True, metadata={"loop_guard": True}), 0)
                    for _ in reply.tool_calls
                ]
            else:
                timed_results = await self._execute_all(reply.tool_calls, context)
            if timed_results is None:
                yield self._cancelled_event()
                return
            for call, (result, elapsed_ms) in zip(reply.tool_calls, timed_results):
                inline_output, artifact_path = self._offload(call.id, result.output)
                stored_result = ToolResult(inline_output, result.is_error, dict(result.metadata))
                if artifact_path is not None:
                    stored_result.metadata["artifact_path"] = str(artifact_path)
                    stored_result.metadata["original_chars"] = len(result.output)
                self._append_tool_result(call.id, call.name, stored_result)
                data = {
                    "id": call.id,
                    "name": call.name,
                    "source": self.tools.source(call.name),
                    "is_error": stored_result.is_error,
                    "elapsed_ms": elapsed_ms,
                    "output": stored_result.output,
                    **stored_result.metadata,
                }
                if call.name.startswith("mcp__"):
                    segments = call.name.split("__", 2)
                    data["mcp_server"] = segments[1] if len(segments) > 2 else "unknown"
                if self.tracer:
                    self.tracer.emit("tool_end", data)
                    if call.name == "load_skill" and not stored_result.is_error:
                        self.tracer.emit("skill_loaded", {"name": call.arguments.get("name")})
                    elif call.name == "search_memory":
                        self.tracer.emit(
                            "memory_search",
                            {"query": call.arguments.get("query"), "output": stored_result.output},
                        )
                    elif call.name == "remember" and not stored_result.is_error:
                        self.tracer.emit("memory_write", {"text": call.arguments.get("text")})
                yield AgentEvent("tool_end", stored_result.output, data)

        error = f"Agent did not finish within {self.max_steps} steps"
        if self.tracer:
            self.tracer.finish(status="failed", data={"reason": error})
        raise MaxStepsExceeded(error)

    async def _execute_timed(
        self,
        call: ToolCall,
        context: ToolContext,
    ) -> tuple[ToolResult, int]:
        started = time.monotonic()
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
                ToolResult(
                    f"Hook blocked {name}: {reason}",
                    is_error=True,
                    metadata={"hook_blocked": True, "hook_event": HookEvent.PRE_TOOL_USE.value},
                ),
                int((time.monotonic() - started) * 1000),
            )
        arguments = pre_hooks.payload.get("tool_input", arguments)
        if not isinstance(arguments, dict):
            return (
                ToolResult(
                    f"Hook produced invalid tool_input for {name}: expected an object",
                    is_error=True,
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
        async with self._resource_locks.acquire(resources):
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
            result = ToolResult(
                f"Hook blocked result from {name}: {reason}",
                is_error=True,
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
                result = ToolResult(
                    f"Hook produced an invalid post_tool_use payload for {name}",
                    is_error=True,
                    metadata={"hook_invalid_payload": True},
                )
            else:
                result = ToolResult(output, is_error, dict(metadata))
        return result, int((time.monotonic() - started) * 1000)

    async def _execute_all(self, calls, context):
        async def execute_batch():
            # Every call starts concurrently; hierarchical read/write resource locks
            # serialize only conflicting effects while gather preserves result order.
            return await asyncio.gather(*(self._execute_timed(call, context) for call in calls))

        gather_task = asyncio.create_task(execute_batch())
        cancel_task = asyncio.create_task(self.cancel_event.wait())
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

    def _record_tool_batch(self, calls) -> int:
        signature = json.dumps(
            [{"name": call.name, "arguments": call.arguments} for call in calls],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if signature == self._last_tool_batch:
            self._repeated_tool_batches += 1
        else:
            self._last_tool_batch = signature
            self._repeated_tool_batches = 1
        return self._repeated_tool_batches

    def _offload(self, call_id: str, output: str) -> tuple[str, Path | None]:
        if self.artifact_store is None:
            return output, None
        run_id = self.tracer.run_id if self.tracer else "untraced"
        return self.artifact_store.offload(run_id=run_id, tool_call_id=call_id, output=output)

    def _compact_if_needed(
        self,
        *,
        force: bool = False,
        trigger: str = "threshold",
    ) -> AgentEvent | None:
        if self.compactor is None:
            return None
        result = self.compactor.compact(self.messages, force=force)
        if not result.compacted:
            return None
        self.messages = result.messages
        data = {
            "before_tokens": result.before_tokens,
            "after_tokens": result.after_tokens,
            "summarized_messages": result.summarized_messages,
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
        self.messages.append(
            Message("tool", prefix + result.output, tool_call_id=call_id, name=name)
        )
