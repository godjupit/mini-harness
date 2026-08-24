"""Model trace recording helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from mini_openharness.models import Message
from mini_openharness.trace import TraceSink


@dataclass(frozen=True)
class ModelTraceRecorder:
    """Record model lifecycle details when a trace sink is configured."""

    tracer: TraceSink | None

    def record_request(
        self,
        *,
        step: int,
        attempt: int,
        messages: Iterable[Message],
        tools: list[dict[str, Any]],
    ) -> None:
        if self.tracer is None:
            return
        self.tracer.emit(
            "model_request",
            {
                "step": step,
                "attempt": attempt,
                "messages": [message.to_dict() for message in messages],
                "tools": tools,
            },
        )

    def record_first_token(self, data: dict[str, Any]) -> None:
        self._emit("first_token", data)

    def record_assistant_delta(self, text: str) -> None:
        self._emit("assistant_delta", {"text": text})

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.tracer is not None:
            self.tracer.emit(kind, data)
