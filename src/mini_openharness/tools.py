"""Typed tool registry with workspace and mutation boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jsonschema import SchemaError, ValidationError, validate

from mini_openharness.permissions import ApprovalCallback, PermissionPolicy, extract_path

if TYPE_CHECKING:
    from mini_openharness.trace import TraceWriter

JsonSchema = dict[str, Any]


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
class ToolContext:
    workspace: Path
    allow_write: bool = False
    permission_policy: PermissionPolicy | None = None
    approval_callback: ApprovalCallback | None = None
    tracer: TraceWriter | None = None
    tool_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class ToolResult:
    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    description: str
    parameters: JsonSchema
    read_only: bool

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute a validated model-requested action."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    def source(self, name: str) -> str:
        if name.startswith("mcp__"):
            return "mcp"
        if name == "load_skill":
            return "skill"
        return "local"

    def is_read_only(self, name: str) -> bool:
        """Return the declared effect class; unknown tools are treated as mutating."""
        tool = self._tools.get(name)
        return bool(tool is not None and tool.read_only)

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
            except (KeyError, TypeError, ValueError):
                pass
        if tool.read_only:
            return (ResourceAccess(f"tool:{name}", "read"),)
        return (ResourceAccess("*", "write", tree=True),)

    async def execute(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        try:
            validate(instance=arguments, schema=tool.parameters)
            policy = context.permission_policy or PermissionPolicy(
                default_mutation="allow" if context.allow_write else "ask"
            )
            decision = policy.evaluate(
                tool_name=name,
                read_only=tool.read_only,
                path=extract_path(arguments),
            )
            allowed = decision.action == "allow"
            if decision.action == "ask" and context.approval_callback is not None:
                allowed = await context.approval_callback(
                    name,
                    f"{decision.reason}; arguments={arguments}",
                )
            if context.tracer is not None:
                context.tracer.emit(
                    "permission_decision",
                    {
                        "tool": name,
                        "source": self.source(name),
                        "requested_action": decision.action,
                        "allowed": allowed,
                        "reason": decision.reason,
                        "path": extract_path(arguments),
                    },
                )
            if not allowed:
                reason = (
                    "approval was denied"
                    if decision.action == "ask" and context.approval_callback is not None
                    else decision.reason
                )
                return ToolResult(f"Permission denied for {name}: {reason}", is_error=True)
            try:
                return await asyncio.wait_for(
                    tool.run(arguments, context),
                    timeout=context.tool_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    f"{name} timed out after {context.tool_timeout_seconds:g} seconds",
                    is_error=True,
                    metadata={"timed_out": True},
                )
        except (ValidationError, SchemaError) as exc:
            return ToolResult(f"Invalid arguments for {name}: {exc.message}", is_error=True)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(f"Invalid arguments for {name}: {exc}", is_error=True)
        except Exception as exc:  # keep tool failures inside the agent loop
            return ToolResult(f"{name} failed: {type(exc).__name__}: {exc}", is_error=True)


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
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return ToolResult(content)

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments["path"]))
        return (ResourceAccess(f"fs:{path}", "read"),)


class ListFilesTool:
    name = "list_files"
    description = "List files below a directory in the workspace."
    read_only = True
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
        return ToolResult(f"Wrote {len(content.encode('utf-8'))} bytes to {arguments['path']}")

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments["path"]))
        return (ResourceAccess(f"fs:{path}", "write"),)


def default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListFilesTool())
    registry.register(WriteFileTool())
    return registry


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
