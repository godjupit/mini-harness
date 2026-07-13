"""Provider-neutral conversation models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        calls = tuple(ToolCall(**call) for call in data.get("tool_calls", ()))
        return cls(
            role=data["role"],
            content=data.get("content", ""),
            tool_calls=calls,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )


@dataclass(frozen=True)
class ModelReply:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
