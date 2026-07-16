"""Append-only JSONL traces and safe, side-effect-free replay."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    timestamp: str
    elapsed_ms: int
    kind: str
    data: dict[str, Any]


class TraceSink(Protocol):
    run_id: str

    def emit(self, kind: str, data: Mapping[str, Any] | None = None) -> TraceEvent: ...

    def finish(
        self, *, status: str, data: Mapping[str, Any] | None = None
    ) -> TraceEvent: ...


class TraceWriteError(RuntimeError):
    pass


class LocalJsonlTraceSink:
    """Write one run as JSONL; safe to call from concurrent tool tasks."""

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        redact_secrets: bool = True,
        strict: bool = False,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.run_id = run_id or _new_run_id()
        self.path = self.root / f"{self.run_id}.jsonl"
        self._started = time.monotonic()
        self._sequence = 0
        self._lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._finished_event: TraceEvent | None = None
        self.redact_secrets = redact_secrets
        self.strict = strict
        self.on_error = on_error
        self._disabled = False
        self._reported_error = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor = self._open_append()
            os.close(descriptor)
        except OSError as exc:
            self._handle_write_error(exc)
        self.emit("run_start", {"run_id": self.run_id, **(metadata or {})})

    @property
    def disabled(self) -> bool:
        return self._disabled

    def emit(self, kind: str, data: Mapping[str, Any] | None = None) -> TraceEvent:
        with self._lock:
            self._sequence += 1
            event = TraceEvent(
                sequence=self._sequence,
                timestamp=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=int((time.monotonic() - self._started) * 1000),
                kind=kind,
                data=(
                    _redact(_json_safe(dict(data or {})))
                    if self.redact_secrets
                    else _json_safe(dict(data or {}))
                ),
            )
            if not self._disabled:
                line = (json.dumps(asdict(event), ensure_ascii=False) + "\n").encode("utf-8")
                try:
                    self._append_line(line)
                except OSError as exc:
                    self._handle_write_error(exc)
            return event

    def finish(
        self, *, status: str, data: Mapping[str, Any] | None = None
    ) -> TraceEvent:
        with self._finish_lock:
            if self._finished_event is None:
                self._finished_event = self.emit(
                    "run_end", {"status": status, **dict(data or {})}
                )
            return self._finished_event

    def _open_append(self) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _append_line(self, line: bytes) -> None:
        descriptor = self._open_append()
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("trace write returned zero bytes")
                view = view[written:]
        finally:
            os.close(descriptor)

    def _handle_write_error(self, exc: OSError) -> None:
        message = f"Trace disabled after write failure for {self.path}: {exc}"
        if self.strict:
            raise TraceWriteError(message) from exc
        self._disabled = True
        if self._reported_error:
            return
        self._reported_error = True
        if self.on_error is not None:
            try:
                self.on_error(message)
                return
            except Exception:
                pass
        warnings.warn(message, RuntimeWarning, stacklevel=3)


TraceWriter = LocalJsonlTraceSink


class MemoryTraceSink:
    """In-memory TraceSink for tests and embedding without filesystem writes."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        redact_secrets: bool = True,
    ) -> None:
        self.run_id = run_id or _new_run_id()
        self.events: list[TraceEvent] = []
        self.redact_secrets = redact_secrets
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._finished_event: TraceEvent | None = None
        self.emit("run_start", {"run_id": self.run_id, **dict(metadata or {})})

    def emit(self, kind: str, data: Mapping[str, Any] | None = None) -> TraceEvent:
        with self._lock:
            payload = _json_safe(dict(data or {}))
            event = TraceEvent(
                sequence=len(self.events) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=int((time.monotonic() - self._started) * 1000),
                kind=kind,
                data=_redact(payload) if self.redact_secrets else payload,
            )
            self.events.append(event)
            return event

    def finish(
        self, *, status: str, data: Mapping[str, Any] | None = None
    ) -> TraceEvent:
        with self._finish_lock:
            if self._finished_event is None:
                self._finished_event = self.emit(
                    "run_end", {"status": status, **dict(data or {})}
                )
            return self._finished_event


@dataclass(frozen=True)
class TraceSummary:
    run_id: str
    status: str
    started_at: str
    elapsed_ms: int
    event_count: int
    prompt: str
    path: Path


class TraceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def list(self) -> list[TraceSummary]:
        if not self.root.is_dir():
            return []
        summaries: list[TraceSummary] = []
        for path in self.root.glob("*.jsonl"):
            events = list(self.read(path.stem))
            if not events:
                continue
            start = events[0]
            end = events[-1]
            summaries.append(
                TraceSummary(
                    run_id=path.stem,
                    status=str(end.data.get("status", "running")),
                    started_at=start.timestamp,
                    elapsed_ms=end.elapsed_ms,
                    event_count=len(events),
                    prompt=str(start.data.get("prompt", "")),
                    path=path,
                )
            )
        return sorted(summaries, key=lambda item: item.started_at, reverse=True)

    def read(self, run_id: str) -> Iterable[TraceEvent]:
        if not run_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in run_id
        ):
            raise ValueError("Invalid run id")
        path = self.root / f"{run_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"Trace not found: {run_id}")
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if content and not content.endswith("\n"):
            # A store may inspect an active trace between partial os.write calls.
            # LocalJsonlTraceSink always terminates complete records with a newline.
            lines = lines[:-1]
        for line in lines:
            if not line.strip():
                continue
            payload = json.loads(line)
            yield TraceEvent(
                sequence=int(payload["sequence"]),
                timestamp=str(payload["timestamp"]),
                elapsed_ms=int(payload["elapsed_ms"]),
                kind=str(payload["kind"]),
                data=dict(payload.get("data", {})),
            )

    def replay(self, run_id: str) -> Iterable[str]:
        """Render recorded events only; replay never invokes providers or tools."""
        for event in self.read(run_id):
            yield render_event(event)

    def prune(
        self,
        *,
        older_than_days: float | None = None,
        max_runs: int | None = None,
        dry_run: bool = True,
    ) -> list[Path]:
        if older_than_days is not None and older_than_days <= 0:
            raise ValueError("older_than_days must be positive")
        if max_runs is not None and max_runs < 1:
            raise ValueError("max_runs must be at least 1")
        completed = [summary for summary in self.list() if summary.status != "running"]
        candidates: set[Path] = set()
        if older_than_days is not None:
            cutoff = time.time() - older_than_days * 86_400
            candidates.update(
                summary.path for summary in completed if summary.path.stat().st_mtime < cutoff
            )
        if max_runs is not None:
            candidates.update(summary.path for summary in completed[max_runs:])
        ordered = sorted(candidates, key=lambda path: path.stat().st_mtime)
        if not dry_run:
            for path in ordered:
                path.unlink(missing_ok=True)
        return ordered


def render_event(event: TraceEvent) -> str:
    prefix = f"{event.elapsed_ms / 1000:7.3f}s  {event.kind:20}"
    if event.kind == "assistant_delta":
        detail = repr(event.data.get("text", ""))
    elif event.kind in {"tool_start", "tool_end", "permission_decision"}:
        detail = json.dumps(event.data, ensure_ascii=False)
    else:
        detail = json.dumps(event.data, ensure_ascii=False)
    return f"{prefix} {detail}"


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    return str(value)


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
}
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def _redact(value: Any, *, key: str | None = None) -> Any:
    """Best-effort trace hygiene; permission and storage controls are still required."""
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
        return _OPENAI_KEY_PATTERN.sub("sk-[REDACTED]", value)
    if isinstance(value, dict):
        return {
            str(item_key): _redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_access_token", "_refresh_token")
    )
