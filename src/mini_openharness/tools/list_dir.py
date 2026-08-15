"""list_dir tool: one-level directory listing inside the workspace."""

from __future__ import annotations

from typing import Any

from mini_openharness.tools.base import (
    ResourceAccess,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    _is_listable,
    _safe_path,
)


class ListDirTool:
    name = "list_dir"
    description = (
        "List the files and directories directly inside a directory in the "
        "workspace. Directories are suffixed with '/'. Use find_files to search "
        "recursively for files by name."
    )
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
        entries = []
        for item in sorted(path.iterdir(), key=lambda candidate: candidate.name):
            if not _is_listable(context.workspace, item):
                continue
            relative = str(item.relative_to(context.workspace.resolve()))
            entries.append(relative + ("/" if item.is_dir() else ""))
        return ToolResult("\n".join(entries[:500]) or "(empty directory)")

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments.get("path", ".")))
        return (ResourceAccess(f"fs:{path}", "read", tree=True),)
