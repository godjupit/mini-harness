"""Rule-based permission decisions for non-shell tools."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from mini_openharness.permissions.shell import evaluate_command
from mini_openharness.permissions.types import (
    PermissionAction,
    PermissionDecision,
    PermissionRule,
)


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
                    command=str(raw.get("command", "*")),
                )
            )
        return cls(rules, default_mutation=default)

    def evaluate(
        self,
        *,
        tool_name: str,
        read_only: bool,
        path: str | None = None,
        source: str = "local",
        command: str | None = None,
    ) -> PermissionDecision:
        candidate_path = path or ""
        if command is not None:
            return evaluate_command(
                self,
                tool_name=tool_name,
                path=candidate_path,
                command=command,
            )
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
        category = "MCP tools" if source == "mcp" else "mutating tools"
        return PermissionDecision(
            self.default_mutation, f"{category} default to {self.default_mutation}"
        )
