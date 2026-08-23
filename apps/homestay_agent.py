#!/usr/bin/env python3
"""Gin LookLook Homestay Agent built on the shared Mini OpenHarness kernel."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
APP_ROOT = PROJECT_ROOT / "apps" / "homestay"
DATA_ROOT = APP_ROOT / "data"
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != PROJECT_ROOT]
sys.path.insert(0, str(SRC_ROOT))

from mini_openharness import (  # noqa: E402
    MARKDOWN_OUTPUT,
    AgentApp,
    AgentProfile,
    PermissionPolicy,
    ToolRegistry,
)
from mini_openharness.tools import ToolSearchTool  # noqa: E402


def build_homestay_tools() -> ToolRegistry:
    """Expose discovery only; business tools are supplied by Homestay MCP."""
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    return registry


def homestay_mcp_config() -> str:
    """Resolve the default production config or an explicit local-dev config."""
    configured = Path(
        os.environ.get(
            "HOMESTAY_MCP_CONFIG",
            str(APP_ROOT / "config" / "homestay-mcp.json"),
        )
    )
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return str(configured)


def homestay_workspace() -> Path:
    """Resolve the Gin LookLook workspace used by Homestay MCP operations."""
    configured = os.environ.get("HOMESTAY_WORKSPACE")
    if configured:
        return Path(configured).expanduser().resolve()
    sibling = PROJECT_ROOT.parent / "gin-looklook"
    return sibling.resolve() if sibling.is_dir() else Path.cwd().resolve()


HOMESTAY_PROFILE = AgentProfile(
    name="homestay",
    system_prompt=(APP_ROOT / "config" / "system-prompt.md").read_text(
        encoding="utf-8"
    ),
    tool_factory=build_homestay_tools,
    prompt_mode="replace",
    mcp_config=homestay_mcp_config(),
    permission_policy=PermissionPolicy.HUMAN_APPROVAL,
    permission_config=str(APP_ROOT / "config" / "homestay-permissions.json"),
    max_steps=12,
    output_protocol=MARKDOWN_OUTPUT,
    enable_skills=True,
    enable_memory_prompt=True,
    skills_dir=str(APP_ROOT / "skills"),
    memory_dir=str(APP_ROOT / "memory"),
)

APP = AgentApp(HOMESTAY_PROFILE)


def app_arguments(arguments: list[str]) -> list[str]:
    """Apply Homestay-owned workspace and data paths before user overrides."""
    return [
        "--workspace",
        str(homestay_workspace()),
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
