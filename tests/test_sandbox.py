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


def make_shell(workspace: Path) -> BwrapShell:
    venv_bin = Path(sys.executable).parent
    env = {
        **os.environ,
        "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}",
    }
    return BwrapShell(workspace, env=env)


async def run(shell: BwrapShell, command: str) -> tuple[str, bool]:
    result = await shell.run(command=command, timeout=60)
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
def test_cwd_persists_across_commands(tmp_path):
    shell = make_shell(tmp_path)

    first, first_error = asyncio.run(run(shell, "mkdir -p subdir && cd subdir"))
    assert not first_error
    output, is_error = asyncio.run(run(shell, "pwd"))

    assert not is_error
    assert output.strip() == str((tmp_path / "subdir").resolve())


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
    assert SandboxedShellTool.descriptor.effect == "write"
    assert SandboxedShellTool.descriptor.destructive is True
    assert SandboxedShellTool.parameters["required"] == ["command"]
