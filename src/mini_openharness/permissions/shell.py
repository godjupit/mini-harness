"""Shell-command permission evaluation, separate from general tool rules."""

from __future__ import annotations

import fnmatch
import shlex
from typing import TYPE_CHECKING

from mini_openharness.permissions.types import (
    PermissionAction,
    PermissionDecision,
    PermissionRule,
)

if TYPE_CHECKING:
    from mini_openharness.permissions.policy import PermissionPolicy


def evaluate_command(
    policy: PermissionPolicy,
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
        for rule in policy.rules
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
        action = fallback.action if fallback is not None else policy.default_mutation
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
                    policy.default_mutation,
                    f"no command rule matched subcommand: {subcommand}",
                )
            )
        else:
            decisions.append(_command_decision(rule.action, subcommand, rule))

    return next(
        (decision for action in ("deny", "ask") for decision in decisions if decision.action == action),
        decisions[0],
    )


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
