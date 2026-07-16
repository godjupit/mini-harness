from __future__ import annotations

import asyncio
import os
import socket
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from mcp.client.auth import OAuthFlowError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mini_openharness.mcp_auth import (
    FileOAuthStorage,
    LoopbackOAuthFlow,
    McpOAuthConfig,
    build_oauth_provider,
)
from mini_openharness.mcp import McpManager, McpTool
from mini_openharness.tools import ToolContext, ToolRegistry


class FakeSession:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"{name}:{arguments['value']}")],
            structuredContent=None,
            isError=False,
        )


class StructuredSession:
    def __init__(self, value):
        self.value = value

    async def call_tool(self, name, arguments):
        del name, arguments
        return SimpleNamespace(content=[], structuredContent=self.value, isError=False)


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
    assert blocked.failure.code == "permission_denied"
    assert allowed.output == "echo:hello"


def test_mcp_descriptor_preserves_source_without_name_prefix(tmp_path):
    tool = McpTool(
        server_name="demo",
        remote_name="echo",
        description="echo",
        parameters={"type": "object"},
        session=FakeSession(),
    )
    tool.name = "echo"
    registry = ToolRegistry()
    registry.register(tool)

    assert registry.source("echo") == "mcp"
    assert registry.descriptor("echo").source_id == "demo"
    assert registry.descriptor_inferred("echo") is False
    assert registry.attribution("echo")["mcp_server"] == "demo"


def test_mcp_output_schema_is_validated_and_structured_content_is_preserved(tmp_path):
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    valid = McpTool(
        server_name="demo",
        remote_name="count",
        description="count",
        parameters={"type": "object"},
        output_schema=schema,
        session=StructuredSession({"count": 2}),
    )
    invalid = McpTool(
        server_name="demo",
        remote_name="bad_count",
        description="bad count",
        parameters={"type": "object"},
        output_schema=schema,
        session=StructuredSession({"count": "two"}),
    )

    valid_result = asyncio.run(valid.run({}, ToolContext(tmp_path)))
    invalid_result = asyncio.run(invalid.run({}, ToolContext(tmp_path)))

    assert valid_result.metadata["structured_content"] == {"count": 2}
    assert valid_result.metadata["output_schema_valid"] is True
    assert invalid_result.is_error
    assert invalid_result.metadata["output_schema_valid"] is False


def test_mcp_read_only_annotation_requires_explicit_server_trust(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"trusted":{"command":"python","trustToolAnnotations":true},'
        '"untrusted":{"command":"python"}}}',
        encoding="utf-8",
    )

    manager = McpManager.from_file(config)

    assert manager.configs["trusted"].trust_tool_annotations is True
    assert manager.configs["untrusted"].trust_tool_annotations is False


def test_http_mcp_oauth_config_and_env_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STATIC_TOKEN", "Bearer test-token")
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"remote":{"url":"https://mcp.example.com/mcp",'
        '"headersEnv":{"X-Test-Authorization":"MCP_STATIC_TOKEN"},'
        '"oauth":{"tokenFile":"tokens/remote.json",'
        '"redirectUri":"http://127.0.0.1:9876/callback","scopes":"tools:read"}}}}',
        encoding="utf-8",
    )

    manager = McpManager.from_file(config)
    remote = manager.configs["remote"]

    assert remote.command is None
    assert remote.url == "https://mcp.example.com/mcp"
    assert remote.headers["X-Test-Authorization"] == "Bearer test-token"
    assert remote.oauth is not None
    assert remote.oauth.token_file == (tmp_path / "tokens" / "remote.json").resolve()
    assert remote.oauth.scopes == "tools:read"


def test_oauth_token_storage_is_atomic_and_owner_only(tmp_path):
    storage = FileOAuthStorage(tmp_path / "oauth" / "tokens.json")

    async def round_trip():
        await storage.set_tokens(
            OAuthToken(access_token="access", refresh_token="refresh", expires_in=3600)
        )
        await storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=["http://127.0.0.1:8765/callback"],
                client_id="client-id",
            )
        )
        return await storage.get_tokens(), await storage.get_client_info()

    tokens, client = asyncio.run(round_trip())

    assert tokens is not None and tokens.access_token == "access"
    assert client is not None and client.client_id == "client-id"
    assert stat.S_IMODE(storage.path.stat().st_mode) == 0o600


def test_oauth_token_storage_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "tokens.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="non-symlink"):
        FileOAuthStorage(link)


def test_oauth_remote_server_requires_https(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"bad":{"url":"http://mcp.example.com/mcp",'
        '"oauth":{"redirectUri":"http://127.0.0.1:8765/callback"}}}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="HTTPS"):
        McpManager.from_file(config)


def test_loopback_oauth_callback_captures_code_and_state():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    async def exercise():
        flow = LoopbackOAuthFlow(
            f"http://127.0.0.1:{port}/callback", open_browser=False
        )
        await flow.redirect_handler("https://auth.example/authorize")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /callback?code=abc&state=state-1 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return await flow.callback_handler(), response

    result, response = asyncio.run(exercise())

    assert result == ("abc", "state-1")
    assert b"200 OK" in response


def test_oauth_refuses_authorization_server_without_pkce_s256(tmp_path):
    provider = build_oauth_provider(
        "https://mcp.example.com/mcp",
        McpOAuthConfig(token_file=tmp_path / "token.json", open_browser=False),
    )
    provider.context.oauth_metadata = SimpleNamespace(
        code_challenge_methods_supported=["plain"]
    )

    with pytest.raises(OAuthFlowError, match="PKCE S256"):
        asyncio.run(provider._perform_authorization_code_grant())


def test_real_streamable_http_mcp_transport(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = tmp_path / "http_server.py"
    server.write_text(
        "import os\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('http-eval', host='127.0.0.1', port=int(os.environ['PORT']))\n"
        "@mcp.tool()\n"
        "def echo(text: str) -> str:\n    return 'http:' + text\n"
        "if __name__ == '__main__':\n    mcp.run(transport='streamable-http')\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(server)],
        env={**os.environ, "PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("HTTP MCP server did not start")

        config = tmp_path / "mcp.json"
        config.write_text(
            '{"mcpServers":{"http":{"url":"http://127.0.0.1:'
            + str(port)
            + '/mcp"}}}',
            encoding="utf-8",
        )

        async def exercise():
            registry = ToolRegistry()
            manager = McpManager.from_file(config)
            try:
                names = await manager.connect_and_register(registry)
                result = await registry.execute(
                    "mcp__http__echo",
                    {"text": "works"},
                    ToolContext(tmp_path, allow_write=True),
                )
                return names, result
            finally:
                await manager.close()

        names, result = asyncio.run(exercise())

        assert names == ["mcp__http__echo"]
        assert result.output == "http:works"
    finally:
        process.terminate()
        process.wait(timeout=5)
