"""Rule-based permission decisions with optional human approval."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal


PermissionAction = Literal["allow", "deny", "ask"]
ApprovalCallback = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True)
class PermissionRule:
    action: PermissionAction
    tool: str = "*"
    path: str = "*"


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str
    rule: PermissionRule | None = None


class PermissionPolicy:
    """Evaluate explicit rules first, then safe defaults."""

    def __init__(
        self,
        rules: list[PermissionRule] | None = None,
        *,
        default_mutation: PermissionAction = "ask",
    ) -> None:
        self.rules = list(rules or [])
        self.default_mutation = default_mutation

    @classmethod
    def from_file(cls, path: str | Path) -> "PermissionPolicy":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        default = str(payload.get("default", "ask"))
        if default not in {"allow", "deny", "ask"}:
            raise ValueError("Permission default must be allow, deny, or ask")
        rules = []
        for raw in payload.get("rules", []):
            action = str(raw["action"])
            if action not in {"allow", "deny", "ask"}:
                raise ValueError(f"Invalid permission action: {action}")
            rules.append(
                PermissionRule(
                    action=action,
                    tool=str(raw.get("tool", "*")),
                    path=str(raw.get("path", "*")),
                )
            )
        return cls(rules, default_mutation=default)

    def evaluate(
        self,
        *,
        tool_name: str,
        read_only: bool,
        path: str | None = None,
    ) -> PermissionDecision:
        candidate_path = path or ""
        for rule in self.rules:
            if fnmatch.fnmatch(tool_name, rule.tool) and (
                rule.path == "*" or fnmatch.fnmatch(candidate_path, rule.path)
            ):
                return PermissionDecision(
                    rule.action,
                    f"matched rule tool={rule.tool} path={rule.path}",
                    rule,
                )
        if read_only:
            return PermissionDecision("allow", "read-only tools are allowed")
        source = "MCP tools" if tool_name.startswith("mcp__") else "mutating tools"
        return PermissionDecision(
            self.default_mutation, f"{source} default to {self.default_mutation}"
        )


def extract_path(arguments: dict[str, object]) -> str | None:
    for key in ("path", "file_path", "root"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None
