"""Shared permission types used by both general tool and shell evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal


PermissionAction = Literal["allow", "deny", "ask"]
ApprovalCallback = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True)
class PermissionRule:
    action: PermissionAction
    tool: str = "*"
    path: str = "*"
    command: str = "*"


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str
    rule: PermissionRule | None = None


def extract_path(arguments: dict[str, object]) -> str | None:
    for key in ("path", "file_path", "root"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None
