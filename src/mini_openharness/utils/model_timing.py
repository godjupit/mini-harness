"""Timing metrics for one model-provider attempt."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelAttemptTiming:
    """Collect first-activity and response timing for a model attempt."""

    started_at: float = field(default_factory=time.monotonic)
    first_activity_ms: float | None = None
    first_reasoning_ms: float | None = None
    first_text_ms: float | None = None
    first_tool_call_ms: float | None = None

    def _mark_activity(self) -> float:
        elapsed_ms = (time.monotonic() - self.started_at) * 1000
        if self.first_activity_ms is None:
            self.first_activity_ms = elapsed_ms
        return elapsed_ms

    def mark_reasoning(self) -> None:
        elapsed_ms = self._mark_activity()
        if self.first_reasoning_ms is None:
            self.first_reasoning_ms = elapsed_ms

    def mark_text(
        self, *, step: int, attempt: int
    ) -> dict[str, Any] | None:
        """Mark text activity and return first-token timing data once."""
        elapsed_ms = self._mark_activity()
        if self.first_text_ms is not None:
            return None
        self.first_text_ms = elapsed_ms
        return {
            "step": step,
            "attempt": attempt,
            "ttft_ms": self._rounded(self.first_activity_ms),
            "first_activity_ms": self._rounded_optional(self.first_activity_ms),
            "first_text_ms": self._rounded(self.first_text_ms),
        }

    def mark_tool_call(self) -> None:
        elapsed_ms = self._mark_activity()
        if self.first_tool_call_ms is None:
            self.first_tool_call_ms = elapsed_ms

    def mark_activity(self) -> None:
        self._mark_activity()

    def response_data(self) -> dict[str, float | None]:
        total_ms = (time.monotonic() - self.started_at) * 1000
        ttft_ms = (
            self.first_activity_ms
            if self.first_activity_ms is not None
            else total_ms
        )
        generation_ms = max(0.0, total_ms - ttft_ms)
        return {
            "ttft_ms": round(ttft_ms, 1),
            "generation_ms": round(generation_ms, 1),
            "first_activity_ms": self._rounded_optional(self.first_activity_ms),
            "first_reasoning_ms": self._rounded_optional(self.first_reasoning_ms),
            "first_text_ms": self._rounded_optional(self.first_text_ms),
            "first_tool_call_ms": self._rounded_optional(self.first_tool_call_ms),
            "total_ms": round(total_ms, 1),
            "request_to_first_token_ms": round(ttft_ms, 1),
            "first_to_last_token_ms": round(generation_ms, 1),
            "request_total_ms": round(total_ms, 1),
        }

    @staticmethod
    def _rounded(value: float | None) -> float:
        if value is None:
            raise RuntimeError("Timing value has not been recorded")
        return round(value, 1)

    @staticmethod
    def _rounded_optional(value: float | None) -> float | None:
        return round(value, 1) if value is not None else None
