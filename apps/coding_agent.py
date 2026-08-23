#!/usr/bin/env python3
"""Coding Agent application built from the shared kernel and an explicit profile."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
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
    system_prompt=(
        "You are the Coding Agent application. Work inside the selected workspace, "
        "inspect relevant code before editing, and verify requested changes."
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
    skills_dir=str(PROJECT_ROOT / "agent_assets" / "coding" / "skills"),
    memory_dir=str(PROJECT_ROOT / "agent_assets" / "coding" / "memory"),
)

APP = AgentApp(CODING_PROFILE)


if __name__ == "__main__":
    raise SystemExit(APP.run())
