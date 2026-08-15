"""grep tool: regex content search returning file:line matches."""

from __future__ import annotations

import asyncio
import fnmatch
import re
from pathlib import Path
from typing import Any

from mini_openharness.tools.base import (
    ResourceAccess,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    _is_listable,
    _safe_path,
)

_GREP_MAX_MATCHES = 200
_GREP_MAX_LINE_CHARS = 300


class GrepTool:
    name = "grep"
    description = (
        "Search file contents inside the workspace with a regular expression. "
        "Returns matches as 'file:line: text'. Pass path to restrict the search to "
        "a directory or a single file, and include for a filename glob (e.g. '*.py')."
    )
    read_only = True
    descriptor = ToolDescriptor(effect="read", path_argument="path")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1},
            "path": {"type": "string", "default": "."},
            "include": {"type": "string"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = str(arguments["pattern"])
        raw_path = str(arguments.get("path", "."))
        include = arguments.get("include")
        try:
            root = _safe_path(context.workspace, raw_path)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult(f"Invalid regex: {exc}", is_error=True)
        if root.is_file():
            targets = [root] if _is_listable(context.workspace, root) else []
        elif root.is_dir():
            targets = sorted(
                item
                for item in root.rglob("*")
                if item.is_file()
                and _is_listable(context.workspace, item)
                and (include is None or fnmatch.fnmatch(item.name, str(include)))
            )
        else:
            return ToolResult(f"Path not found: {raw_path}", is_error=True)
        matches = await asyncio.to_thread(
            _grep_files, targets, regex, context.workspace.resolve()
        )
        if not matches:
            return ToolResult(f"(no matches for {pattern!r})")
        output = "\n".join(matches)
        if len(matches) >= _GREP_MAX_MATCHES:
            output += (
                f"\n(matches truncated at {_GREP_MAX_MATCHES}; "
                "narrow the search with path/include)"
            )
        return ToolResult(output)

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        root = _safe_path(context.workspace, str(arguments.get("path", ".")))
        if root.is_file():
            return (ResourceAccess(f"fs:{root}", "read"),)
        return (ResourceAccess(f"fs:{root}", "read", tree=True),)


def _grep_files(
    targets: list[Path],
    regex: re.Pattern[str],
    workspace: Path,
) -> list[str]:
    matches: list[str] = []
    for path in targets:
        try:
            with path.open("rb") as handle:
                head = handle.read(8192)
        except OSError:
            continue
        if b"\x00" in head:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = str(path.relative_to(workspace))
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                display = line.strip()[:_GREP_MAX_LINE_CHARS]
                matches.append(f"{relative}:{lineno}: {display}")
                if len(matches) >= _GREP_MAX_MATCHES:
                    return matches
    return matches
