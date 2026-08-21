"""Core tool infrastructure: registry, permissions, resources, results, paths."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from jsonschema import SchemaError, ValidationError, validate

from mini_openharness.permissions import (
    ApprovalHandler,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    build_default_rules,
)

if TYPE_CHECKING:
    from mini_openharness.trace import TraceSink

JsonSchema = dict[str, Any]
ToolEffect = Literal["read", "write", "remote", "compute", "unknown"]
FailureStage = Literal["lookup", "validate", "authorize", "execute", "postprocess"]


@dataclass(frozen=True)
class ResourceAccess:
    """One logical resource lock requested by a tool invocation."""

    key: str
    mode: str = "read"
    tree: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"read", "write"}:
            raise ValueError("resource mode must be read or write")


class ResourceLockManager:
    """Fair-enough async read/write locks supporting hierarchical tree resources."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active: list[tuple[object, tuple[ResourceAccess, ...]]] = []

    @asynccontextmanager
    async def acquire(self, resources: tuple[ResourceAccess, ...]):
        token = object()
        normalized = tuple(sorted(resources, key=lambda item: (item.key, item.mode, item.tree)))
        async with self._condition:
            await self._condition.wait_for(lambda: self._available(normalized))
            self._active.append((token, normalized))
        try:
            yield
        finally:
            async with self._condition:
                self._active = [entry for entry in self._active if entry[0] is not token]
                self._condition.notify_all()

    def _available(self, requested: tuple[ResourceAccess, ...]) -> bool:
        return not any(
            _resources_conflict(candidate, held)
            for candidate in requested
            for _, active in self._active
            for held in active
        )


def _resources_conflict(left: ResourceAccess, right: ResourceAccess) -> bool:
    if left.mode == right.mode == "read":
        return False
    if left.key == "*" or right.key == "*":
        return True
    if left.key == right.key:
        return True
    if left.tree and right.key.startswith(left.key.rstrip("/") + "/"):
        return True
    if right.tree and left.key.startswith(right.key.rstrip("/") + "/"):
        return True
    return False


@dataclass(frozen=True)
class FileSnapshot:
    sha256: str
    size: int
    mtime_ns: int


class FileSnapshotStore:
    """Per-run optimistic-concurrency snapshots keyed by resolved path."""

    def __init__(self) -> None:
        self._snapshots: dict[Path, FileSnapshot] = {}

    @staticmethod
    def snapshot(path: Path, data: bytes) -> FileSnapshot:
        file_stat = path.stat()
        return FileSnapshot(
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            mtime_ns=file_stat.st_mtime_ns,
        )

    def record(self, path: Path, data: bytes) -> FileSnapshot:
        snapshot = self.snapshot(path, data)
        self._snapshots[path.resolve()] = snapshot
        return snapshot

    def get(self, path: Path) -> FileSnapshot | None:
        return self._snapshots.get(path.resolve())


