"""Bubblewrap-sandboxed host shell: real host environment, restricted writes."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_openharness.tools import ResourceAccess, ToolContext, ToolDescriptor, ToolResult


class SandboxUnavailableError(RuntimeError):
    pass


@dataclass
class ShellContext:
    """Session-scoped shell state shared across tool calls."""

    cwd: Path
    env: dict[str, str]


class BwrapShell:
    """Run host bash inside bubblewrap; workspace writable, rest read-only.

    Every command is a fresh bash process, but they share one ShellContext, so
    ``cd`` persists and the host environment (python, .venv, git, PATH) is
    reused directly.
    """

    _PWD_MARKER = "__MINI_OH_PWD__="

    def __init__(
        self,
        workspace: str | Path,
        env: dict[str, str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.context = ShellContext(
            cwd=self.workspace,
            env=dict(os.environ if env is None else env),
        )
        self.bwrap = shutil.which("bwrap")

    def ensure_available(self) -> None:
        if self.bwrap is None:
            raise SandboxUnavailableError(
                "bwrap (bubblewrap) is required for sandbox_shell; "
                "install it (e.g. apt install bubblewrap) and retry"
            )

    def _argv(self, command: str) -> list[str]:
        wrapped = (
            f"{command}\n"
            "__mini_oh_rc=$?\n"
            "printf '\\n__MINI_OH_PWD__=%s\\n' \"$PWD\"\n"
            "exit $__mini_oh_rc\n"
        )
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
            str(self.context.cwd),
            "/bin/bash",
            "-lc",
            wrapped,
        ]

    async def run(self, *, command: str, timeout: float) -> ToolResult:
        self.ensure_available()
        process = await asyncio.create_subprocess_exec(
            *self._argv(command),
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
        text, pwd = self._extract_pwd(text)
        if pwd:
            self.context.cwd = Path(pwd)
        if len(text) > 12_000:
            text = text[:12_000] + "\n...[truncated]..."
        return ToolResult(
            text or "(no output)",
            is_error=process.returncode != 0,
            metadata={"returncode": process.returncode, "sandbox": "bwrap"},
        )

    def _extract_pwd(self, text: str) -> tuple[str, str | None]:
        marker = self._PWD_MARKER
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith(marker):
                pwd = line[len(marker) :].strip()
                del lines[index]
                return "\n".join(lines).strip(), pwd or None
        return text, None


class SandboxedShellTool:
    name = "sandbox_shell"
    description = (
        "Run a shell command on the host through a bubblewrap sandbox. "
        "The host environment (python, pytest, git, node, .venv, PATH) is "
        "available directly; only the workspace is writable, the rest of the "
        "filesystem is read-only, and /tmp is a fresh temporary directory. "
        "Each command is a new bash process but the working directory persists "
        "across calls. Use workspace-relative paths (e.g. 123/cli.py)."
    )
    read_only = False
    descriptor = ToolDescriptor(
        source="sandbox",
        effect="write",
        destructive=True,
        command_argument="command",
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1},
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
        del arguments
        return (
            ResourceAccess(f"fs:{context.workspace.resolve()}", "write", tree=True),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        timeout = min(
            float(arguments.get("timeout_seconds", 30)),
            context.tool_timeout_seconds,
        )
        return await self.shell.run(
            command=str(arguments["command"]),
            timeout=timeout,
        )


async def _stop_process(process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()
