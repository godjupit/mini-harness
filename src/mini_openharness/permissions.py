"""Rule-based permission decisions with optional human approval."""

from __future__ import annotations

import fnmatch
import json
import shlex
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
    command: str = "*"


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
            return self._evaluate_command(
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

    def _evaluate_command(
        self,
        *,
        tool_name: str,
        path: str,
        command: str,
    ) -> PermissionDecision:
        """Evaluate shell commands per subcommand, failing closed to ``ask``.

        Exact full-command rules may approve syntax that the conservative parser
        cannot analyze. A matching deny rule always takes precedence.
        """
        rules = [
            rule
            for rule in self.rules
            if fnmatch.fnmatch(tool_name, rule.tool)
            and (rule.path == "*" or fnmatch.fnmatch(path, rule.path))
        ]
        denied = next(
            (rule for rule in rules if rule.action == "deny" and _command_matches(command, rule)),
            None,
        )
        if denied is not None:
            return _command_decision("deny", command, denied)

        exact = next(
            (
                rule
                for rule in rules
                if rule.command == command and rule.action in {"allow", "ask"}
            ),
            None,
        )
        if exact is not None:
            return _command_decision(exact.action, command, exact)

        subcommands = _split_shell_command(command)
        if subcommands is None:
            fallback = next((rule for rule in rules if rule.command == "*"), None)
            action = fallback.action if fallback is not None else self.default_mutation
            return PermissionDecision(
                action,
                "shell command is too complex to analyze safely",
                fallback,
            )

        decisions: list[PermissionDecision] = []
        for subcommand in subcommands:
            matching = [rule for rule in rules if _command_matches(subcommand, rule)]
            rule = next((item for item in matching if item.action == "deny"), None)
            if rule is None:
                rule = next((item for item in matching if item.action == "ask"), None)
            if rule is None:
                rule = next((item for item in matching if item.action == "allow"), None)
            if rule is None:
                decisions.append(
                    PermissionDecision(
                        self.default_mutation,
                        f"no command rule matched subcommand: {subcommand}",
                    )
                )
            else:
                decisions.append(_command_decision(rule.action, subcommand, rule))

        return next(
            (decision for action in ("deny", "ask") for decision in decisions if decision.action == action),
            decisions[0],
        )


def extract_path(arguments: dict[str, object]) -> str | None:
    for key in ("path", "file_path", "root"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None


def _command_matches(command: str, rule: PermissionRule) -> bool:
    return fnmatch.fnmatchcase(command, rule.command)


def _command_decision(
    action: PermissionAction,
    command: str,
    rule: PermissionRule,
) -> PermissionDecision:
    return PermissionDecision(
        action,
        f"matched command rule tool={rule.tool} command={rule.command!r} for {command!r}",
        rule,
    )


def _split_shell_command(command: str) -> list[str] | None:
    """Return normalized simple commands or ``None`` for risky shell syntax."""
    if not command.strip() or "\n" in command or "\r" in command or "`" in command or "$(" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None

    separators = {"&&", "||", ";", "|"}
    unsupported = {"&", "<", ">", "<<", ">>", "(", ")"}
    commands: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in unsupported or any(char in token for char in "<>()"):
            return None
        if token in separators:
            if not current:
                return None
            commands.append(shlex.join(current))
            current = []
            continue
        if set(token) <= set(";&|"):
            return None
        current.append(token)
    if not current:
        return None
    commands.append(shlex.join(current))
    return commands
