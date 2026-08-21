"""Bridge MCP servers into the same local ToolRegistry."""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2
from jsonschema import ValidationError, validate
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mini_openharness.mcp.mcp_auth import McpOAuthConfig, build_oauth_provider
from mini_openharness.tools import ToolContext, ToolDescriptor, ToolRegistry, ToolResult


@dataclass(frozen=True)
class McpServerConfig:
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    oauth: McpOAuthConfig | None = None
    trust_tool_annotations: bool = False
    protocol_mode: str = "auto"


class McpManager:
    """Own MCP client/transport lifecycles and register their tools."""

    def __init__(self, configs: dict[str, McpServerConfig]) -> None:
        self.configs = configs
        self._stacks: list[AsyncExitStack] = []
        self._clients: dict[str, Client] = {}

    @classmethod
    def from_file(cls, path: str | Path) -> "McpManager":
        config_path = Path(path).resolve()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        raw_servers = data.get("mcpServers", data)
        configs: dict[str, McpServerConfig] = {}
        for name, raw in raw_servers.items():
            server_name = str(name)
            cwd = raw.get("cwd")
            if cwd and not Path(cwd).is_absolute():
                cwd = str((config_path.parent / cwd).resolve())
            command = raw.get("command")
            url = raw.get("url")
            if bool(command) == bool(url):
                raise ValueError(
                    f"MCP server {server_name!r} must configure exactly one of command or url"
                )
            headers = {str(key): str(value) for key, value in raw.get("headers", {}).items()}
            for header, env_name in raw.get("headersEnv", {}).items():
                value = os.environ.get(str(env_name))
                if value is None:
                    raise ValueError(
                        f"MCP server {server_name!r} requires environment variable {env_name}"
                    )
                headers[str(header)] = value
            oauth = _oauth_config(
                raw.get("oauth"),
                config_path=config_path,
                server_name=server_name,
            )
            if oauth is not None:
                if not url:
                    raise ValueError(f"MCP server {server_name!r} OAuth requires an HTTP url")
                _validate_oauth_server_url(str(url), server_name)
            protocol_mode = str(raw.get("protocolMode", "auto"))
            if protocol_mode not in {"auto", "legacy"}:
                raise ValueError(
                    f"MCP server {server_name!r} protocolMode must be 'auto' or 'legacy'"
                )
            configs[server_name] = McpServerConfig(
                command=(
                    str(command).replace("{python}", sys.executable) if command else None
                ),
                args=tuple(str(item) for item in raw.get("args", ())),
                env={str(key): str(value) for key, value in raw.get("env", {}).items()},
                cwd=cwd,
                url=str(url) if url else None,
                headers=headers,
                oauth=oauth,
                trust_tool_annotations=bool(raw.get("trustToolAnnotations", False)),
                protocol_mode=protocol_mode,
            )
        return cls(configs)

    async def connect_and_register(self, registry: ToolRegistry) -> list[str]:
        """Connect every configured MCP server and register its tools.

        MCP SDK v2's high-level ``Client`` owns protocol negotiation. In the
        default ``mode='auto'`` it probes the 2026-07-28 ``server/discover``
        path and falls back to the legacy initialize handshake when needed.
        """

        registered: list[str] = []
        for server_name, config in self.configs.items():
            stack = AsyncExitStack()
            try:
                if config.url:
                    auth = build_oauth_provider(config.url, config.oauth) if config.oauth else None
                    http_client = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers=config.headers,
                            auth=auth,
                            follow_redirects=True,
                            timeout=httpx2.Timeout(30, read=300),
                        )
                    )
                    transport = streamable_http_client(
                        config.url,
                        http_client=http_client,
                    )
                else:
                    assert config.command is not None
                    transport = stdio_client(
                        StdioServerParameters(
                            command=config.command,
                            args=list(config.args),
                            env=config.env or None,
                            cwd=config.cwd,
                        )
                    )

                # Client v2 negotiates modern (2026-07-28) vs legacy MCP for us.
                client = await stack.enter_async_context(
                    Client(transport, mode=config.protocol_mode)
                )

                for tool in (await client.list_tools()).tools:
                    adapter = McpTool(
                        server_name=server_name,
                        remote_name=tool.name,
                        description=tool.description or f"MCP tool {tool.name}",
                        parameters=dict(tool.input_schema or {"type": "object"}),
                        output_schema=(
                            dict(tool.output_schema) if tool.output_schema is not None else None
                        ),
                        read_only=bool(
                            config.trust_tool_annotations
                            and tool.annotations is not None
                            and tool.annotations.read_only_hint is True
                        ),
                        client=client,
                    )
                    registry.register(adapter)
                    registered.append(adapter.name)

                self._clients[server_name] = client
                self._stacks.append(stack)
            except BaseException:
                await stack.aclose()
                raise
        return registered

    async def close(self) -> None:
        while self._stacks:
            await self._stacks.pop().aclose()
        self._clients.clear()


