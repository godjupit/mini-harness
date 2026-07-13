from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mini_openharness.mcp import McpManager, McpTool
from mini_openharness.tools import ToolContext, ToolRegistry


class FakeSession:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"{name}:{arguments['value']}")],
            structuredContent=None,
            isError=False,
        )


def test_mcp_config_resolves_relative_cwd(tmp_path):
    config = tmp_path / "config" / "mcp.json"
    config.parent.mkdir()
    config.write_text(
        '{"mcpServers":{"demo":{"command":"python","args":[],"cwd":"../workspace"}}}',
        encoding="utf-8",
    )

    manager = McpManager.from_file(config)

    assert manager.configs["demo"].cwd == str((tmp_path / "workspace").resolve())


def test_mcp_adapter_uses_same_permission_and_registry_path(tmp_path):
    registry = ToolRegistry()
    registry.register(
        McpTool(
            server_name="demo",
            remote_name="echo",
            description="echo",
            parameters={"type": "object"},
            session=FakeSession(),
        )
    )

    blocked = asyncio.run(
        registry.execute("mcp__demo__echo", {"value": "hello"}, ToolContext(tmp_path))
    )
    allowed = asyncio.run(
        registry.execute(
            "mcp__demo__echo",
            {"value": "hello"},
            ToolContext(tmp_path, allow_write=True),
        )
    )

    assert blocked.is_error
    assert allowed.output == "echo:hello"
