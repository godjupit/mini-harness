"""find_files tool: recursive filename-glob search inside the workspace."""

from __future__ import annotations

import fnmatch
from typing import Any

from mini_openharness.tools.base import (
    ResourceAccess,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    _is_listable,
    _safe_path,
)


class FindFilesTool:
    name = "find_files"
    description = (
        "Recursively search for files inside the workspace whose name matches a "
        "glob pattern (e.g. 'cli.py' or '*.py')."
    )
    read_only = True
    descriptor = ToolDescriptor(effect="read", path_argument="path")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "pattern": {"type": "string", "minLength": 1},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = _safe_path(context.workspace, str(arguments.get("path", ".")))
        if not root.is_dir():
            return ToolResult(
                f"Directory not found: {arguments.get('path', '.')}",
                is_error=True,
            )
        pattern = str(arguments["pattern"])
        matches = sorted(
            str(item.relative_to(context.workspace.resolve()))
            for item in root.rglob("*")
            if item.is_file()
            and fnmatch.fnmatch(item.name, pattern)
            and _is_listable(context.workspace, item)
        )
        return ToolResult(
            "\n".join(matches[:500]) or f"(no files match {pattern!r})"
        )

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        root = _safe_path(context.workspace, str(arguments.get("path", ".")))
        return (ResourceAccess(f"fs:{root}", "read", tree=True),)
