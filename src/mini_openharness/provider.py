"""Provider boundary with OpenAI-compatible streaming, retries, and errors."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

import httpx

from mini_openharness.models import Message, ModelReply, ToolCall


class ProviderError(RuntimeError):
    """Base class for normalized provider failures."""


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderNetworkError(ProviderError):
    pass


class ProviderServerError(ProviderError):
    pass


class ProviderInvalidResponseError(ProviderError):
    pass


class ProviderCancelledError(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True)
class ProviderRetry:
    attempt: int
    delay_seconds: float
    error: str


@dataclass(frozen=True)
class ProviderComplete:
    reply: ModelReply


ProviderEvent = ProviderTextDelta | ProviderRetry | ProviderComplete


class StreamingModelProvider(Protocol):
    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one assistant turn and finish with ProviderComplete."""


class CompletionModelProvider(Protocol):
    async def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        """Return one non-streaming assistant turn."""


ModelProvider = StreamingModelProvider | CompletionModelProvider


class OpenAICompatibleProvider:
    """Adapter for OpenAI-compatible streaming chat completions APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.max_retries = max(0, max_retries)
        self.retry_base_delay = max(0.0, retry_base_delay)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        payload = self._payload(messages, tools)
        for attempt in range(self.max_retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderCancelledError("Provider request cancelled")
            emitted_content = False
            try:
                async for event in self._stream_once(payload, cancel_event=cancel_event):
                    if isinstance(event, ProviderTextDelta):
                        emitted_content = True
                    yield event
                return
            except ProviderCancelledError:
                raise
            except ProviderError as exc:
                if emitted_content or attempt >= self.max_retries or not _retryable(exc):
                    raise
                delay = self.retry_base_delay * (2**attempt)
                yield ProviderRetry(attempt + 1, delay, str(exc))
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderCancelledError("Provider request cancelled")
                if cancel_event is None:
                    await asyncio.sleep(delay)
                else:
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    else:
                        raise ProviderCancelledError("Provider request cancelled")

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        reply: ModelReply | None = None
        async for event in self.stream(messages, tools):
            if isinstance(event, ProviderComplete):
                reply = event.reply
        if reply is None:
            raise ProviderInvalidResponseError("Stream ended without a completed response")
        return reply

    async def _stream_once(
        self,
        payload: dict[str, Any],
        *,
        cancel_event: asyncio.Event | None,
    ) -> AsyncIterator[ProviderEvent]:
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        input_tokens = 0
        output_tokens = 0
        try:
            async with self._client.stream("POST", "chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise _status_error(response.status_code, body)
                async for line in response.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        raise ProviderCancelledError("Provider request cancelled")
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ProviderInvalidResponseError(
                            f"Invalid SSE JSON: {raw[:200]}"
                        ) from exc
                    usage = chunk.get("usage") or {}
                    input_tokens = int(usage.get("prompt_tokens", input_tokens) or input_tokens)
                    output_tokens = int(
                        usage.get("completion_tokens", output_tokens) or output_tokens
                    )
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            content_parts.append(str(text))
                            yield ProviderTextDelta(str(text))
                        for raw_call in delta.get("tool_calls") or []:
                            index = int(raw_call.get("index", 0))
                            part = tool_parts.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            part["id"] += str(raw_call.get("id") or "")
                            function = raw_call.get("function") or {}
                            part["name"] += str(function.get("name") or "")
                            part["arguments"] += str(function.get("arguments") or "")
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc) or "Provider request timed out") from exc
        except httpx.NetworkError as exc:
            raise ProviderNetworkError(str(exc) or "Provider network error") from exc
        except asyncio.CancelledError as exc:
            raise ProviderCancelledError("Provider request cancelled") from exc

        calls = tuple(
            _tool_call_from_parts(index, part) for index, part in sorted(tool_parts.items())
        )
        yield ProviderComplete(
            ModelReply(
                content="".join(content_parts),
                tool_calls=calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )

    def _payload(self, messages: list[Message], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_openai_message(message) for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    async def close(self) -> None:
        await self._client.aclose()


def _retryable(exc: ProviderError) -> bool:
    return isinstance(
        exc,
        (ProviderRateLimitError, ProviderTimeoutError, ProviderNetworkError),
    ) or isinstance(exc, ProviderServerError)


def _status_error(status: int, body: str) -> ProviderError:
    detail = f"HTTP {status}: {body[:500]}"
    if status in {401, 403}:
        return ProviderAuthenticationError(detail)
    if status == 429:
        return ProviderRateLimitError(detail)
    if status >= 500:
        return ProviderServerError(detail)
    return ProviderError(detail)


def _tool_call_from_parts(index: int, part: dict[str, str]) -> ToolCall:
    raw_arguments = part["arguments"] or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ProviderInvalidResponseError(
            f"Invalid tool arguments from stream: {raw_arguments!r}"
        ) from exc
    if not isinstance(arguments, dict):
        raise ProviderInvalidResponseError("Tool arguments must be a JSON object")
    return ToolCall(
        id=part["id"] or f"tool-call-{index}",
        name=part["name"],
        arguments=arguments,
    )


def _to_openai_message(message: Message) -> dict[str, Any]:
    result: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        result["tool_call_id"] = message.tool_call_id
    if message.name:
        result["name"] = message.name
    return result


class DemoProvider:
    """Deterministic provider that proves the full loop without an API key."""

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        tool_names = {tool["name"] for tool in tools}
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            return ModelReply(
                content="我先查看工作区。",
                tool_calls=(ToolCall("demo-list", "list_files", {"path": "."}),),
            )
        invoked = {message.name for message in tool_messages}
        if "load_skill" in tool_names and "load_skill" not in invoked:
            return ModelReply(
                content="发现了可用 skill，按需加载完整指令。",
                tool_calls=(ToolCall("demo-skill", "load_skill", {"name": "repository-guide"}),),
            )
        if "read_file" not in invoked:
            return ModelReply(
                content="接着读取项目说明。",
                tool_calls=(ToolCall("demo-read", "read_file", {"path": "README.md"}),),
            )
        preview = tool_messages[-1].content.splitlines()[0][:120]
        return ModelReply(content=f"演示完成：工具链成功读取 README，首行为：{preview}")
