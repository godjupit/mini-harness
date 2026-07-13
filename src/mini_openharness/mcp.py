"""Bridge MCP stdio servers into the same local ToolRegistry."""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mini_openharness.tools import ToolContext, ToolRegistry, ToolResult


@dataclass(frozen=True)
class McpServerConfig:
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


class McpManager:
    """Own MCP process/session lifecycles and register their tools."""

    def __init__(self, configs: dict[str, McpServerConfig]) -> None:
        self.configs = configs
        self._stacks: list[AsyncExitStack] = []
        self._sessions: dict[str, ClientSession] = {}

    @classmethod
    def from_file(cls, path: str | Path) -> "McpManager":
        config_path = Path(path).resolve()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        raw_servers = data.get("mcpServers", data)
        configs: dict[str, McpServerConfig] = {}
        for name, raw in raw_servers.items():
            cwd = raw.get("cwd")
            if cwd and not Path(cwd).is_absolute():
                cwd = str((config_path.parent / cwd).resolve())
            configs[str(name)] = McpServerConfig(
                command=str(raw["command"]).replace("{python}", sys.executable),
                args=tuple(str(item) for item in raw.get("args", ())),
                env={str(key): str(value) for key, value in raw.get("env", {}).items()},
                cwd=cwd,
            )
        return cls(configs)

    async def connect_and_register(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []
        for server_name, config in self.configs.items():
            stack = AsyncExitStack()
            try:
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=config.command,
                            args=list(config.args),
                            env={**os.environ, **config.env},
                            cwd=config.cwd,
                        )
                    )
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                for tool in (await session.list_tools()).tools:
                    adapter = McpTool(
                        server_name=server_name,
                        remote_name=tool.name,
                        description=tool.description or f"MCP tool {tool.name}",
                        parameters=dict(tool.inputSchema or {"type": "object"}),
                        session=session,
                    )
                    registry.register(adapter)
                    registered.append(adapter.name)
                self._sessions[server_name] = session
                self._stacks.append(stack)
            except BaseException:
                await stack.aclose()
                raise
        return registered

    async def close(self) -> None:
        while self._stacks:
            await self._stacks.pop().aclose()
        self._sessions.clear()


class McpTool:
    """Adapt one remote MCP tool to the Mini OpenHarness Tool protocol."""

    read_only = False

    def __init__(
        self,
        *,
        server_name: str,
        remote_name: str,
        description: str,
        parameters: dict[str, Any],
        session: ClientSession,
    ) -> None:
        self.server_name = server_name
        self.remote_name = remote_name
        self.name = f"mcp__{_segment(server_name)}__{_segment(remote_name)}"
        self.description = description
        self.parameters = parameters
        self.session = session

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        result = await self.session.call_tool(self.remote_name, arguments)
        parts: list[str] = []
        for item in result.content:
            if getattr(item, "type", None) == "text":
                parts.append(getattr(item, "text", ""))
            else:
                parts.append(item.model_dump_json())
        structured = getattr(result, "structuredContent", None)
        if structured is not None and not parts:
            parts.append(json.dumps(structured, ensure_ascii=False))
        return ToolResult("\n".join(parts) or "(no output)", is_error=bool(result.isError))


def _segment(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return result if result and result[0].isalpha() else f"tool_{result or 'unnamed'}"
