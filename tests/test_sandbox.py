from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess

import pytest

from mini_openharness.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    SandboxedShellTool,
    SandboxUnavailableError,
)
from mini_openharness.tools import ToolContext, ToolRegistry


def test_docker_sandbox_argv_enforces_core_isolation(tmp_path):
    sandbox = DockerSandbox(DockerSandboxConfig(image="alpine:3.20"))
    sandbox.docker = "/usr/bin/docker"

    argv = sandbox.build_argv(workspace=tmp_path, command="echo ok", name="test-box")

    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--pids-limit") + 1] == "128"
    mount = argv[argv.index("--mount") + 1]
    assert mount == f"type=bind,src={tmp_path.resolve()},dst=/workspace"
    assert argv[-3:] == ["/bin/sh", "-lc", "echo ok"]


def test_sandbox_tool_explains_container_to_workspace_path_mapping():
    assert "/workspace/example.txt" in SandboxedShellTool.description
    assert "workspace-relative path example.txt" in SandboxedShellTool.description
    assert SandboxedShellTool.descriptor.source == "sandbox"
    assert SandboxedShellTool.descriptor.effect == "write"
    assert SandboxedShellTool.descriptor.destructive is True


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_real_docker_shell_writes_only_workspace_and_has_no_network(tmp_path):
    outside = tmp_path.parent / "mini-oh-host-secret"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=container-secret", encoding="utf-8")
    oauth = tmp_path / ".mini-oh" / "oauth"
    oauth.mkdir(parents=True)
    (oauth / "remote.json").write_text("oauth-secret", encoding="utf-8")
    sandbox = DockerSandbox(DockerSandboxConfig(image="alpine:3.20"))
    tools = ToolRegistry()
    tools.register(SandboxedShellTool(sandbox))
    before = _mini_container_names()
    command = (
        "printf workspace-ok > created.txt; "
        'test "$(ls /sys/class/net)" = "lo"; '
        "test ! -s .env; "
        "test ! -r .mini-oh/oauth/remote.json; "
        f"test ! -e {shlex.quote(str(outside))}; "
        "if touch /etc/blocked 2>/dev/null; then exit 7; fi; "
        "printf isolated"
    )

    async def exercise():
        try:
            await sandbox.ensure_available()
        except SandboxUnavailableError as exc:
            pytest.skip(str(exc))
        return await tools.execute(
            "sandbox_shell",
            {"command": command, "timeout_seconds": 15},
            ToolContext(tmp_path, allow_write=True, tool_timeout_seconds=20),
        )

    result = asyncio.run(exercise())

    assert not result.is_error, result.output
    assert result.output == "isolated"
    assert result.metadata["sandbox"] == "docker"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "workspace-ok"
    assert outside.read_text(encoding="utf-8") == "secret"
    assert _mini_container_names() == before


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_docker_shell_timeout_removes_container(tmp_path):
    sandbox = DockerSandbox(DockerSandboxConfig(image="alpine:3.20"))
    before = _mini_container_names()

    async def exercise():
        try:
            await sandbox.ensure_available()
        except SandboxUnavailableError as exc:
            pytest.skip(str(exc))
        return await sandbox.run(workspace=tmp_path, command="sleep 5", timeout=0.2)

    result = asyncio.run(exercise())

    assert result.is_error
    assert result.metadata["timed_out"] is True
    assert _mini_container_names() == before


def _mini_container_names() -> set[str]:
    running = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=mini-oh-", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in running.stdout.splitlines() if line}