class ReadRangeCache:
    """Per-run record of returned read_file ranges to avoid duplicate pages.

    Keys are ``(resolved path, start offset, line count)``; the stored file
    version is ``(mtime_ns, size)``. A version mismatch invalidates the entry
    so a modified file is always re-read.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, int], tuple[int, int]] = {}

    def already_returned(
        self,
        path: str,
        start: int,
        count: int,
        version: tuple[int, int],
    ) -> bool:
        stored = self._entries.get((path, start, count))
        if stored is None:
            return False
        if stored != version:
            del self._entries[(path, start, count)]
            return False
        return True

    def record(self, path: str, start: int, count: int, version: tuple[int, int]) -> None:
        self._entries[(path, start, count)] = version


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    permission_engine: PermissionEngine | None = None
    approval_handler: ApprovalHandler | None = None
    tracer: TraceSink | None = None
    tool_timeout_seconds: float = 30.0
    file_snapshots: FileSnapshotStore | None = None
    read_ranges: ReadRangeCache = field(default_factory=ReadRangeCache)


@dataclass(frozen=True)
class ToolDescriptor:
    """Stable security and attribution metadata for one tool implementation."""

    source: str = "local"
    source_id: str | None = None
    effect: ToolEffect = "unknown"
    destructive: bool = False
    path_argument: str | None = None
    command_argument: str | None = None
    # 0 表示该工具在 registry 层不做超时控制（例如 sandbox_shell 默认不限时）。
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.effect not in {"read", "write", "remote", "compute", "unknown"}:
            raise ValueError(f"Unsupported tool effect: {self.effect}")
        if self.timeout_seconds is not None and self.timeout_seconds < 0:
            raise ValueError("tool descriptor timeout_seconds must be >= 0")
        if not self.source:
            raise ValueError("tool descriptor source must not be empty")
        if self.path_argument == "":
            raise ValueError("tool descriptor path_argument must not be empty")
        if self.command_argument == "":
            raise ValueError("tool descriptor command_argument must not be empty")


@dataclass(frozen=True)
class ToolFailure:
    """Machine-readable failure details alongside the model-facing message."""

    code: str
    stage: FailureStage
    message: str
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("tool failure code must not be empty")
        if self.stage not in {"lookup", "validate", "authorize", "execute", "postprocess"}:
            raise ValueError(f"Unsupported tool failure stage: {self.stage}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


@dataclass(frozen=True)
class ToolResult:
    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    failure: ToolFailure | None = None

    def __post_init__(self) -> None:
        if self.failure is not None and not self.is_error:
            object.__setattr__(self, "is_error", True)

    @classmethod
    def fail(
        cls,
        message: str,
        *,
        code: str,
        stage: FailureStage,
        retryable: bool = False,
        metadata: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            message,
            is_error=True,
            metadata=dict(metadata or {}),
            failure=ToolFailure(
                code=code,
                stage=stage,
                message=message,
                retryable=retryable,
                detail=dict(detail or {}),
            ),
        )


class Tool(Protocol):
    name: str
    description: str
    parameters: JsonSchema

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute a validated model-requested action."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._inferred_descriptors: set[str] = set()

    def items(self) -> tuple[tuple[str, Tool], ...]:
        return tuple(self._tools.items())

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        descriptor = getattr(tool, "descriptor", None)
        if isinstance(descriptor, ToolDescriptor):
            self._descriptors[tool.name] = descriptor
        else:
            self._descriptors[tool.name] = _legacy_descriptor(tool)
            self._inferred_descriptors.add(tool.name)

    def schemas(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        """Return model-facing schemas for all or a selected set of tools."""
        selected = (
            self._tools.items()
            if names is None
            else ((name, self._tools[name]) for name in self._tools if name in names)
        )
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for _, tool in selected
        ]

    def default_exposed_names(self) -> set[str]:
        """Return tools visible before dynamic MCP tool discovery."""
        return {
            name
            for name in self._tools
            if self.descriptor(name).source != "mcp"
        }

    def subset(self, names: tuple[str, ...]) -> ToolRegistry:
        registry = ToolRegistry()
        for name in names:
            if name not in self._tools:
                raise KeyError(f"unknown tool: {name}")
            registry.register(self._tools[name])
        return registry

    def source(self, name: str) -> str:
        return self.descriptor(name).source

    def descriptor(self, name: str) -> ToolDescriptor:
        return self._descriptors.get(
            name,
            ToolDescriptor(source="unknown", effect="unknown"),
        )

    def descriptor_inferred(self, name: str) -> bool:
        return name not in self._tools or name in self._inferred_descriptors

    def permission_path(self, name: str, arguments: dict[str, Any]) -> str | None:
        descriptor = self.descriptor(name)
        if descriptor.path_argument is not None:
            value = arguments.get(descriptor.path_argument)
            return value if isinstance(value, str) else None
        if self.descriptor_inferred(name):
            return _legacy_permission_path(arguments)
        return None

    def permission_command(self, name: str, arguments: dict[str, Any]) -> str | None:
        descriptor = self.descriptor(name)
        if descriptor.command_argument is None:
            return None
        value = arguments.get(descriptor.command_argument)
        return value if isinstance(value, str) else None

    def attribution(self, name: str) -> dict[str, Any]:
        descriptor = self.descriptor(name)
        data: dict[str, Any] = {
            "source": descriptor.source,
            "effect": descriptor.effect,
            "descriptor_inferred": self.descriptor_inferred(name),
        }
        if descriptor.source_id is not None:
            data["source_id"] = descriptor.source_id
            if descriptor.source == "mcp":
                data["mcp_server"] = descriptor.source_id
        return data

    def is_read_only(self, name: str) -> bool:
        """Return the declared effect class; unknown tools are treated as mutating."""
        return name in self._tools and self.descriptor(name).effect == "read"

    def resources(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> tuple[ResourceAccess, ...]:
        """Resolve invocation resources; unsafe/unknown metadata fails closed."""
        tool = self._tools.get(name)
        if tool is None:
            return (ResourceAccess("*", "write", tree=True),)
        resolver = getattr(tool, "resources", None)
        if callable(resolver):
            try:
                resources = tuple(resolver(arguments, context))
                if resources and all(isinstance(item, ResourceAccess) for item in resources):
                    return resources
            except Exception:
                return (ResourceAccess("*", "write", tree=True),)
            return (ResourceAccess("*", "write", tree=True),)
        if self.is_read_only(name):
            return (ResourceAccess(f"tool:{name}", "read"),)
        return (ResourceAccess("*", "write", tree=True),)

    async def execute(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.fail(
                f"Unknown tool: {name}",
                code="unknown_tool",
                stage="lookup",
            )

        try:
            validate(instance=arguments, schema=tool.parameters)
        except ValidationError as exc:
            return ToolResult.fail(
                f"Invalid arguments for {name}: {exc.message}",
                code="invalid_input",
                stage="validate",
            )
        except SchemaError as exc:
            return ToolResult.fail(
                f"Invalid schema for {name}: {exc.message}",
                code="invalid_schema",
                stage="validate",
            )

        descriptor = self.descriptor(name)
        permission_path = self.permission_path(name, arguments)
        permission_command = self.permission_command(name, arguments)
        try:
            engine = context.permission_engine
            if engine is None:
                engine = PermissionEngine(
                    PermissionContext(
                        mode=PermissionMode.DEFAULT,
                        rules=build_default_rules(),
                        workspace=context.workspace,
                    )
                )
            request = PermissionRequest(
                tool_name=name,
                input=arguments,
                source=descriptor.source,
                effect=descriptor.effect,
                destructive=descriptor.destructive,
                path=permission_path,
                command=permission_command,
            )
            decision = engine.authorize(request)
            allowed = decision.behavior == PermissionBehavior.ALLOW
            if decision.behavior == PermissionBehavior.ASK and context.approval_handler is not None:
                approval = await context.approval_handler.request(request, decision)
                allowed = approval.approved
            if context.tracer is not None:
                context.tracer.emit(
                    "permission_decision",
                    {
                        "tool": name,
                        **self.attribution(name),
                        "requested_action": decision.behavior.value,
                        "allowed": allowed,
                        "reason": decision.reason,
                        "path": permission_path,
                        "command": permission_command,
                        "destructive": descriptor.destructive,
                    },
                )
            if not allowed:
                reason = (
                    "approval was denied"
                    if decision.behavior == PermissionBehavior.ASK
                    and context.approval_handler is not None
                    else decision.reason
                )
                return ToolResult.fail(
                    f"Permission denied for {name}: {reason}",
                    code="permission_denied",
                    stage="authorize",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult.fail(
                f"Authorization failed for {name}: {type(exc).__name__}: {exc}",
                code="authorization_error",
                stage="authorize",
            )

        try:
            timeout = (
                context.tool_timeout_seconds
                if descriptor.timeout_seconds is None
                else descriptor.timeout_seconds
            )
            if timeout is not None and timeout > 0:
                result = await asyncio.wait_for(
                    tool.run(arguments, context),
                    timeout=timeout,
                )
            else:
                result = await tool.run(arguments, context)
        except asyncio.TimeoutError:
            return ToolResult.fail(
                f"{name} timed out after {timeout:g} seconds",
                code="timeout",
                stage="execute",
                retryable=True,
                metadata={"timed_out": True},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep tool failures inside the agent loop
            return ToolResult.fail(
                f"{name} failed: {type(exc).__name__}: {exc}",
                code="tool_error",
                stage="execute",
                detail={"exception_type": type(exc).__name__},
            )
        return _normalize_tool_result(name, result)


def _normalize_tool_result(name: str, result: Any) -> ToolResult:
    if not isinstance(result, ToolResult):
        return ToolResult.fail(
            f"{name} returned an invalid result: expected ToolResult",
            code="invalid_result",
            stage="postprocess",
        )
    if result.is_error and result.failure is None:
        return ToolResult(
            result.output,
            is_error=True,
            metadata=dict(result.metadata),
            failure=ToolFailure(
                code="tool_reported_error",
                stage="execute",
                message=result.output,
            ),
        )
    return result


def _legacy_descriptor(tool: Tool) -> ToolDescriptor:
    name = tool.name
    source = "local"
    source_id = None
    if name.startswith("mcp__"):
        source = "mcp"
        segments = name.split("__", 2)
        source_id = segments[1] if len(segments) > 2 else None
    elif name == "load_skill":
        source = "skill"
    effect: ToolEffect = "read" if bool(getattr(tool, "read_only", False)) else "unknown"
    return ToolDescriptor(source=source, source_id=source_id, effect=effect)


def _legacy_permission_path(arguments: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "root"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None


def _safe_path(workspace: Path, raw_path: str) -> Path:
    root = workspace.resolve()
    candidate = (root / raw_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {raw_path}") from exc
    return candidate


def _is_listable(workspace: Path, path: Path) -> bool:
    """Files/dirs the model may list: no runtime secrets or internal state."""
    if _is_runtime_secret(workspace, path):
        return False
    try:
        relative = path.relative_to(workspace.resolve())
    except ValueError:
        return False
    return not any(
        part in {".git", ".mini-oh", ".pytest_cache", ".ruff_cache", "__pycache__", ".venv"}
        for part in relative.parts
    )


def _is_runtime_secret(workspace: Path, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True
    parts = relative.parts
    if not parts:
        return False
    name = parts[-1]
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return True
    return len(parts) >= 2 and parts[0] == ".mini-oh" and parts[1] == "oauth"
