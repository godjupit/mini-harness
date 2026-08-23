#!/usr/bin/env python3
"""Gin LookLook Homestay Agent built on the shared Mini OpenHarness kernel."""

from __future__ import annotations

import os
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
            str(PROJECT_ROOT / "examples" / "homestay-mcp.json"),
        )
    )
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return str(configured)


def homestay_workspace() -> Path:
    """Keep Homestay runtime data with the Gin LookLook project by default."""
    configured = os.environ.get("HOMESTAY_WORKSPACE")
    if configured:
        return Path(configured).expanduser().resolve()
    sibling = PROJECT_ROOT.parent / "gin-looklook"
    return sibling.resolve() if sibling.is_dir() else Path.cwd().resolve()


HOMESTAY_PROFILE = AgentProfile(
    name="homestay",
    system_prompt=(PROJECT_ROOT / "examples" / "homestay-system-prompt.md").read_text(
        encoding="utf-8"
    ),
    tool_factory=build_homestay_tools,
    prompt_mode="replace",
    mcp_config=homestay_mcp_config(),
    permission_policy=PermissionPolicy.HUMAN_APPROVAL,
    permission_config=str(PROJECT_ROOT / "examples" / "homestay-permissions.json"),
    max_steps=12,
    output_protocol=MARKDOWN_OUTPUT,
    enable_skills=True,
    enable_memory_prompt=True,
    skills_dir=str(PROJECT_ROOT / "agent_assets" / "homestay" / "skills"),
    memory_dir=str(PROJECT_ROOT / "agent_assets" / "homestay" / "memory"),
)

APP = AgentApp(HOMESTAY_PROFILE)


if __name__ == "__main__":
    raise SystemExit(
        APP.run(
            [
                "--workspace",
                str(homestay_workspace()),
                *sys.argv[1:],
            ]
        )
    )
