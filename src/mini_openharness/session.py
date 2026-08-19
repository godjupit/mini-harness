"""Append-only JSONL session logs and Claude-Code-style resume detection."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from mini_openharness.models import Message


class Interruption(str, Enum):
    """Where a session ended, mirroring Claude Code's detectTurnInterruption."""

    COMPLETED = "completed"
    INTERRUPTED_PROMPT = "interrupted_prompt"
    INTERRUPTED_TURN = "interrupted_turn"
    DANGLING_TOOL_CALLS = "dangling_tool_calls"


def detect_interruption(messages: list[Message]) -> Interruption:
    """Classify a session by its last non-system message."""
    relevant = [message for message in messages if message.role != "system"]
    if not relevant:
        return Interruption.INTERRUPTED_PROMPT
    last = relevant[-1]
    if last.role == "user":
        return Interruption.INTERRUPTED_PROMPT
    if last.role == "tool":
        return Interruption.INTERRUPTED_TURN
    if last.tool_calls:
        resolved = {message.tool_call_id for message in relevant if message.role == "tool"}
        if any(call.id not in resolved for call in last.tool_calls):
            return Interruption.DANGLING_TOOL_CALLS
        return Interruption.INTERRUPTED_TURN
    return Interruption.COMPLETED


def strip_dangling_tool_calls(messages: list[Message]) -> list[Message]:
    """Drop every unresolved tool_call so a resumed transcript is valid.

    Mirrors Claude Code's filterUnresolvedToolUses: a tool_use with no tool_result
    must not reach the model. If stripping leaves an empty assistant message it is
    removed entirely.
    """
    resolved = {message.tool_call_id for message in messages if message.role == "tool"}
    sanitized: list[Message] = []
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            sanitized.append(message)
            continue
        calls = tuple(call for call in message.tool_calls if call.id in resolved)
        if calls == message.tool_calls:
            sanitized.append(message)
        elif message.content or calls:
            sanitized.append(Message("assistant", message.content, tool_calls=calls))
    return sanitized


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    first_prompt: str
    created_at: str
    message_count: int
    path: Path


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    messages: tuple[Message, ...]
    meta: dict[str, Any]
    path: Path


class SessionLog:
    """Append-only JSONL of conversation messages, one Message per line.

    A meta header line is written on creation; resume appends further messages to
    the same file via open_existing without rewriting that header.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.session_id = session_id or uuid4().hex
        self.path = _session_path(self.root, self.session_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self._append_line(
            json.dumps(
                {
                    "type": "meta",
                    "session_id": self.session_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **(metadata or {}),
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

    @classmethod
    def open_existing(cls, root: str | Path, session_id: str) -> "SessionLog":
        """Open an existing session file for appending, keeping its meta header."""
        instance = cls.__new__(cls)
        instance.root = Path(root).resolve()
        instance.session_id = session_id
        instance.path = _session_path(instance.root, session_id)
        if not instance.path.is_file():
            raise FileNotFoundError(f"Session not found: {session_id}")
        return instance

    def append_message(self, message: Message) -> None:
        self._append_line(
            json.dumps(
                {"type": "message", "message": message.to_dict()},
                ensure_ascii=False,
            ).encode("utf-8")
        )

    def append_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Append a non-message audit event (e.g. subagent_start / subagent_end)."""
        self._append_line(
            json.dumps(
                {"type": event_type, **data},
                ensure_ascii=False,
            ).encode("utf-8")
        )

    def append_compaction(
        self,
        *,
        summary: str,
        replaced_messages: int,
        before_tokens: int,
        after_tokens: int,
        summary_source: str,
    ) -> None:
        """Persist a context compaction so resume reconstructs the same state."""
        self._append_line(
            json.dumps(
                {
                    "type": "compaction",
                    "summary": summary,
                    "replaced": replaced_messages,
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "summary_source": summary_source,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

    def _append_line(self, line: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            # Records are newline-terminated so _parse can split lines and a
            # process killed mid-write leaves only a harmless trailing half-line.
            view = memoryview(line + b"\n")
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("session write returned zero bytes")
                view = view[written:]
        finally:
            os.close(descriptor)


class SessionStore:
    """Read and enumerate session logs; tolerates a killed process's half line."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def list(self) -> list[SessionSummary]:
        if not self.root.is_dir():
            return []
        summaries: list[SessionSummary] = []
        for path in self.root.glob("*.jsonl"):
            try:
                meta, messages = self._parse(path)
            except OSError:
                continue
            if not meta and not messages:
                # A file with no readable records is not a session; a session
                # created but never written to still has its meta header.
                continue
            summaries.append(
                SessionSummary(
                    session_id=path.stem,
                    first_prompt=_first_user_text(messages) or str(meta.get("first_prompt", "")),
                    created_at=_created_at(path),
                    message_count=len(messages),
                    path=path,
                )
            )
        return sorted(summaries, key=lambda item: item.created_at, reverse=True)

    def latest(self) -> SessionSummary | None:
        sessions = self.list()
        return sessions[0] if sessions else None

    def read(self, session_id: str) -> SessionRecord:
        path = _session_path(self.root, session_id)
        if not path.is_file():
            raise FileNotFoundError(f"Session not found: {session_id}")
        meta, messages = self._parse(path)
        return SessionRecord(
            session_id=session_id,
            messages=tuple(messages),
            meta=meta,
            path=path,
        )

    def _parse(self, path: Path) -> tuple[dict[str, Any], list[Message]]:
        meta: dict[str, Any] = {}
        messages: list[Message] = []
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    # A process may have been killed mid-write; a trailing partial
                    # line is ignored. Complete records always end with a newline.
                    continue
                if payload.get("type") == "meta":
                    meta = dict(payload)
                elif payload.get("type") == "message":
                    try:
                        messages.append(Message.from_dict(payload.get("message", {})))
                    except (KeyError, TypeError):
                        continue
                elif payload.get("type") == "compaction":
                    summary = str(payload.get("summary") or "")
                    try:
                        replaced = int(payload.get("replaced", 0))
                    except (TypeError, ValueError):
                        replaced = 0
                    _apply_compaction(messages, summary, replaced)
        return meta, messages


def _created_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _first_user_text(messages: list[Message]) -> str:
    return next(
        (message.content for message in messages if message.role == "user"),
        "",
    )


def _session_path(root: Path, session_id: str) -> Path:
    """Return a session path without allowing callers to escape ``root``."""
    if not session_id or not session_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("session id may contain only letters, digits, '-' and '_'")
    return root / f"{session_id}.jsonl"


def _apply_compaction(messages: list[Message], summary: str, replaced: int) -> None:
    """Replace the oldest ``replaced`` body messages with a summary system message."""
    if replaced <= 0 or not summary:
        return
    if messages and messages[0].role == "system":
        head, body = messages[:1], messages[1:]
    else:
        head, body = [], list(messages)
    replaced = min(replaced, len(body))
    if replaced <= 0:
        return
    messages[:] = head + [Message("system", summary)] + body[replaced:]
