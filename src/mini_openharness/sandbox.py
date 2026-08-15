"""Bubblewrap-sandboxed host shell: real host environment, restricted writes."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_openharness.permissions.safety import check_shell_safety
from mini_openharness.permissions.types import PermissionBehavior
from mini_openharness.tools import ResourceAccess, ToolContext, ToolDescriptor, ToolResult


class SandboxUnavailableError(RuntimeError):
    pass


@dataclass
class ShellContext:
    """Session-scoped shell state: env is shared, cwd is not persisted."""

    env: dict[str, str]


class BwrapShell:
    """Run host bash inside bubblewrap; workspace writable, rest read-only.

    Every command is a fresh bash process starting at the workspace root unless
    an explicit ``cwd`` is given. The host environment (python, .venv, git,
    PATH) is shared through ShellContext.
    """

    def __init__(
        self,
        workspace: str | Path,
        env: dict[str, str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.context = ShellContext(env=dict(os.environ if env is None else env))
        self.bwrap = shutil.which("bwrap")

    def ensure_available(self) -> None:
        if self.bwrap is None:
            raise SandboxUnavailableError(
                "bwrap (bubblewrap) is required for sandbox_shell; "
                "install it (e.g. apt install bubblewrap) and retry"
            )

    def _resolve_cwd(self, cwd: str | Path | None) -> Path:
        if cwd is None:
            return self.workspace
        target = (self.workspace / cwd).resolve()
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError(f"cwd escapes workspace: {cwd}") from exc
        return target

    def _argv(self, command: str, cwd: Path) -> list[str]:
        workspace = str(self.workspace)
        return [
            self.bwrap,
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--bind",
            workspace,
            workspace,
            "--dev",
            "/dev",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--die-with-parent",
            "--chdir",
            str(cwd),
            "/bin/bash",
            "-lc",
            command,
        ]

    async def run(
        self,
        *,
        command: str,
        timeout: float,
        cwd: str | Path | None = None,
    ) -> ToolResult:
        self.ensure_available()
        cwd_path = self._resolve_cwd(cwd)
        process = await asyncio.create_subprocess_exec(
            *self._argv(command, cwd_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.context.env,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _stop_process(process)
            return ToolResult(
                f"Sandbox command timed out after {timeout:g} seconds",
                is_error=True,
                metadata={"timed_out": True, "sandbox": "bwrap"},
            )
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        text = output.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if len(text) > 12_000:
            text = text[:12_000] + "\n...[truncated]..."
        return ToolResult(
            text or "(no output)",
            is_error=process.returncode != 0,
            metadata={"returncode": process.returncode, "sandbox": "bwrap"},
        )


class SandboxedShellTool:
    name = "sandbox_shell"
    description = (
        "Run a shell command on the host through a bubblewrap sandbox. "
        "The host environment (python, pytest, git, node, .venv, PATH) is "
        "available directly; only the workspace is writable, the rest of the "
        "filesystem is read-only, and /tmp is a fresh temporary directory. "
        "Each command starts at the workspace root; pass cwd to run elsewhere "
        "(e.g. cwd='123'). The working directory is never persisted across "
        "calls. Use workspace-relative paths (e.g. 123/cli.py)."
    )
    read_only = False
    descriptor = ToolDescriptor(
        source="sandbox",
        # shell 能读写任意东西，但具体安全由 per-command classifier 决定，
        # 不再用静态 write+destructive 标记全部 shell 调用。
        effect="compute",
        destructive=False,
        command_argument="command",
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1},
            "cwd": {"type": "string", "default": "."},
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 600,
                "default": 30,
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, shell: BwrapShell) -> None:
        self.shell = shell

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        command = str(arguments.get("command", ""))
        result = check_shell_safety(command, context.workspace)
        mode = (
            "read"
            if result.safe and result.behavior == PermissionBehavior.ALLOW
            else "write"
        )
        return (
            ResourceAccess(f"fs:{context.workspace.resolve()}", mode, tree=True),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        timeout = min(
            float(arguments.get("timeout_seconds", 30)),
            context.tool_timeout_seconds,
        )
        return await self.shell.run(
            command=str(arguments["command"]),
            timeout=timeout,
            cwd=arguments.get("cwd"),
        )


async def _stop_process(process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()
