"""OAuth 2.1 helpers for remote MCP servers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, OAuthFlowError
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)


@dataclass(frozen=True)
class McpOAuthConfig:
    token_file: Path
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    scopes: str | None = None
    client_name: str = "Mini OpenHarness"
    client_metadata_url: str | None = None
    open_browser: bool = True
    timeout_seconds: float = 300.0


class FileOAuthStorage:
    """SDK token storage using an atomic owner-readable JSON file."""

    def __init__(self, path: str | Path) -> None:
        candidate = Path(path).expanduser()
        self.path = candidate if candidate.is_absolute() else (Path.cwd() / candidate).absolute()
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError("MCP OAuth token path must be a regular non-symlink file")
            os.chmod(self.path, 0o600)
        self._lock = asyncio.Lock()

    async def get_tokens(self) -> OAuthToken | None:
        payload = await self._read()
        raw = payload.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await self._update("tokens", tokens.model_dump(mode="json", by_alias=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        payload = await self._read()
        raw = payload.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._update(
            "client_info", client_info.model_dump(mode="json", by_alias=True)
        )

    async def _read(self) -> dict[str, Any]:
        async with self._lock:
            return self._read_sync()

    def _read_sync(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("MCP OAuth token path must be a regular non-symlink file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("MCP OAuth token file must contain a JSON object")
        return payload

    async def _update(self, key: str, value: Any) -> None:
        async with self._lock:
            self._update_sync(key, value)

    def _update_sync(self, key: str, value: Any) -> None:
        payload = self._read_sync()
        payload[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(raw_temporary)
        try:
            os.chmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        os.chmod(self.path, 0o600)


class LoopbackOAuthFlow:
    """Receive one OAuth callback on an explicitly configured loopback URI."""

    def __init__(
        self,
        redirect_uri: str,
        *,
        open_browser: bool = True,
        timeout_seconds: float = 300.0,
    ) -> None:
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MCP OAuth redirect_uri must be an http loopback address")
        if parsed.port is None:
            raise ValueError("MCP OAuth redirect_uri must include an explicit port")
        self.redirect_uri = redirect_uri
        self.host = parsed.hostname
        self.port = parsed.port
        self.path = parsed.path or "/"
        self.open_browser = open_browser
        self.timeout_seconds = timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None

    async def redirect_handler(self, authorization_url: str) -> None:
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        self._server = await asyncio.start_server(self._handle_callback, self.host, self.port)
        print(f"Open this URL to authorize MCP access:\n{authorization_url}")
        if self.open_browser:
            await asyncio.to_thread(webbrowser.open, authorization_url)

    async def callback_handler(self) -> AuthorizationCodeResult:
        if self._result is None:
            raise RuntimeError("OAuth callback server was not started")
        try:
            return await asyncio.wait_for(self._result, timeout=self.timeout_seconds)
        finally:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None

    async def _handle_callback(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = (await reader.readline()).decode("ascii", errors="replace")
            parts = request_line.split()
            method = parts[0] if parts else ""
            target = parts[1] if len(parts) >= 2 else ""
            parsed = urlparse(target)
            while await reader.readline() not in {b"\r\n", b"\n", b""}:
                pass
            query = parse_qs(parsed.query)
            code = query.get("code", [""])[0]
            state = query.get("state", [None])[0]
            iss = query.get("iss", [None])[0]
            error = query.get("error", [None])[0]
            valid_path = parsed.path == self.path
            ok = method == "GET" and valid_path and bool(code) and not error
            status = "200 OK" if ok else "400 Bad Request"
            message = (
                "Authorization complete. You may close this window."
                if ok
                else f"Authorization failed: {error or 'invalid callback'}"
            )
            body = message.encode("utf-8")
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
            if self._result is not None and not self._result.done():
                if ok:
                    self._result.set_result(
                        AuthorizationCodeResult(code=code, state=state, iss=iss)
                    )
                else:
                    self._result.set_exception(
                        RuntimeError(error or "Invalid OAuth callback")
                    )
        finally:
            writer.close()
            await writer.wait_closed()


class StrictOAuthClientProvider(OAuthClientProvider):
    """MCP SDK OAuth provider with the spec-mandated PKCE metadata check."""

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        metadata = self.context.oauth_metadata
        methods = metadata.code_challenge_methods_supported if metadata else None
        if not methods or "S256" not in methods:
            raise OAuthFlowError(
                "Authorization server metadata must advertise PKCE S256 support"
            )
        return await super()._perform_authorization_code_grant()


def build_oauth_provider(server_url: str, config: McpOAuthConfig) -> OAuthClientProvider:
    flow = LoopbackOAuthFlow(
        config.redirect_uri,
        open_browser=config.open_browser,
        timeout_seconds=config.timeout_seconds,
    )
    metadata = OAuthClientMetadata(
        redirect_uris=[config.redirect_uri],
        scope=config.scopes,
        client_name=config.client_name,
    )
    # The SDK owns discovery, PKCE S256 generation, state + RFC 9207 issuer
    # validation, RFC 8707 resource indicators, token refresh, dynamic
    # registration, and scope step-up. We retain the explicit metadata check
    # because the MCP authorization specification requires clients to verify
    # advertised PKCE support before proceeding.
    return StrictOAuthClientProvider(
        server_url,
        metadata,
        FileOAuthStorage(config.token_file),
        redirect_handler=flow.redirect_handler,
        callback_handler=flow.callback_handler,
        client_metadata_url=config.client_metadata_url,
    )
