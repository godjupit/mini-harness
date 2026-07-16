"""Extensible, policy-aware lifecycle hooks for the mini agent runtime."""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import os
import shlex
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol

from mini_openharness.trace import TraceSink


class HookEvent(str, Enum):
    """Stable lifecycle points exposed by :class:`AgentLoop`."""

    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    STOP = "stop"


FailureMode = Literal["continue", "block"]


@dataclass(frozen=True)
class HookContext:
    """Immutable invocation context passed to every hook implementation."""

    event: HookEvent
    payload: dict[str, Any]
    workspace: Path


@dataclass(frozen=True)
class HookResult:
    """One hook decision plus optional changes for following hooks/runtime."""

    blocked: bool = False
    reason: str = ""
    output: str = ""
    updated_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookRunResult:
    name: str
    hook_type: str
    blocked: bool
    reason: str
    output: str
    elapsed_ms: int
    failed: bool = False


@dataclass(frozen=True)
class AggregatedHookResult:
    """Ordered hook results and the payload after all accepted updates."""

    payload: dict[str, Any]
    results: tuple[HookRunResult, ...] = ()

    @property
    def blocked(self) -> bool:
        return any(result.blocked for result in self.results)

    @property
    def reason(self) -> str:
        return next((result.reason for result in reversed(self.results) if result.reason), "")


class Hook(Protocol):
    """Extension protocol; custom hooks need no executor changes."""

    name: str
    priority: int
    matcher: str | None
    timeout_seconds: float
    failure_mode: FailureMode

    async def run(self, context: HookContext) -> HookResult:
        """Evaluate one lifecycle event."""


HookHandler = Callable[[HookContext], HookResult | None | Awaitable[HookResult | None]]


@dataclass
class CallbackHook:
    """Adapt a sync or async Python callback to the Hook protocol."""

    name: str
    handler: HookHandler
    priority: int = 0
    matcher: str | None = None
    timeout_seconds: float = 10.0
    failure_mode: FailureMode = "block"

    async def run(self, context: HookContext) -> HookResult:
        value = self.handler(context)
        if inspect.isawaitable(value):
            value = await value
        if value is None:
            return HookResult()
        if not isinstance(value, HookResult):
            raise TypeError("callback hook must return HookResult or None")
        return value


