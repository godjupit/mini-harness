"""Safety fallbacks: fail-closed defaults and conservative path/shell checks."""

from __future__ import annotations

import shlex
import fnmatch
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
        result = check_path_safety(request.path, context.workspace)
        if not result.safe:
            return result
        if request.effect == "write":
            if is_sensitive_write_path(request.path, context.workspace):
                return SafetyResult(
                    False,
                    PermissionBehavior.ASK,
                    f"sensitive file write: {request.path}",
                )
            return SafetyResult(
                True,
                PermissionBehavior.ALLOW,
                "workspace edit is allowed",
            )
        return SafetyResult(True, reason="")
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
    if chains is not None and len(chains) == 1 and matches_safe_builtin(command):
        return SafetyResult(True, PermissionBehavior.ALLOW, "built-in safe command")
    if chains is None:
        return SafetyResult(
            False,
            PermissionBehavior.ASK,
            "shell command cannot be statically verified",
        )
    verdicts = [classify_simple_command(argv, workspace) for argv in chains]
    if any(verdict == "deny" for verdict in verdicts):
        return SafetyResult(
            False,
            PermissionBehavior.DENY,
            f"destructive subcommand: {' '.join(chains[verdicts.index('deny')])}",
        )
    if all(verdict == "allow" for verdict in verdicts):
        return SafetyResult(True, PermissionBehavior.ALLOW, "routine shell command")
    return SafetyResult(
        False,
        PermissionBehavior.ASK,
        "subcommand is not routine-safe",
    )


def resolve_safe_path(path: str, workspace: Path) -> Path:
    root = workspace.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


def is_dangerous_command(command: str) -> bool:
    """Return True for clear injection boundary violations (hard DENY)."""
    return "\n" in command or "\r" in command


SAFE_COMMANDS = frozenset(
    {"ls", "pwd", "cat", "head", "tail", "grep", "find", "which", "file"}
)
SAFE_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "branch"})
SAFE_PYTHON_PATTERNS = (
    "python --version*",
    "python3 --version*",
    "python -m pytest*",
    "python3 -m pytest*",
)
SAFE_NPM_PATTERNS = ("npm test*", "npm run test*")


def matches_safe_builtin(command: str) -> bool:
    """Layer 4: known read-only / routine commands never enter review."""
    cmd = command.strip()
    if not cmd:
        return False
    tokens = cmd.split()
    if tokens[0] in SAFE_COMMANDS:
        return True
    if tokens[0] == "git" and len(tokens) >= 2:
        return tokens[1] in SAFE_GIT_SUBCOMMANDS
    return any(
        fnmatch.fnmatch(cmd, pattern)
        for pattern in (*SAFE_PYTHON_PATTERNS, *SAFE_NPM_PATTERNS)
    )


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
    {"pwd", "ls", "cat", "head", "tail", "wc", "which", "grep", "rg", "find", "file"}
)
GIT_SAFE_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "branch"})


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
    separators = {"&&", "|"}
    unsupported = {"||", ";", "&", "<", ">", "<<", ">>", "(", ")"}
    chains: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in separators:
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


def classify_simple_command(argv: list[str], workspace: Path | None) -> str:
    """Classify one command: ``allow``, ``ask``, or ``deny``."""
    parts = _skip_assignments(argv)
    if not parts:
        return "ask"
    name, args = parts[0], parts[1:]
    if is_destructive_command(name, args):
        return "deny"
    if name in ROUTINE_SAFE_COMMANDS:
        return "allow"
    if name == "cd":
        return "allow" if is_safe_cd(args[0] if args else None, workspace) else "ask"
    if name in {"python", "python3"}:
        if is_routine_test_command(args) or is_version_command(args):
            return "allow"
        return "ask"
    if name == "pytest":
        return "allow"
    if name == "npm":
        return "allow" if is_npm_test(args) else "ask"
    if name == "cargo":
        return "allow" if args and args[0] == "test" else "ask"
    if name == "go":
        return "allow" if args and args[0] == "test" else "ask"
    if name == "git":
        return "allow" if args and args[0] in GIT_SAFE_SUBCOMMANDS else "ask"
    return "ask"


def is_routine_test_command(args: list[str]) -> bool:
    """Recognize ``python -m pytest`` (the only auto-allowed python form)."""
    return len(args) >= 2 and args[0] == "-m" and args[1] == "pytest"


def is_version_command(args: list[str]) -> bool:
    return args in (["--version"], ["-V"])


def is_npm_test(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "test":
        return True
    return args[0] == "run" and len(args) >= 2 and args[1] == "test"


def is_destructive_command(name: str, args: list[str]) -> bool:
    """Hard DENY for clearly destructive invocations (e.g. ``rm -rf /``)."""
    if name not in {"rm", "rmdir"}:
        return False
    recursive = any(flag in {"-rf", "-fr"} for flag in args)
    if not recursive:
        return False
    return any(
        target in {"/", "/*"}
        for target in args
        if not target.startswith("-")
    )


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


SENSITIVE_WRITE_NAMES = frozenset(
    {".npmrc", ".pypirc", ".pypirc", "credentials", ".credentials"}
)


def is_sensitive_write_path(path: str, workspace: Path) -> bool:
    """Layer 5: sensitive writes (secrets/config) stay ASK; plain edits ALLOW."""
    try:
        relative = (workspace / path).resolve().relative_to(workspace.resolve())
    except ValueError:
        return True  # outside the workspace is DENY'd by check_path_safety
    parts = relative.parts
    if not parts:
        return True
    name = parts[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if ".git" in parts or ".github" in parts:
        return True
    if name in SENSITIVE_WRITE_NAMES:
        return True
    if any(part in {"secrets", "credentials"} for part in parts):
        return True
    return False


def _skip_assignments(argv: list[str]) -> list[str]:
    index = 0
    while index < len(argv) and "=" in argv[index] and " " not in argv[index]:
        index += 1
    return argv[index:]
