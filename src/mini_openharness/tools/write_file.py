"""write_file tool: create new files or fully overwrite files."""

from __future__ import annotations

import asyncio
from typing import Any

from mini_openharness.tools.base import (
    ResourceAccess,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    _safe_path,
)


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
