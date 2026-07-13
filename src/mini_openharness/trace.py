"""Append-only JSONL traces and safe, side-effect-free replay."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    timestamp: str
    elapsed_ms: int
    kind: str
    data: dict[str, Any]


class TraceWriter:
    """Write one run as JSONL; safe to call from concurrent tool tasks."""

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or _new_run_id()
        self.path = self.root / f"{self.run_id}.jsonl"
        self._started = time.monotonic()
        self._sequence = 0
        self._lock = threading.Lock()
        self._finished_event: TraceEvent | None = None
        self.emit("run_start", {"run_id": self.run_id, **(metadata or {})})

    def emit(self, kind: str, data: dict[str, Any] | None = None) -> TraceEvent:
        with self._lock:
            self._sequence += 1
            event = TraceEvent(
                sequence=self._sequence,
                timestamp=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=int((time.monotonic() - self._started) * 1000),
                kind=kind,
                data=_json_safe(data or {}),
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
            return event

    def finish(self, *, status: str, data: dict[str, Any] | None = None) -> TraceEvent:
        if self._finished_event is None:
            self._finished_event = self.emit("run_end", {"status": status, **(data or {})})
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
        for line in path.read_text(encoding="utf-8").splitlines():
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
