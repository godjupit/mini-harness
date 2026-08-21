"""Typed tool registry with workspace and mutation boundaries."""

from mini_openharness.tools.base import (
    FileSnapshot,
    FileSnapshotStore,
    ResourceAccess,
    ResourceLockManager,
    Tool,
    ToolContext,
    ToolDescriptor,
    ToolFailure,
    ToolRegistry,
    ToolResult,
)
from mini_openharness.tools.edit_file import EditFileTool
from mini_openharness.tools.find_files import FindFilesTool
from mini_openharness.tools.grep import GrepTool
from mini_openharness.tools.list_dir import ListDirTool
from mini_openharness.tools.memory import MEMORY_TYPES, MemoryReadTool, MemoryWriteTool
from mini_openharness.tools.read_file import (
    DEFAULT_READ_LINES,
    FULL_READ_MAX_LINES,
    ReadFileTool,
)
from mini_openharness.tools.write_file import WriteFileTool
from mini_openharness.tools.tool_search import ToolSearchTool


def default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListDirTool())
    registry.register(FindFilesTool())
    registry.register(GrepTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(MemoryWriteTool())
    registry.register(MemoryReadTool())
    registry.register(ToolSearchTool(registry))
    return registry


__all__ = [
    "EditFileTool",
    "FileSnapshot",
    "FileSnapshotStore",
    "FindFilesTool",
    "FULL_READ_MAX_LINES",
    "GrepTool",
    "ListDirTool",
    "MEMORY_TYPES",
    "MemoryReadTool",
    "MemoryWriteTool",
    "DEFAULT_READ_LINES",
    "ReadFileTool",
    "ResourceAccess",
    "ResourceLockManager",
    "Tool",
    "ToolContext",
    "ToolDescriptor",
    "ToolFailure",
    "ToolRegistry",
    "ToolResult",
    "ToolSearchTool",
    "WriteFileTool",
    "default_tools",
]