@dataclass
class CommandHook:
    """Run an argv-based subprocess hook without invoking a host shell."""

    name: str
    command: tuple[str, ...]
    priority: int = 0
    matcher: str | None = None
    timeout_seconds: float = 10.0
    failure_mode: FailureMode = "block"
    environment: dict[str, str] = field(default_factory=dict)
    expect_json: bool = False
    inherit_environment: bool = False

    async def run(self, context: HookContext) -> HookResult:
        process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=context.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **(
                    dict(os.environ)
                    if self.inherit_environment
                    else _minimal_subprocess_environment()
                ),
                **self.environment,
                "MINI_OH_HOOK_EVENT": context.event.value,
                "MINI_OH_HOOK_NAME": self.name,
            },
        )
        request = json.dumps(
            {"event": context.event.value, "payload": context.payload},
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        try:
            stdout, stderr = await process.communicate(request)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        output = stdout.decode("utf-8", errors="replace").strip()
        error_output = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = "\n".join(part for part in (output, error_output) if part)
            detail = detail[-8_000:] or f"exit code {process.returncode}"
            raise RuntimeError(f"command hook failed: {detail}")
        if not output:
            return HookResult(output=error_output)
        try:
            response = json.loads(output)
        except json.JSONDecodeError as exc:
            if self.expect_json:
                raise ValueError("command hook stdout must be one JSON object") from exc
            return HookResult(output="\n".join(part for part in (output, error_output) if part))
        if not isinstance(response, dict):
            if self.expect_json:
                raise ValueError("command hook stdout must be one JSON object")
            return HookResult(output="\n".join(part for part in (output, error_output) if part))
        decision = response.get("decision", "allow")
        if decision not in {"allow", "block"}:
            raise ValueError("hook decision must be 'allow' or 'block'")
        updated_payload = response.get("updated_payload", {})
        if not isinstance(updated_payload, dict):
            raise ValueError("updated_payload must be an object")
        return HookResult(
            blocked=decision == "block",
            reason=str(response.get("reason", "")),
            output=str(response.get("output", error_output)),
            updated_payload=dict(updated_payload),
        )


class HookRegistry:
    """Store hook implementations by lifecycle event in stable priority order."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[Hook]] = defaultdict(list)

    def register(self, event: HookEvent | str, hook: Hook) -> None:
        resolved_event = HookEvent(event)
        _validate_hook(hook)
        self._hooks[resolved_event].append(hook)

    def get(self, event: HookEvent | str) -> tuple[Hook, ...]:
        hooks = self._hooks.get(HookEvent(event), ())
        return tuple(sorted(hooks, key=lambda hook: -hook.priority))

    def __bool__(self) -> bool:
        return any(self._hooks.values())


class HookExecutor:
    """Run matching hooks sequentially and aggregate decisions deterministically."""

    def __init__(
        self,
        registry: HookRegistry,
        *,
        workspace: str | Path,
        tracer: TraceSink | None = None,
    ) -> None:
        self.registry = registry
        self.workspace = Path(workspace).resolve()
        self.tracer = tracer

    async def execute(
        self, event: HookEvent | str, payload: dict[str, Any]
    ) -> AggregatedHookResult:
        resolved_event = HookEvent(event)
        current_payload = dict(payload)
        results: list[HookRunResult] = []
        for hook in self.registry.get(resolved_event):
            if not _matches(hook.matcher, resolved_event, current_payload):
                continue
            started = time.monotonic()
            hook_type = type(hook).__name__
            if self.tracer:
                self.tracer.emit(
                    "hook_start",
                    {
                        "event": resolved_event.value,
                        "name": hook.name,
                        "type": hook_type,
                        "payload": current_payload,
                    },
                )
            failed = False
            try:
                result = await asyncio.wait_for(
                    hook.run(HookContext(resolved_event, dict(current_payload), self.workspace)),
                    timeout=hook.timeout_seconds,
                )
                if not isinstance(result, HookResult):
                    raise TypeError("hook must return HookResult")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                result = HookResult(
                    blocked=hook.failure_mode == "block",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result.updated_payload and not result.blocked:
                current_payload.update(result.updated_payload)
            run_result = HookRunResult(
                name=hook.name,
                hook_type=hook_type,
                blocked=result.blocked,
                reason=result.reason,
                output=result.output,
                elapsed_ms=elapsed_ms,
                failed=failed,
            )
            results.append(run_result)
            if self.tracer:
                self.tracer.emit(
                    "hook_end",
                    {
                        "event": resolved_event.value,
                        "name": hook.name,
                        "type": hook_type,
                        "blocked": result.blocked,
                        "failed": failed,
                        "reason": result.reason,
                        "output": result.output,
                        "updated_payload": result.updated_payload,
                        "elapsed_ms": elapsed_ms,
                    },
                )
            if result.blocked:
                break
        return AggregatedHookResult(dict(current_payload), tuple(results))


def load_hook_registry(path: str | Path) -> HookRegistry:
    """Load command hooks from a strict JSON configuration file."""

    config_path = Path(path).resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    raw_events = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(raw_events, dict):
        raise ValueError("hook config must contain a 'hooks' object")
    registry = HookRegistry()
    for raw_event, raw_hooks in raw_events.items():
        event = HookEvent(raw_event)
        if not isinstance(raw_hooks, list):
            raise ValueError(f"hooks.{raw_event} must be an array")
        for index, raw in enumerate(raw_hooks):
            if not isinstance(raw, dict):
                raise ValueError(f"hooks.{raw_event}[{index}] must be an object")
            if raw.get("type", "command") != "command":
                raise ValueError("JSON hook config currently supports type='command'")
            raw_command = raw.get("command")
            if isinstance(raw_command, str):
                command = tuple(shlex.split(raw_command))
            elif isinstance(raw_command, list) and all(
                isinstance(item, str) for item in raw_command
            ):
                command = tuple(raw_command)
            else:
                raise ValueError(f"hooks.{raw_event}[{index}].command must be string or array")
            command = tuple(item.replace("{python}", sys.executable) for item in command)
            if not command:
                raise ValueError(f"hooks.{raw_event}[{index}].command cannot be empty")
            environment = raw.get("env", {})
            if not isinstance(environment, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            ):
                raise ValueError(f"hooks.{raw_event}[{index}].env must be a string map")
            registry.register(
                event,
                CommandHook(
                    name=str(raw.get("name", f"{raw_event}-{index + 1}")),
                    command=command,
                    priority=int(raw.get("priority", 0)),
                    matcher=str(raw["matcher"]) if raw.get("matcher") else None,
                    timeout_seconds=float(raw.get("timeout_seconds", 10.0)),
                    failure_mode=str(raw.get("failure_mode", "block")),  # type: ignore[arg-type]
                    environment=dict(environment),
                    expect_json=bool(raw.get("expect_json", False)),
                    inherit_environment=bool(raw.get("inherit_environment", False)),
                ),
            )
    return registry


def _validate_hook(hook: Hook) -> None:
    if not hook.name:
        raise ValueError("hook name cannot be empty")
    if hook.timeout_seconds <= 0:
        raise ValueError("hook timeout_seconds must be positive")
    if hook.failure_mode not in {"continue", "block"}:
        raise ValueError("hook failure_mode must be 'continue' or 'block'")
    if not callable(getattr(hook, "run", None)):
        raise TypeError("hook must provide an async run(context) method")


def _matches(matcher: str | None, event: HookEvent, payload: dict[str, Any]) -> bool:
    if not matcher:
        return True
    if event in {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE}:
        subject = str(payload.get("tool_name", ""))
    else:
        subject = str(payload.get("prompt") or payload.get("response") or "")
    return fnmatch.fnmatchcase(subject, matcher)


def _minimal_subprocess_environment() -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "VIRTUAL_ENV",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}
