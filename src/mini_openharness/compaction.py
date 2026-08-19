"""Deterministic context compaction and large-output artifact storage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_openharness.models import Message
from mini_openharness.tokens import HeuristicCounter, TokenCounter


SUMMARY_PREFIX = "[Compacted conversation summary]"
DEFAULT_THRESHOLD_RATIO = 0.7
DEFAULT_KEEP_RECENT_TOKENS = 12_000

COMPACTION_SYSTEM_PROMPT = """You create a precise handoff summary for a coding agent.
Return plain text only; do not call tools. Preserve the user's requirements,
important decisions, files changed or inspected, tool findings, errors and fixes,
current work state, and remaining work. Be concise but include exact technical
details that a later agent needs to continue safely."""

COMPACTION_USER_PROMPT = """Summarize the earlier conversation above. The summary
will replace that history while recent messages remain available verbatim."""


def estimate_tokens(
    messages: list[Message],
    counter: TokenCounter | None = None,
) -> int:
    """Estimate tokens for a message list.

    Uses the provided real token counter when available; otherwise the
    dependency-free heuristic (characters // 4) keeps behavior unchanged.
    """
    if counter is None:
        characters = 0
        for message in messages:
            characters += len(message.content) + len(message.role)
            for call in message.tool_calls:
                characters += len(call.name) + len(str(call.arguments))
        return max(1, characters // 4)
    total = 0
    for message in messages:
        total += counter.count_tokens(message.content) + counter.count_tokens(message.role)
        for call in message.tool_calls:
            total += counter.count_tokens(call.name) + counter.count_tokens(str(call.arguments))
    return max(1, total)


@dataclass(frozen=True)
class CompactionResult:
    messages: list[Message]
    compacted: bool
    before_tokens: int
    after_tokens: int
    summarized_messages: int = 0
    summary_source: str = "none"
    summary_input_tokens: int = 0
    summary_output_tokens: int = 0
    summary_text: str = ""


class ContextCompactor:
    def __init__(
        self,
        *,
        threshold_tokens: int = 12_000,
        keep_recent_units: int = 1,
        keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
        context_window_tokens: int | None = None,
        threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
        token_counter: TokenCounter | None = None,
    ) -> None:
        if not 0 < threshold_ratio <= 1:
            raise ValueError("threshold_ratio must be in (0, 1]")
        if keep_recent_units < 1:
            raise ValueError("keep_recent_units must be at least 1")
        if keep_recent_tokens <= 0:
            raise ValueError("keep_recent_tokens must be positive")
        self.threshold_tokens = threshold_tokens
        self.keep_recent_units = keep_recent_units
        self.keep_recent_tokens = keep_recent_tokens
        self.context_window_tokens = context_window_tokens
        self.threshold_ratio = threshold_ratio
        self.token_counter = token_counter or HeuristicCounter()

    @property
    def effective_threshold(self) -> int:
        """Compaction threshold: 70% of the model context window when known."""
        if self.context_window_tokens is not None:
            return max(1, int(self.context_window_tokens * self.threshold_ratio))
        return self.threshold_tokens

    def compact(self, messages: list[Message], *, force: bool = False) -> CompactionResult:
        plan = self._plan(messages, force=force)
        if plan is None:
            before = estimate_tokens(messages, self.token_counter)
            return CompactionResult(list(messages), False, before, before)
        system, old_messages, recent_messages, before = plan
        return self._result(
            system,
            old_messages,
            recent_messages,
            before,
            _summarize(old_messages),
            summary_source="deterministic",
        )

    async def compact_with_provider(
        self,
        messages: list[Message],
        provider: Any,
        *,
        force: bool = False,
    ) -> CompactionResult:
        """Use a no-tools model call for a Claude-Code-style handoff summary.

        The deterministic summarizer remains the safe fallback if the summary
        request fails or returns an unusable reply.
        """
        plan = self._plan(messages, force=force)
        if plan is None:
            before = estimate_tokens(messages, self.token_counter)
            return CompactionResult(list(messages), False, before, before)
        system, old_messages, recent_messages, before = plan
        try:
            reply = await provider.complete(
                [Message("system", COMPACTION_SYSTEM_PROMPT)]
                + old_messages
                + [Message("user", COMPACTION_USER_PROMPT)],
                [],
            )
            summary_text = reply.content.strip()
            if summary_text and not reply.tool_calls:
                return self._result(
                    system,
                    old_messages,
                    recent_messages,
                    before,
                    f"{SUMMARY_PREFIX}\n{summary_text}",
                    summary_source="model",
                    summary_input_tokens=reply.input_tokens,
                    summary_output_tokens=reply.output_tokens,
                )
        except Exception:
            # The main run should not become unrecoverable merely because a
            # secondary summary request failed. The original conversation is
            # still represented by the deterministic fallback below.
            pass
        return self._result(
            system,
            old_messages,
            recent_messages,
            before,
            _summarize(old_messages),
            summary_source="deterministic_fallback",
        )

    def _plan(
        self, messages: list[Message], *, force: bool
    ) -> tuple[Message | None, list[Message], list[Message], int] | None:
        before = estimate_tokens(messages, self.token_counter)
        if (not force and before <= self.effective_threshold) or len(messages) < 4:
            return None
        system = messages[0] if messages and messages[0].role == "system" else None
        body = messages[1:] if system else messages[:]
        units = _atomic_units(body)
        recent_units, old_units = self._split_units(units)
        if not old_units:
            return None
        return (
            system,
            [message for unit in old_units for message in unit],
            [message for unit in recent_units for message in unit],
            before,
        )

    def _split_units(
        self,
        units: list[list[Message]],
    ) -> tuple[list[list[Message]], list[list[Message]]]:
        """Split into recent raw units and older summarized units by token budget.

        Scans from the newest unit backwards and keeps units while the running
        estimate stays within ``keep_recent_tokens``. At least
        ``keep_recent_units`` units are always retained, so an oversized latest
        turn (e.g. a huge tool result) is never cut out of the active context.
        """
        recent: list[list[Message]] = []
        tokens = 0
        for unit in reversed(units):
            size = estimate_tokens(unit, self.token_counter)
            if len(recent) >= self.keep_recent_units and tokens + size > self.keep_recent_tokens:
                break
            recent.append(unit)
            tokens += size
        recent.reverse()
        old_units = units[: len(units) - len(recent)]
        return recent, old_units

    def _result(
        self,
        system: Message | None,
        old_messages: list[Message],
        recent_messages: list[Message],
        before: int,
        summary_text: str,
        *,
        summary_source: str,
        summary_input_tokens: int = 0,
        summary_output_tokens: int = 0,
    ) -> CompactionResult:
        compacted = ([system] if system else []) + [Message("system", summary_text)] + recent_messages
        return CompactionResult(
            compacted,
            True,
            before,
            estimate_tokens(compacted, self.token_counter),
            len(old_messages),
            summary_source,
            summary_input_tokens,
            summary_output_tokens,
            summary_text,
        )


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
