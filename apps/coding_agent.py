#!/usr/bin/env python3
"""Coding Agent application built from the shared kernel and an explicit profile."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
APP_ROOT = PROJECT_ROOT / "apps" / "coding"
DATA_ROOT = APP_ROOT / "data"
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != PROJECT_ROOT]
sys.path.insert(0, str(SRC_ROOT))

from mini_openharness import (  # noqa: E402
    MARKDOWN_OUTPUT,
    AgentApp,
    AgentProfile,
    PermissionPolicy,
    default_tools,
)


CODING_PROFILE = AgentProfile(
    name="coding",
    system_prompt=(APP_ROOT / "config" / "system-prompt.md").read_text(
        encoding="utf-8"
    ),
    tool_factory=default_tools,
    prompt_mode="append",
    permission_policy=PermissionPolicy.AUTO_REVIEW,
    max_steps=30,
    output_protocol=MARKDOWN_OUTPUT,
    enable_sandbox_shell=True,
    enable_skills=True,
    enable_subagents=True,
    enable_memory_prompt=True,
    skills_dir=str(APP_ROOT / "skills"),
    memory_dir=str(APP_ROOT / "memory"),
)

APP = AgentApp(CODING_PROFILE)


def app_arguments(arguments: list[str]) -> list[str]:
    """Apply Coding-owned data paths before user overrides."""
    return [
        "--session-dir",
        str(DATA_ROOT / "sessions"),
        "--trace-dir",
        str(DATA_ROOT / "traces"),
        "--artifact-dir",
        str(DATA_ROOT / "artifacts"),
        *arguments,
    ]


if __name__ == "__main__":
    raise SystemExit(APP.run(app_arguments(sys.argv[1:])))
