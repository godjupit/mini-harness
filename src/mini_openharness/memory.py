"""Small durable-memory store with automatic relevance retrieval."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mini_openharness.tools import ToolContext, ToolResult


@dataclass(frozen=True)
class Memory:
    text: str
    tags: tuple[str, ...] = ()
    created_at: float = 0.0


class MemoryStore:
    """Persist durable facts separately from a verbatim conversation session."""

    def __init__(self, path: str | Path, *, max_records: int = 200) -> None:
        self.path = Path(path)
        self.max_records = max_records

    def add(self, text: str, tags: list[str] | tuple[str, ...] = ()) -> Memory:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("memory text cannot be empty")
        records = self.all()
        memory = Memory(
            clean_text,
            tuple(str(tag).strip() for tag in tags if str(tag).strip()),
            time.time(),
        )
        records = [item for item in records if item.text != clean_text]
        records.append(memory)
        self._save(records[-self.max_records :])
        return memory

    def all(self) -> list[Memory]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Memory file must contain a JSON array")
        return [
            Memory(
                text=str(item["text"]),
                tags=tuple(item.get("tags", ())),
                created_at=float(item.get("created_at", 0.0)),
            )
            for item in data
        ]

    def search(self, query: str, *, limit: int = 5) -> list[Memory]:
        query_tokens = _tokens(query)
        scored: list[tuple[int, float, Memory]] = []
        for memory in self.all():
            haystack = f"{memory.text} {' '.join(memory.tags)}"
            score = len(query_tokens & _tokens(haystack))
            if score:
                scored.append((score, memory.created_at, memory))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[:limit]]

    def prompt(self, query: str) -> str:
        memories = self.search(query)
        if not memories:
            return ""
        return "Relevant durable memories:\n" + "\n".join(f"- {memory.text}" for memory in memories)

    def _save(self, records: list[Memory]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class RememberTool:
    name = "remember"
    description = "Persist one durable user preference, project fact, or decision across sessions."
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        memory = self.store.add(str(arguments["text"]), arguments.get("tags", ()))
        return ToolResult(f"Remembered: {memory.text}")


class SearchMemoryTool:
    name = "search_memory"
    description = "Search durable memories from earlier sessions."
    read_only = True
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        matches = self.store.search(str(arguments["query"]))
        return ToolResult("\n".join(memory.text for memory in matches) or "No relevant memories.")


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese)
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {word for word in words if word}
