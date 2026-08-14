"""Docker-only sandboxed shell for Mini OpenHarness."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from mini_openharness.tools import ResourceAccess, ToolContext, ToolDescriptor, ToolResult


class SandboxUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerSandboxConfig:
    image: str = "python:3.12-slim"
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 128
    tmpfs_size: str = "64m"
    network: bool = False
    writable: bool = False
    root: bool = False
    persistent: bool = False

    def __post_init__(self) -> None:
        if not self.image.strip():
            raise ValueError("sandbox image cannot be empty")
        if self.cpus <= 0 or self.pids_limit < 1:
            raise ValueError("sandbox resource limits must be positive")


class DockerSandbox:
    """Execute one non-interactive command in one disposable locked-down container."""

    def __init__(self, config: DockerSandboxConfig | None = None) -> None:
        self.config = config or DockerSandboxConfig()
        self.docker = shutil.which("docker")
        self._name: str | None = None

    async def ensure_available(self) -> None:
        if self.docker is None:
            raise SandboxUnavailableError("Docker CLI is required for sandbox shell")
        process = await asyncio.create_subprocess_exec(
            self.docker,
            "image",
            "inspect",
            self.config.image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise SandboxUnavailableError(
                f"Docker image {self.config.image!r} is unavailable; pull it explicitly. {detail}"
            )

    def _container_flags(self, *, workspace: Path, name: str) -> list[str]:
        root = workspace.resolve()
        flags = [
            "--init",
            "--name",
            name,
            "--cpus",
            str(self.config.cpus),
            "--memory",
            self.config.memory,
            "--pids-limit",
            str(self.config.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.config.tmpfs_size}",
            "--mount",
            f"type=bind,src={root},dst=/workspace",
            "--workdir",
            "/workspace",
        ]
        if not self.config.root:
            flags.insert(flags.index("--mount"), "--user")
            flags.insert(
                flags.index("--mount"),
                f"{_host_id('getuid')}:{_host_id('getgid')}",
            )
        if not self.config.network:
            flags.insert(flags.index("--cpus"), "--network")
            flags.insert(flags.index("--cpus"), "none")
        if not self.config.writable:
            flags.insert(flags.index("--tmpfs"), "--read-only")
        for secret in _workspace_secret_files(root):
            destination = Path("/workspace") / secret.relative_to(root)
            flags.extend(
                [
                    "--mount",
                    f"type=bind,src=/dev/null,dst={destination},readonly",
                ]
            )
        oauth_dir = root / ".mini-oh" / "oauth"
        if oauth_dir.exists():
            flags.extend(
                [
                    "--mount",
                    "type=tmpfs,dst=/workspace/.mini-oh/oauth,tmpfs-mode=000",
                ]
            )
        return flags

    def build_argv(self, *, workspace: Path, command: str, name: str) -> list[str]:
        if self.docker is None:
            raise SandboxUnavailableError("Docker CLI is required for sandbox shell")
        return [
            self.docker,
            "run",
            "--rm",
            *self._container_flags(workspace=workspace, name=name),
            self.config.image,
            "/bin/sh",
            "-lc",
            command,
        ]

    def _container_name(self) -> str:
        if self._name is None:
            self._name = f"mini-oh-sandbox-{uuid4().hex[:12]}"
        return self._name

    async def _container_state(self, name: str) -> bool | None:
        process = await asyncio.create_subprocess_exec(
            self.docker,
            "inspect",
            "-f",
            "{{.State.Running}}",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            return None
        return output.decode("utf-8", errors="replace").strip() == "true"

    async def ensure_container(self, workspace: Path) -> None:
        """Lazily create the session-scoped container and start it if stopped."""
        if self.docker is None:
            raise SandboxUnavailableError("Docker CLI is required for sandbox shell")
        name = self._container_name()
        state = await self._container_state(name)
        if state is None:
            argv = [
                self.docker,
                "run",
                "-d",
                *self._container_flags(workspace=workspace, name=name),
                self.config.image,
                "sleep",
                "infinity",
            ]
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                raise SandboxUnavailableError(
                    "failed to create sandbox container: "
                    + stderr.decode("utf-8", errors="replace").strip()
                )
        elif state is False:
            start = await asyncio.create_subprocess_exec(
                self.docker,
                "start",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await start.communicate()
            if start.returncode != 0:
                raise SandboxUnavailableError(
                    "failed to start sandbox container: "
                    + stderr.decode("utf-8", errors="replace").strip()
                )

    async def run(self, *, workspace: Path, command: str, timeout: float) -> ToolResult:
        await self.ensure_available()
        if self.config.persistent:
            await self.ensure_container(workspace)
            name = self._container_name()
            argv = [
                self.docker,
                "exec",
                name,
                "/bin/sh",
                "-lc",
                command,
            ]
            disposable = False
        else:
            name = f"mini-oh-{uuid4().hex[:12]}"
            argv = self.build_argv(workspace=workspace, command=command, name=name)
            disposable = True
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if disposable:
                await self._remove(name)
            await _stop_process(process)
            return ToolResult(
                f"Sandbox command timed out after {timeout:g} seconds",
                is_error=True,
                metadata={"timed_out": True, "sandbox": "docker"},
            )
        except asyncio.CancelledError:
            if disposable:
                await self._remove(name)
            await _stop_process(process)
            raise
        text = output.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
        if len(text) > 12_000:
            text = text[:12_000] + "\n...[truncated]..."
        return ToolResult(
            text or "(no output)",
            is_error=process.returncode != 0,
            metadata={
                "returncode": process.returncode,
                "sandbox": "docker-persistent" if self.config.persistent else "docker",
            },
        )

    async def close(self) -> None:
        """Remove the session-scoped container; no-op for disposable mode."""
        if self.config.persistent and self._name is not None:
            await self._remove(self._name)

    async def _remove(self, name: str) -> None:
        if self.docker is None:
            return
        cleanup = await asyncio.create_subprocess_exec(
            self.docker,
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=5)
        except asyncio.TimeoutError:
            cleanup.kill()
            await cleanup.wait()


class SandboxedShellTool:
    name = "sandbox_shell"
    description = (
        "Run a non-interactive shell command in a disposable Docker container. "
        "Only the workspace is mounted writable at /workspace; network is disabled "
        "unless --sandbox-network is set. The container is disposable per command "
        "unless --sandbox-persistent is used: install and run in the same command, "
        "or use an image that already has your tools. "
        "For read_file/write_file after this tool, convert /workspace/example.txt "
        "to the workspace-relative path example.txt."
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

    def __init__(self, sandbox: DockerSandbox) -> None:
        self.sandbox = sandbox

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
        return await self.sandbox.run(
            workspace=context.workspace,
            command=str(arguments["command"]),
            timeout=timeout,
        )


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


def _host_id(name: str) -> int:
    resolver = getattr(os, name, None)
    return int(resolver()) if callable(resolver) else 1000


def _workspace_secret_files(root: Path) -> list[Path]:
    secrets = []
    for path in root.rglob(".env*"):
        if path.is_file() and path.name != ".env.example" and not any(
            part in {".git", ".venv", "venv", "node_modules"}
            for part in path.relative_to(root).parts
        ):
            secrets.append(path)
    return sorted(secrets)
