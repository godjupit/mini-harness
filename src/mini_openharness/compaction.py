"""Deterministic context compaction and large-output artifact storage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mini_openharness.models import Message


SUMMARY_PREFIX = "[Compacted conversation summary]"


def estimate_tokens(messages: list[Message]) -> int:
    characters = 0
    for message in messages:
        characters += len(message.content) + len(message.role)
        for call in message.tool_calls:
            characters += len(call.name) + len(str(call.arguments))
    return max(1, characters // 4)


@dataclass(frozen=True)
class CompactionResult:
    messages: list[Message]
    compacted: bool
    before_tokens: int
    after_tokens: int
    summarized_messages: int = 0


class ContextCompactor:
    def __init__(self, *, threshold_tokens: int = 12_000, keep_recent_units: int = 6) -> None:
        self.threshold_tokens = threshold_tokens
        self.keep_recent_units = max(1, keep_recent_units)

    def compact(self, messages: list[Message]) -> CompactionResult:
        before = estimate_tokens(messages)
        if before <= self.threshold_tokens or len(messages) < 4:
            return CompactionResult(list(messages), False, before, before)

        system = messages[0] if messages and messages[0].role == "system" else None
        body = messages[1:] if system else messages[:]
        units = _atomic_units(body)
        if len(units) <= self.keep_recent_units:
            return CompactionResult(list(messages), False, before, before)
        old_units = units[: -self.keep_recent_units]
        recent_units = units[-self.keep_recent_units :]
        old_messages = [message for unit in old_units for message in unit]
        summary = Message("system", _summarize(old_messages))
        compacted = (
            ([system] if system else [])
            + [summary]
            + [message for unit in recent_units for message in unit]
        )
        after = estimate_tokens(compacted)
        return CompactionResult(compacted, True, before, after, len(old_messages))


class ArtifactStore:
    def __init__(self, root: str | Path, *, max_inline_chars: int = 8_000) -> None:
        self.root = Path(root).resolve()
        self.max_inline_chars = max_inline_chars

    def offload(self, *, run_id: str, tool_call_id: str, output: str) -> tuple[str, Path | None]:
        if len(output) <= self.max_inline_chars:
            return output, None
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in tool_call_id
        )
        path = directory / f"{safe_id or 'tool-output'}.txt"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(output, encoding="utf-8")
        os.replace(temporary, path)
        head = output[: self.max_inline_chars // 2]
        tail = output[-self.max_inline_chars // 2 :]
        inline = (
            f"{head}\n\n[... {len(output) - len(head) - len(tail)} characters offloaded "
            f"to {path} ...]\n\n{tail}"
        )
        return inline, path


def _atomic_units(messages: list[Message]) -> list[list[Message]]:
    """Keep every assistant tool-call message with all following tool results."""
    units: list[list[Message]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        unit = [message]
        index += 1
        if message.role == "assistant" and message.tool_calls:
            pending = {call.id for call in message.tool_calls}
            while index < len(messages) and messages[index].role == "tool":
                tool_message = messages[index]
                unit.append(tool_message)
                if tool_message.tool_call_id:
                    pending.discard(tool_message.tool_call_id)
                index += 1
            if pending:
                # An incomplete tool turn stays as one indivisible recent unit.
                pass
        units.append(unit)
    return units


def _summarize(messages: list[Message]) -> str:
    lines = [SUMMARY_PREFIX]
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            calls = ", ".join(call.name for call in message.tool_calls)
            lines.append(f"- assistant requested tools: {calls}")
        content = " ".join(message.content.split())
        if content:
            lines.append(f"- {message.role}: {content[:300]}")
    return "\n".join(lines)
