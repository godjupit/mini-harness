"""Real bwrap sandbox shell demo: host env, cwd persistence, fs boundaries.

Run from the mini-openharness repo (no API key needed):

    .venv/bin/python examples/sandbox_shell_demo.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mini_openharness.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    build_default_rules,
)
from mini_openharness.sandbox import BwrapShell, SandboxUnavailableError


async def run(shell: BwrapShell, command: str, label: str) -> None:
    result = await shell.run(command=command, timeout=60)
    marker = "✓" if not result.is_error else "✗"
    print(f"{marker} {label}")
    print(f"    command: {command!r}")
    print(f"    output:  {result.output[:200]!r}")
    print(f"    cwd now: {shell.context.cwd}")
    print()


def permission_table(workspace: Path) -> None:
    engine = PermissionEngine(
        PermissionContext(
            mode=PermissionMode.DEFAULT,
            rules=build_default_rules(),
            workspace=workspace,
        )
    )
    print("=== 权限层（sandbox_shell 仍走 PermissionEngine） ===")
    cases = [
        ("ls", "routine → ALLOW"),
        ("cd src && git status", "routine → ALLOW"),
        ("rm -rf x", "destructive → ASK"),
        ("git push", "non-routine git → ASK"),
        ("ls\nrm -rf /", "multi-line → DENY"),
    ]
    for command, expected in cases:
        request = PermissionRequest(
            tool_name="sandbox_shell",
            input={"command": command},
            command=command,
            effect="write",
            source="sandbox",
            destructive=True,
        )
        decision = engine.authorize(request)
        print(f"    {decision.behavior.name:5} {command!r:24} expected: {expected}")
    print()


def main() -> int:
    if shutil.which("bwrap") is None:
        print("bwrap is not installed; install bubblewrap and retry.")
        return 1

    workspace = Path(tempfile.mkdtemp(prefix="bwrap-demo-"))
    print(f"workspace: {workspace}\n")

    # 宿主环境准备：在 workspace 里建 .venv 和 git 仓库
    subprocess.run(
        [sys.executable, "-m", "venv", str(workspace / ".venv")],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)

    venv_bin = Path(sys.executable).parent
    env = {**os.environ, "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}"}
    shell = BwrapShell(workspace, env=env)

    async def demo() -> None:
        print("=== 宿主环境直接可用 ===")
        await run(shell, "python --version", "host python")
        await run(shell, "pytest --version", "host pytest")
        await run(shell, ".venv/bin/python --version", "workspace .venv python")
        await run(shell, "git status", "host git")

        print("=== cwd 跨命令保持 ===")
        await run(shell, "mkdir -p subdir && cd subdir", "cd into subdir")
        await run(shell, "pwd", "pwd after cd")

        print("=== 文件系统边界 ===")
        await run(shell, "echo hi > f.txt", "write inside workspace")
        written = shell.context.cwd / "f.txt"
        print(f"    {written} = {written.read_text(encoding='utf-8').strip()!r}")
        print()
        await run(shell, "touch /etc/mini-oh-demo", "write to /etc (should fail)")

        permission_table(workspace)

    try:
        asyncio.run(demo())
    except SandboxUnavailableError as exc:
        print(f"sandbox unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
