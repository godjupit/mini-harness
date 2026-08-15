from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mini_openharness.sandbox import (
    BwrapShell,
    SandboxUnavailableError,
    SandboxedShellTool,
)
from mini_openharness.tools import ToolContext


def make_shell(workspace: Path) -> BwrapShell:
    venv_bin = Path(sys.executable).parent
    env = {
        **os.environ,
        "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}",
    }
    return BwrapShell(workspace, env=env)


async def run(
    shell: BwrapShell,
    command: str,
    *,
    cwd: str | None = None,
) -> tuple[str, bool]:
    result = await shell.run(command=command, timeout=60, cwd=cwd)
    return result.output, result.is_error


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_host_python_is_available(tmp_path):
    shell = make_shell(tmp_path)

    output, is_error = asyncio.run(run(shell, "python --version"))

    assert not is_error
    assert output.startswith("Python")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_host_pytest_is_available(tmp_path):
    shell = make_shell(tmp_path)

    output, is_error = asyncio.run(run(shell, "pytest --version"))

    assert not is_error
    assert "pytest" in output


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_workspace_venv_python_runs(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "venv", str(tmp_path / ".venv")],
        check=True,
    )
    shell = make_shell(tmp_path)

    output, is_error = asyncio.run(run(shell, ".venv/bin/python --version"))

    assert not is_error
    assert output.startswith("Python")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_git_status_runs(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    shell = make_shell(tmp_path)

    output, is_error = asyncio.run(run(shell, "git status"))

    assert not is_error
    assert "On branch" in output or "nothing to commit" in output


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_cwd_parameter_sets_working_directory(tmp_path):
    shell = make_shell(tmp_path)

    first, first_error = asyncio.run(run(shell, "mkdir -p subdir", cwd="."))
    assert not first_error
    output, is_error = asyncio.run(run(shell, "pwd", cwd="subdir"))

    assert not is_error
    assert output.strip() == str((tmp_path / "subdir").resolve())


def test_cwd_not_persisted_across_commands(tmp_path):
    shell = make_shell(tmp_path)

    first, first_error = asyncio.run(run(shell, "mkdir -p subdir", cwd="."))
    assert not first_error
    output, is_error = asyncio.run(run(shell, "pwd"))

    assert not is_error
    assert output.strip() == str(tmp_path.resolve())


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_workspace_is_writable(tmp_path):
    shell = make_shell(tmp_path)

    output, is_error = asyncio.run(run(shell, "echo hi > f.txt"))

    assert not is_error
    assert (tmp_path / "f.txt").read_text(encoding="utf-8").strip() == "hi"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_root_filesystem_is_read_only(tmp_path):
    shell = make_shell(tmp_path)

    output, is_error = asyncio.run(run(shell, "touch /etc/mini-oh-perm-test"))

    assert is_error
    assert "Read-only file system" in output or "Permission denied" in output


def test_bwrap_missing_raises_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mini_openharness.sandbox.shutil.which",
        lambda _name: None,
    )
    shell = BwrapShell(tmp_path)

    with pytest.raises(SandboxUnavailableError, match="bwrap"):
        asyncio.run(run(shell, "ls"))


def test_sandbox_tool_metadata():
    assert SandboxedShellTool.descriptor.source == "sandbox"
    assert SandboxedShellTool.descriptor.effect == "compute"
    assert SandboxedShellTool.descriptor.destructive is False
    assert SandboxedShellTool.parameters["required"] == ["command"]


def test_shell_resource_lock_follows_classifier(tmp_path):
    tool = SandboxedShellTool(BwrapShell(tmp_path))
    context = ToolContext(tmp_path)

    assert tool.resources({"command": "ls"}, context)[0].mode == "read"
    assert tool.resources({"command": "cd src && pytest"}, context)[0].mode == "read"
    assert tool.resources({"command": "pip install requests"}, context)[0].mode == "write"
    assert tool.resources({"command": "rm -rf /"}, context)[0].mode == "write"
