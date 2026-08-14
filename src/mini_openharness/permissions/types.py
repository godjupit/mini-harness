from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    BYPASS = "bypass"


@dataclass(frozen=True)
class PermissionRule:
    behavior: PermissionBehavior
    tool: str = "*"
    # pattern: fnmatch glob against the request's path (file tools) or command (shell tools); "*" matches anything
    pattern: str = "*"
    source: str = "builtin"


@dataclass
class PermissionRules:
    deny: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)
    allow: list[PermissionRule] = field(default_factory=list)


@dataclass
class PermissionContext:
    mode: PermissionMode
    rules: PermissionRules
    workspace: Path


@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str
    input: dict[str, Any]

    source: str = "local"
    effect: str = "unknown"
    destructive: bool = False

    path: str | None = None
    command: str | None = None


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    reason: str
    matched_rule: PermissionRule | None = None
