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
    behavior: PermissionBehavior = PermissionBehavior.DENY
    reason: str = ""


def check_safety(
    request: PermissionRequest,
    context: PermissionContext,
) -> SafetyResult:
    if request.command is not None:
        return check_shell_safety(request.command)
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


def check_shell_safety(command: str) -> SafetyResult:
    if is_dangerous_command(command):
        return SafetyResult(
            False,
            PermissionBehavior.DENY,
            "multi-line shell command is not allowed",
        )
    if is_complex_command(command):
        return SafetyResult(
            False,
            PermissionBehavior.ASK,
            "shell command cannot be statically verified",
        )
    return SafetyResult(True, reason="")


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
