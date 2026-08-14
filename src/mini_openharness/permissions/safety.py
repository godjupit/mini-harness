"""Safety fallbacks: fail-closed defaults and conservative path/shell checks."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from mini_openharness.permissions.types import (
    PermissionBehavior,
    PermissionContext,
    PermissionRequest,
)


@dataclass(frozen=True)
class SafetyResult:
    safe: bool
    # DENY (hard block) or ASK (escalate) when unsafe. The one exception:
    # a verified routine shell command returns safe=True with ALLOW, so the
    # engine can grant ALLOW without consulting the write default.
    behavior: PermissionBehavior = PermissionBehavior.DENY
    reason: str = ""


def check_safety(
    request: PermissionRequest,
    context: PermissionContext,
) -> SafetyResult:
    if request.command is not None:
        return check_shell_safety(request.command, context.workspace)
    if request.path is not None:
        if context.workspace is None:
            return SafetyResult(
                False,
                PermissionBehavior.DENY,
                "no workspace configured for path safety check",
            )
        return check_path_safety(request.path, context.workspace)
    return SafetyResult(True, reason="")


def check_path_safety(path: str, workspace: Path) -> SafetyResult:
    root = workspace.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        return SafetyResult(
            False,
            PermissionBehavior.DENY,
            f"path escapes workspace: {path}",
        )
    return SafetyResult(True, reason="")


def check_shell_safety(command: str, workspace: Path | None = None) -> SafetyResult:
    if is_dangerous_command(command):
        return SafetyResult(
            False,
            PermissionBehavior.DENY,
            "multi-line shell command is not allowed",
        )
    chains = split_command_chain(command)
    if chains is None:
        return SafetyResult(
            False,
            PermissionBehavior.ASK,
            "shell command cannot be statically verified",
        )
    for argv in chains:
        if not classify_simple_command(argv, workspace):
            return SafetyResult(
                False,
                PermissionBehavior.ASK,
                f"subcommand is not routine-safe: {' '.join(argv)}",
            )
    return SafetyResult(True, PermissionBehavior.ALLOW, "routine shell command")


def resolve_safe_path(path: str, workspace: Path) -> Path:
    root = workspace.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


def is_dangerous_command(command: str) -> bool:
    """Return True for clear injection boundary violations (hard DENY)."""
    return "\n" in command or "\r" in command


def is_complex_command(command: str) -> bool:
    """Return True when shell syntax cannot be statically verified (ASK)."""
    if not command.strip():
        return False
    markers = ("$(", "`", ">", "<", "&", "(", ")", "|", ";")
    if any(marker in command for marker in markers):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        list(lexer)
    except ValueError:
        return True
    return False


ROUTINE_SAFE_COMMANDS = frozenset(
    {"pwd", "ls", "cat", "head", "tail", "wc", "which", "grep", "rg"}
)
GIT_SAFE_SUBCOMMANDS = frozenset({"status", "diff", "log", "show"})


def split_command_chain(command: str) -> list[list[str]] | None:
    """Split a command on ``&&`` into argv lists; ``None`` for unsupported syntax."""
    if not command.strip():
        return []
    if "$(" in command or "`" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    unsupported = {"|", "||", ";", "&", "<", ">", "<<", ">>", "(", ")"}
    chains: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == "&&":
            if not current:
                return None
            chains.append(current)
            current = []
            continue
        if token in unsupported or any(char in token for char in "<>()"):
            return None
        current.append(token)
    if not current:
        return None
    chains.append(current)
    return chains


def classify_simple_command(argv: list[str], workspace: Path | None) -> bool:
    """Return True when a single command is routine-safe (ALLOW-able)."""
    parts = _skip_assignments(argv)
    if not parts:
        return False
    name, args = parts[0], parts[1:]
    if name in ROUTINE_SAFE_COMMANDS:
        return True
    if name == "cd":
        return is_safe_cd(args[0] if args else None, workspace)
    if name in {"python", "python3"}:
        return is_routine_test_command(args)
    if name == "pytest":
        return True
    if name == "npm":
        return len(args) >= 1 and args[0] == "test"
    if name == "cargo":
        return len(args) >= 1 and args[0] == "test"
    if name == "go":
        return len(args) >= 1 and args[0] == "test"
    if name == "git":
        return len(args) >= 1 and args[0] in GIT_SAFE_SUBCOMMANDS
    return False


def is_routine_test_command(args: list[str]) -> bool:
    """Recognize ``python -m pytest`` (the only auto-allowed python form)."""
    return len(args) >= 2 and args[0] == "-m" and args[1] == "pytest"


def is_safe_cd(path: str | None, workspace: Path | None) -> bool:
    """Allow ``cd`` only when the resolved target stays inside the workspace."""
    if not path or workspace is None:
        return False
    root = workspace.resolve()
    if path.startswith("/workspace"):
        relative = path[len("/workspace") :].lstrip("/")
        target = (root / relative).resolve()
    else:
        target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _skip_assignments(argv: list[str]) -> list[str]:
    index = 0
    while index < len(argv) and "=" in argv[index] and " " not in argv[index]:
        index += 1
    return argv[index:]