class McpTool:
    """Adapt one remote MCP tool to the Mini OpenHarness Tool protocol."""

    def __init__(
        self,
        *,
        server_name: str,
        remote_name: str,
        description: str,
        parameters: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
        read_only: bool = False,
        client: Client,
    ) -> None:
        self.server_name = server_name
        self.remote_name = remote_name
        self.name = f"mcp__{_segment(server_name)}__{_segment(remote_name)}"
        self.description = description
        self.parameters = parameters
        self.output_schema = output_schema
        # MCP annotations are only hints. The manager honors read_only_hint solely
        # when the server is explicitly configured as trusted.
        self.read_only = read_only
        self.descriptor = ToolDescriptor(
            source="mcp",
            source_id=server_name,
            effect="read" if read_only else "remote",
        )
        self.client = client

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        del arguments, context
        from mini_openharness.tools import ResourceAccess

        mode = "read" if self.read_only else "write"
        return (ResourceAccess(f"mcp:{self.server_name}", mode, tree=True),)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        result = await self.client.call_tool(self.remote_name, arguments)
        parts: list[str] = []
        for item in result.content:
            if getattr(item, "type", None) == "text":
                parts.append(getattr(item, "text", ""))
            else:
                parts.append(item.model_dump_json(by_alias=True))

        structured = getattr(result, "structured_content", None)
        is_error = bool(getattr(result, "is_error", False))

        # MCP SDK v2 validates negotiated protocol responses itself. Keep this
        # local check as defense-in-depth and to preserve ToolResult metadata.
        # Tool-level error results are intentionally exempt from output-schema
        # validation by the MCP specification.
        if self.output_schema is not None and not is_error:
            try:
                validate(instance=structured, schema=self.output_schema)
            except ValidationError as exc:
                return ToolResult(
                    f"MCP tool {self.remote_name} returned invalid structured output: "
                    f"{exc.message}",
                    is_error=True,
                    metadata={"output_schema_valid": False},
                )

        if structured is not None and not parts:
            parts.append(json.dumps(structured, ensure_ascii=False))

        metadata: dict[str, Any] = {}
        if structured is not None:
            metadata["structured_content"] = structured
        if self.output_schema is not None and not is_error:
            metadata["output_schema_valid"] = True

        return ToolResult(
            "\n".join(parts) or "(no output)",
            is_error=is_error,
            metadata=metadata,
        )


def _segment(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return result if result and result[0].isalpha() else f"tool_{result or 'unnamed'}"


def _oauth_config(
    raw: Any,
    *,
    config_path: Path,
    server_name: str,
) -> McpOAuthConfig | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"MCP server {server_name!r} oauth must be an object")
    token_file = Path(
        str(raw.get("tokenFile", f".mini-oh/oauth/{_segment(server_name)}.json"))
    )
    if not token_file.is_absolute():
        token_file = (config_path.parent / token_file).resolve()
    return McpOAuthConfig(
        token_file=token_file,
        redirect_uri=str(raw.get("redirectUri", "http://127.0.0.1:8765/callback")),
        scopes=str(raw["scopes"]) if raw.get("scopes") else None,
        client_name=str(raw.get("clientName", "Mini OpenHarness")),
        client_metadata_url=(
            str(raw["clientMetadataUrl"]) if raw.get("clientMetadataUrl") else None
        ),
        open_browser=bool(raw.get("openBrowser", True)),
        timeout_seconds=float(raw.get("timeoutSeconds", 300.0)),
    )


def _validate_oauth_server_url(url: str, server_name: str) -> None:
    parsed = urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parsed.hostname or parsed.fragment or (
        parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)
    ):
        raise ValueError(
            f"MCP server {server_name!r} OAuth url must be HTTPS or HTTP loopback "
            "without a fragment"
        )
