from __future__ import annotations

import asyncio

import httpx
import pytest

from mini_openharness.models import Message
from mini_openharness.provider import (
    OpenAICompatibleProvider,
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderComplete,
    ProviderRetry,
    ProviderTextDelta,
)


def collect(provider, cancel_event=None):
    async def run():
        return [
            event
            async for event in provider.stream(
                [Message("user", "hello")], [], cancel_event=cancel_event
            )
        ]

    return asyncio.run(run())


def test_streaming_text_fragmented_tool_call_and_usage():
    body = (
        'data: {"choices":[{"delta":{"content":"hi "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"there","tool_calls":[{"index":0,"id":"call-","function":{"name":"read_","arguments":"{\\"pa"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"1","function":{"name":"file","arguments":"th\\":\\"a\\"}"}}]}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )
    provider = OpenAICompatibleProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
    )
    try:
        events = collect(provider)
    finally:
        asyncio.run(provider.close())

    assert (
        "".join(event.text for event in events if isinstance(event, ProviderTextDelta))
        == "hi there"
    )
    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.tool_calls[0].id == "call-1"
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].arguments == {"path": "a"}
    assert (reply.input_tokens, reply.output_tokens) == (7, 3)


def test_authentication_error_is_normalized_without_retry():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, text="bad key")

    provider = OpenAICompatibleProvider(
        api_key="bad",
        model="test",
        transport=httpx.MockTransport(handler),
        retry_base_delay=0,
    )
    try:
        try:
            collect(provider)
        except ProviderAuthenticationError:
            pass
        else:
            raise AssertionError("authentication error should be normalized")
    finally:
        asyncio.run(provider.close())
    assert attempts == 1


def test_pre_cancelled_request_never_reaches_network():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, text="data: [DONE]\n\n")

    provider = OpenAICompatibleProvider(
        api_key="test", model="test", transport=httpx.MockTransport(handler)
    )
    event = asyncio.Event()
    event.set()
    try:
        try:
            collect(provider, event)
        except ProviderCancelledError:
            pass
        else:
            raise AssertionError("cancelled request should stop")
    finally:
        asyncio.run(provider.close())
    assert attempts == 0


@pytest.mark.parametrize("failure", ["500", "timeout", "network"])
def test_retryable_failures_retry_before_any_content(failure):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "500":
                return httpx.Response(500, text="temporary")
            if failure == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200,
            text=('data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'),
        )

    provider = OpenAICompatibleProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(handler),
        retry_base_delay=0,
    )
    try:
        events = collect(provider)
    finally:
        asyncio.run(provider.close())
    assert attempts == 2
    assert any(isinstance(event, ProviderRetry) for event in events)
    assert any(isinstance(event, ProviderComplete) for event in events)
