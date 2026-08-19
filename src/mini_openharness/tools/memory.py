"""memory tools: write and read explicit long-term memory in the workspace."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_openharness.tools.base import (
    ResourceAccess,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    _safe_path,
)

# Closed taxonomy: these are the only long-term memory types allowed.
MEMORY_TYPES = ("user", "feedback", "project", "reference")

_INDEX_HEADER = (
    "# Memory Index\n\n"
    "MEMORY.md is an index, not a memory. "
    "Long-term memories live in the topic files listed below.\n"
)


def _sanitize_topic(topic: str) -> str | None:
    """Return a safe topic slug, or None when the topic cannot be a filename."""
    if not topic or len(topic) > 80 or "/" in topic or "\\" in topic or ".." in topic:
        return None
    cleaned = re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")
    return cleaned or None


def _topic_filename(memory_type: str, topic: str) -> str | None:
    cleaned = _sanitize_topic(topic)
    if cleaned is None:
        return None
    return f"{memory_type}_{cleaned}.md"


def _topic_title(memory_type: str, topic: str) -> str:
    words = [part.capitalize() for part in f"{memory_type}_{topic}".split("_") if part]
    return " ".join(words)


def _summary(content: str) -> str:
    text = " ".join(content.split())
    return text if len(text) <= 120 else text[:117] + "..."


def _yaml_scalar(value: str) -> str:
    """Quote a value as a safe double-quoted YAML scalar."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_topic_file(
    path: Path,
    name: str,
    description: str,
    memory_type: str,
    content: str,
) -> None:
    """Create a topic file with YAML frontmatter, or append and refresh its description."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"- {date}: {content}\n"
    frontmatter = (
        "---\n"
        f"name: {_yaml_scalar(name)}\n"
        f"description: {_yaml_scalar(description)}\n"
        f"type: {memory_type}\n"
        "---\n"
    )
    if not path.is_file():
        path.write_text(frontmatter + "\n" + entry, encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0] == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line == "---":
                break
            if line.startswith("description:"):
                lines[index] = f"description: {_yaml_scalar(description)}"
                break
    path.write_text("\n".join(lines) + "\n" + entry, encoding="utf-8")


def _upsert_index(memory_dir: Path, filename: str, title: str, content: str) -> None:
    """Add or refresh the topic's line in memdir/MEMORY.md (one line per topic file)."""
    index_path = memory_dir / "MEMORY.md"
    if not index_path.is_file():
        index_path.write_text(_INDEX_HEADER, encoding="utf-8")
    lines = index_path.read_text(encoding="utf-8").splitlines()
    entry = f"- [{title}]({filename}) — {_summary(content)}"
    marker = f"]({filename})"
    for index, line in enumerate(lines):
        if marker in line:
            lines[index] = entry
            break
    else:
        lines.append(entry)
    index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


class MemoryWriteTool:
    name = "memory_write"
    description = (
        "Save a long-term memory into the workspace memdir/ folder. "
        "Call this immediately when the user explicitly asks you to remember "
        "something, or states a durable fact that should survive across sessions. "
        "The memory is written to memdir/{type}_{topic}.md (e.g. user_role.md, "
        "feedback_testing.md, project_release.md), indexed in memdir/MEMORY.md, "
        "and prefixed with a YAML frontmatter block (name, one-line description, type). "
        "type must be one of the four allowed categories: 'user' (role, knowledge "
        "level, goals, preferences), 'feedback' (corrections or approval of how "
        "you work), 'project' (background that cannot be derived from code), "
        "'reference' (where external information lives, e.g. Linear/Slack/Grafana). "
        "topic is a short lowercase slug naming the memory, such as 'role', "
        "'testing', or 'release'."
    )
    read_only = False
    descriptor = ToolDescriptor(effect="write", destructive=False)
    parameters = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(MEMORY_TYPES)},
            "topic": {"type": "string", "minLength": 1},
            "content": {"type": "string", "minLength": 1},
        },
        "required": ["type", "topic", "content"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        memory_type = str(arguments["type"])
        topic = str(arguments["topic"])
        content = " ".join(str(arguments["content"]).split()).strip()
        if not content:
            return ToolResult("content must not be empty", is_error=True)

        filename = _topic_filename(memory_type, topic)
        if filename is None:
            return ToolResult(
                f"topic {topic!r} cannot be turned into a safe filename",
                is_error=True,
            )
        target = _safe_path(context.workspace, f"memdir/{filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        title = _topic_title(memory_type, topic)
        description = _summary(content)
        await asyncio.to_thread(
            _write_topic_file,
            target,
            title,
            description,
            memory_type,
            content,
        )
        await asyncio.to_thread(_upsert_index, target.parent, filename, title, content)
        return ToolResult(
            f"Saved to memdir/{filename}; index updated.",
            metadata={
                "type": memory_type,
                "topic": topic,
                "file": filename,
                "content": content,
            },
        )

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        memory_type = str(arguments.get("type", ""))
        topic = str(arguments.get("topic", ""))
        filename = _topic_filename(memory_type, topic)
        if memory_type not in MEMORY_TYPES or filename is None:
            return (ResourceAccess("*", "write", tree=True),)
        target = _safe_path(context.workspace, f"memdir/{filename}")
        return (ResourceAccess(f"fs:{target}", "write"),)


class MemoryReadTool:
    name = "memory_read"
    description = (
        "Load one topic memory file from the workspace memdir/ folder on demand, "
        "for example memory_read(file='permissions.md'). Only the memdir/MEMORY.md "
        "index is injected at session start; call this tool when the current "
        "question actually needs a topic listed there. Do not read every memory "
        "file up front."
    )
    read_only = True
    descriptor = ToolDescriptor(effect="read", path_argument="file")
    parameters = {
        "type": "object",
        "properties": {
            "file": {"type": "string", "minLength": 1},
        },
        "required": ["file"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        filename = str(arguments["file"])
        if (
            not filename
            or len(filename) > 120
            or "/" in filename
            or "\\" in filename
            or ".." in filename
            or not filename.endswith(".md")
        ):
            return ToolResult(
                "file must be a plain .md filename inside memdir/",
                is_error=True,
            )
        target = _safe_path(context.workspace, f"memdir/{filename}")
        if not target.is_file():
            return ToolResult(f"Memory file not found: memdir/{filename}", is_error=True)
        text = await asyncio.to_thread(target.read_text, encoding="utf-8")
        return ToolResult(text.strip() or "(empty memory file)")

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        filename = str(arguments.get("file", ""))
        if (
            not filename
            or len(filename) > 120
            or "/" in filename
            or "\\" in filename
            or ".." in filename
            or not filename.endswith(".md")
        ):
            return (ResourceAccess("*", "read", tree=True),)
        target = _safe_path(context.workspace, f"memdir/{filename}")
        return (ResourceAccess(f"fs:{target}", "read"),)
