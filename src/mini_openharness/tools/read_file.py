"""read_file tool: explicit, locatable, paginated reads of UTF-8 workspace files."""

from __future__ import annotations

import asyncio
from typing import Any

from mini_openharness.tools.base import (
    ResourceAccess,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    _is_runtime_secret,
    _safe_path,
)

DEFAULT_READ_LINES = 300
FULL_READ_MAX_LINES = 500


def _count_lines(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _page_header(
    relative: str,
    total: int,
    *,
    start: int,
    count: int,
    more: bool,
    next_offset: int,
) -> str:
    if count > 0:
        lines = f"Lines: {start + 1}-{start + count} of {total}"
    else:
        lines = f"Lines: (none) of {total}"
    return (
        f"File: {relative}\n"
        f"{lines}\n"
        f"More: {'true' if more else 'false'}\n"
        f"Next offset: {next_offset}\n"
    )


class ReadFileTool:
    name = "read_file"
    description = (
        "Read a workspace file.\n"
        "\n"
        "For large files, read only the relevant range using offset and limit.\n"
        "Search or locate the relevant symbol first when possible instead of paging "
        "through an entire large file.\n"
        "\n"
        "The result reports the returned line range, total line count, whether more "
        "content exists, and the next offset.\n"
        "\n"
        "Do not repeatedly request the same unchanged range."
    )
    read_only = True
    descriptor = ToolDescriptor(effect="read", path_argument="path")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Starting line, 0-based. Units are lines, not characters or bytes.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum number of lines to return. Units are lines, not characters or bytes.",
            },
        },
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

        offset = arguments.get("offset")
        limit = arguments.get("limit")
        if offset is not None and offset < 0:
            return ToolResult.fail(
                f"offset must be >= 0, got {offset}",
                code="invalid_input",
                stage="validate",
            )
        if limit is not None and limit <= 0:
            return ToolResult.fail(
                f"limit must be >= 1, got {limit}",
                code="invalid_input",
                stage="validate",
            )

        stat_result = await asyncio.to_thread(path.stat)
        version = (stat_result.st_mtime_ns, stat_result.st_size)
        data = await asyncio.to_thread(path.read_bytes)
        if context.file_snapshots is not None:
            context.file_snapshots.record(path, data)
        total = _count_lines(data)
        start = offset if offset is not None else 0
        if limit is None:
            if offset is None and total <= FULL_READ_MAX_LINES:
                resolved_limit = total
            else:
                resolved_limit = DEFAULT_READ_LINES
        else:
            resolved_limit = limit

        if start < total:
            count = min(resolved_limit, total - start)
        else:
            count = 0
        end = start + count
        more = end < total
        next_offset = end
        relative = str(path.relative_to(context.workspace.resolve()))

        if count == 0:
            if total == 0:
                note = "\n(file is empty)"
            else:
                note = (
                    f"\n(offset {start} is beyond the end of the file; "
                    "no lines returned)"
                )
            return ToolResult(
                _page_header(
                    relative,
                    total,
                    start=start,
                    count=0,
                    more=False,
                    next_offset=start,
                )
                + note
            )

        cache = context.read_ranges
        if cache is not None and cache.already_returned(str(path), start, count, version):
            return ToolResult(
                f"File unchanged. Lines {start + 1}-{end} were already returned "
                "earlier.\n"
                "Use the previous result or request a different range."
            )

        content = data.decode("utf-8")
        lines = content.splitlines(keepends=True)
        body = "".join(lines[start:end])
        if cache is not None:
            cache.record(str(path), start, count, version)
        return ToolResult(
            _page_header(
                relative,
                total,
                start=start,
                count=count,
                more=more,
                next_offset=next_offset,
            )
            + "\n"
            + body
        )

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments["path"]))
        return (ResourceAccess(f"fs:{path}", "read"),)
