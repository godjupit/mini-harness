"""Provider boundary with OpenAI-compatible streaming, retries, and errors."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

import httpx

from mini_openharness.errors.provider import (
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderContextWindowError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderNetworkError,
    ProviderOutputTruncatedError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from mini_openharness.models import Message, ModelReply, ToolCall
from mini_openharness.utils.tokens import HeuristicCounter, TokenCounter


@dataclass(frozen=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True)
class ProviderReasoningDelta:
    delta: str


@dataclass(frozen=True)
class ProviderToolCallStart:
    index: int
    name: str | None = None
    call_id: str | None = None


@dataclass(frozen=True)
class ProviderToolCallDelta:
    index: int
    arguments_delta: str


@dataclass(frozen=True)
class ProviderRetry:
    attempt: int
    delay_seconds: float
    error: str


@dataclass(frozen=True)
class ProviderComplete:
    reply: ModelReply


ProviderEvent = (
    ProviderTextDelta
    | ProviderReasoningDelta
    | ProviderToolCallStart
    | ProviderToolCallDelta
    | ProviderRetry
    | ProviderComplete
)


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
        context_window_tokens: int | None = None,
        token_counter: TokenCounter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.token_counter = token_counter or HeuristicCounter()
        self.max_retries = max(0, max_retries)
        self.retry_base_delay = max(0.0, retry_base_delay)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def count_tokens(self, text: str) -> int:
        return self.token_counter.count_tokens(text)

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
        tool_call_started: set[int] = set()
        input_tokens = 0
        output_tokens = 0
        finish_reasons: set[str] = set()
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
                        finish_reason = choice.get("finish_reason")
                        if finish_reason:
                            finish_reasons.add(str(finish_reason))
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            yield ProviderReasoningDelta(str(reasoning))
                        text = delta.get("content")
                        if text:
                            content_parts.append(str(text))
                            yield ProviderTextDelta(str(text))
                        for raw_call in delta.get("tool_calls") or []:
                            index = int(raw_call.get("index", 0))
                            part = tool_parts.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            function = raw_call.get("function") or {}
                            call_id = str(raw_call.get("id") or "")
                            function_name = str(function.get("name") or "")
                            arguments_delta = str(function.get("arguments") or "")
                            if index not in tool_call_started:
                                tool_call_started.add(index)
                                yield ProviderToolCallStart(
                                    index=index,
                                    name=function_name or None,
                                    call_id=call_id or None,
                                )
                            part["id"] += call_id
                            part["name"] += function_name
                            part["arguments"] += arguments_delta
                            if arguments_delta:
                                yield ProviderToolCallDelta(
                                    index=index, arguments_delta=arguments_delta
                                )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc) or "Provider request timed out") from exc
        except httpx.NetworkError as exc:
            raise ProviderNetworkError(str(exc) or "Provider network error") from exc
        except asyncio.CancelledError as exc:
            raise ProviderCancelledError("Provider request cancelled") from exc

        if "length" in finish_reasons:
            raise ProviderOutputTruncatedError(
                "Chat completion was truncated with finish_reason=length"
            )
        if "content_filter" in finish_reasons:
            raise ProviderInvalidResponseError(
                "Chat completion was stopped by the provider content filter"
            )
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


class OpenAIResponsesProvider(OpenAICompatibleProvider):
    """OpenAI Responses API adapter using typed Items and streaming events."""

    async def _stream_once(
        self,
        payload: dict[str, Any],
        *,
        cancel_event: asyncio.Event | None,
    ) -> AsyncIterator[ProviderEvent]:
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        tool_call_started: set[int] = set()
        input_tokens = 0
        output_tokens = 0
        completed = False
        try:
            async with self._client.stream("POST", "responses", json=payload) as response:
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
                        event = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ProviderInvalidResponseError(
                            f"Invalid Responses SSE JSON: {raw[:200]}"
                        ) from exc
                    event_type = str(event.get("type", ""))
                    if event_type == "response.output_text.delta":
                        text = str(event.get("delta") or "")
                        if text:
                            content_parts.append(text)
                            yield ProviderTextDelta(text)
                    elif event_type == "response.output_item.added":
                        item = event.get("item") or {}
                        if item.get("type") == "function_call":
                            index = int(event.get("output_index", 0))
                            tool_parts[index] = {
                                "id": str(item.get("call_id") or item.get("id") or ""),
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or ""),
                            }
                            if index not in tool_call_started:
                                tool_call_started.add(index)
                                yield ProviderToolCallStart(
                                    index=index,
                                    name=str(item.get("name") or None),
                                    call_id=str(
                                        item.get("call_id") or item.get("id") or None
                                    ),
                                )
                    elif event_type == "response.function_call_arguments.delta":
                        index = int(event.get("output_index", 0))
                        part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        arguments_delta = str(event.get("delta") or "")
                        part["arguments"] += arguments_delta
                        if arguments_delta:
                            yield ProviderToolCallDelta(
                                index=index, arguments_delta=arguments_delta
                            )
                    elif event_type == "response.function_call_arguments.done":
                        index = int(event.get("output_index", 0))
                        part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        # The documented done event contains item_id, not call_id;
                        # preserve the call_id collected from output_item.added.
                        part["name"] = str(event.get("name") or part["name"])
                        part["arguments"] = str(event.get("arguments") or "{}")
                    elif event_type == "response.output_item.done":
                        item = event.get("item") or {}
                        if item.get("type") == "function_call":
                            index = int(event.get("output_index", 0))
                            tool_parts[index] = {
                                "id": str(item.get("call_id") or item.get("id") or ""),
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or "{}"),
                            }
                            if index not in tool_call_started:
                                tool_call_started.add(index)
                                yield ProviderToolCallStart(
                                    index=index,
                                    name=str(item.get("name") or None),
                                    call_id=str(
                                        item.get("call_id") or item.get("id") or None
                                    ),
                                )
                    elif event_type == "response.completed":
                        completed = True
                        usage = (event.get("response") or {}).get("usage") or {}
                        input_tokens = int(usage.get("input_tokens", 0) or 0)
                        output_tokens = int(usage.get("output_tokens", 0) or 0)
                    elif event_type == "response.incomplete":
                        response_data = event.get("response") or {}
                        details = response_data.get("incomplete_details") or {}
                        reason = str(details.get("reason") or "unknown")
                        if reason == "max_output_tokens":
                            raise ProviderOutputTruncatedError(
                                "Responses API response incomplete: max_output_tokens"
                            )
                        raise ProviderInvalidResponseError(
                            f"Responses API response.incomplete: {details or event}"
                        )
                    elif event_type in {"error", "response.failed"}:
                        detail = event.get("error") or (event.get("response") or {}).get("error")
                        raise ProviderInvalidResponseError(
                            f"Responses API {event_type}: {detail or event}"
                        )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc) or "Provider request timed out") from exc
        except httpx.NetworkError as exc:
            raise ProviderNetworkError(str(exc) or "Provider network error") from exc
        except asyncio.CancelledError as exc:
            raise ProviderCancelledError("Provider request cancelled") from exc

        if not completed:
            raise ProviderInvalidResponseError("Responses stream ended before response.completed")
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
        instructions = "\n\n".join(
            message.content for message in messages if message.role == "system" and message.content
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "input": _to_responses_items(messages),
            "stream": True,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = []
            for tool in tools:
                definition = {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                }
                if _strict_schema_compatible(tool["parameters"]):
                    definition["strict"] = True
                # For incompatible schemas, omit strict. Current Responses API
                # attempts normalization and falls back to best-effort calling;
                # explicit false would disable that behavior.
                payload["tools"].append(definition)
            payload["tool_choice"] = "auto"
        return payload


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
    if status in {400, 413} and _is_context_window_error(body):
        return ProviderContextWindowError(detail)
    if status >= 500:
        return ProviderServerError(detail)
    return ProviderError(detail)


def _is_context_window_error(body: str) -> bool:
    lowered = body.lower()
    if "context_length_exceeded" in lowered or "maximum context length" in lowered:
        return True
    if "prompt is too long" in lowered or "too many tokens" in lowered:
        return True
    return "context window" in lowered and any(
        marker in lowered for marker in ("exceed", "maximum", "too long", "limit")
    )


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


def _strict_schema_compatible(schema: Any) -> bool:
    """Conservatively detect the documented strict function-schema subset."""
    if not isinstance(schema, dict) or "default" in schema:
        return False
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        return len(non_null_types) == 1 and _strict_schema_compatible(
            {**schema, "type": non_null_types[0]}
        )
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or schema.get("additionalProperties") is not False
            or set(required) != set(properties)
        ):
            return False
        return all(_strict_schema_compatible(value) for value in properties.values())
    if schema_type == "array":
        return _strict_schema_compatible(schema.get("items"))
    return schema_type in {"string", "integer", "number", "boolean", "null"}


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


def _to_responses_items(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role in {"user", "assistant"} and message.content:
            items.append({"role": message.role, "content": message.content})
        if message.role == "assistant":
            items.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                }
                for call in message.tool_calls
            )
        elif message.role == "tool":
            if not message.tool_call_id:
                raise ProviderInvalidResponseError("Tool result is missing tool_call_id")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
    return items


class DemoProvider:
    """Deterministic provider that proves the full loop without an API key."""

    context_window_tokens = None

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        tool_names = {tool["name"] for tool in tools}
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            return ModelReply(
                content="我先查看工作区。",
                tool_calls=(ToolCall("demo-list", "list_dir", {"path": "."}),),
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
