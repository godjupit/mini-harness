"""Typed tool registry with workspace and mutation boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
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


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    permission_engine: PermissionEngine | None = None
    approval_handler: ApprovalHandler | None = None
    tracer: TraceSink | None = None
    tool_timeout_seconds: float = 30.0
    file_snapshots: FileSnapshotStore | None = None


@dataclass(frozen=True)
class ToolDescriptor:
    """Stable security and attribution metadata for one tool implementation."""

    source: str = "local"
    source_id: str | None = None
    effect: ToolEffect = "unknown"
    destructive: bool = False
    path_argument: str | None = None
    command_argument: str | None = None

    def __post_init__(self) -> None:
        if self.effect not in {"read", "write", "remote", "compute", "unknown"}:
            raise ValueError(f"Unsupported tool effect: {self.effect}")
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

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]
    
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
            result = await asyncio.wait_for(
                tool.run(arguments, context),
                timeout=context.tool_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ToolResult.fail(
                f"{name} timed out after {context.tool_timeout_seconds:g} seconds",
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


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file inside the workspace."
    read_only = True
    descriptor = ToolDescriptor(effect="read", path_argument="path")
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _safe_path(context.workspace, str(arguments["path"]))
        if _is_runtime_secret(context.workspace, path):
            return ToolResult(
                f"Reading protected runtime secret is not allowed: {arguments['path']}",
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(f"File not found: {arguments['path']}", is_error=True)
        data = await asyncio.to_thread(path.read_bytes)
        content = data.decode("utf-8")
        if context.file_snapshots is not None:
            context.file_snapshots.record(path, data)
        return ToolResult(content)

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments["path"]))
        return (ResourceAccess(f"fs:{path}", "read"),)


class ListFilesTool:
    name = "list_files"
    description = "List files below a directory in the workspace."
    read_only = True
    descriptor = ToolDescriptor(effect="read", path_argument="path")
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "default": "."}},
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _safe_path(context.workspace, str(arguments.get("path", ".")))
        if not path.is_dir():
            return ToolResult(f"Directory not found: {arguments.get('path', '.')}", is_error=True)
        files = sorted(
            str(item.relative_to(context.workspace.resolve()))
            for item in path.rglob("*")
            if item.is_file()
            and not _is_runtime_secret(context.workspace, item)
            and not any(
                part in {".git", ".mini-oh", ".pytest_cache", ".ruff_cache", "__pycache__", ".venv"}
                for part in item.relative_to(context.workspace.resolve()).parts
            )
        )
        return ToolResult("\n".join(files[:500]) or "(empty workspace)")

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments.get("path", ".")))
        return (ResourceAccess(f"fs:{path}", "read", tree=True),)


class WriteFileTool:
    name = "write_file"
    description = "Write a UTF-8 text file inside the workspace."
    read_only = False
    descriptor = ToolDescriptor(effect="write", destructive=True, path_argument="path")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _safe_path(context.workspace, str(arguments["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(arguments["content"])
        await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        if context.file_snapshots is not None:
            context.file_snapshots.record(path, content.encode("utf-8"))
        return ToolResult(f"Wrote {len(content.encode('utf-8'))} bytes to {arguments['path']}")

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments["path"]))
        return (ResourceAccess(f"fs:{path}", "write"),)


class EditFileTool:
    name = "edit_file"
    description = (
        "Replace an exact text fragment in a UTF-8 workspace file. Read the file first so "
        "the runtime can reject edits based on stale content."
    )
    read_only = False
    descriptor = ToolDescriptor(effect="write", destructive=True, path_argument="path")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
            "expected_sha256": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{64}$",
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_path = str(arguments["path"])
        try:
            path = _safe_path(context.workspace, raw_path)
        except ValueError as exc:
            return ToolResult.fail(
                str(exc),
                code="invalid_path",
                stage="validate",
            )
        if _is_runtime_secret(context.workspace, path):
            return ToolResult.fail(
                f"Editing protected runtime secret is not allowed: {raw_path}",
                code="protected_file",
                stage="execute",
            )
        if not path.is_file():
            return ToolResult.fail(
                f"File not found: {raw_path}",
                code="file_not_found",
                stage="execute",
            )

        expected_hash = arguments.get("expected_sha256")
        if expected_hash is None and context.file_snapshots is not None:
            snapshot = context.file_snapshots.get(path)
            expected_hash = snapshot.sha256 if snapshot is not None else None
        if expected_hash is None:
            return ToolResult.fail(
                f"Read {raw_path} before editing it, or provide expected_sha256",
                code="file_not_read",
                stage="validate",
            )

        data = await asyncio.to_thread(path.read_bytes)
        current_hash = hashlib.sha256(data).hexdigest()
        if current_hash != str(expected_hash).lower():
            return ToolResult.fail(
                f"File changed since it was read: {raw_path}",
                code="file_changed",
                stage="execute",
                detail={"expected_sha256": expected_hash, "actual_sha256": current_hash},
            )

        content = data.decode("utf-8")
        old_text = str(arguments["old_text"])
        new_text = str(arguments["new_text"])
        occurrences = content.count(old_text)
        if occurrences == 0:
            return ToolResult.fail(
                f"Text to replace was not found in {raw_path}",
                code="match_not_found",
                stage="execute",
            )
        replace_all = bool(arguments.get("replace_all", False))
        if occurrences > 1 and not replace_all:
            return ToolResult.fail(
                f"Text to replace appears {occurrences} times in {raw_path}; use replace_all",
                code="ambiguous_match",
                stage="execute",
                detail={"occurrences": occurrences},
            )

        replacements = occurrences if replace_all else 1
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        updated_data = updated.encode("utf-8")
        mode = stat.S_IMODE(path.stat().st_mode)
        try:
            await asyncio.to_thread(
                _atomic_replace_bytes,
                path,
                updated_data,
                mode,
                current_hash,
            )
        except _FileChangedDuringEdit:
            return ToolResult.fail(
                f"File changed while the edit was being prepared: {raw_path}",
                code="file_changed",
                stage="execute",
            )
        except OSError as exc:
            return ToolResult.fail(
                f"Atomic replace failed for {raw_path}: {exc}",
                code="atomic_replace_failed",
                stage="execute",
                detail={"exception_type": type(exc).__name__},
            )

        if context.file_snapshots is not None:
            snapshot = context.file_snapshots.record(path, updated_data)
            updated_hash = snapshot.sha256
        else:
            updated_hash = hashlib.sha256(updated_data).hexdigest()
        return ToolResult(
            f"Replaced {replacements} occurrence(s) in {raw_path}",
            metadata={"replacements": replacements, "sha256": updated_hash},
        )

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments["path"]))
        return (ResourceAccess(f"fs:{path}", "write"),)


def default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListFilesTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    return registry


class _FileChangedDuringEdit(RuntimeError):
    pass


def _atomic_replace_bytes(
    path: Path,
    data: bytes,
    mode: int,
    expected_sha256: str,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        latest_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if latest_hash != expected_sha256:
            raise _FileChangedDuringEdit
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


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
