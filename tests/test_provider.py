from __future__ import annotations

import asyncio

import httpx
import pytest

from mini_openharness.models import Message, ToolCall
from mini_openharness.provider import (
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderComplete,
    ProviderContextWindowError,
    ProviderOutputTruncatedError,
    ProviderReasoningDelta,
    ProviderRetry,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallStart,
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


def test_chat_stream_text_only():
    body = (
        'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo there"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
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
        == "hello there"
    )
    assert not any(isinstance(event, ProviderReasoningDelta) for event in events)
    assert not any(
        isinstance(event, (ProviderToolCallStart, ProviderToolCallDelta))
        for event in events
    )
    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.content == "hello there"
    assert reply.tool_calls == ()


def test_chat_stream_without_reasoning_content_still_works():
    body = (
        'data: {"choices":[{"delta":{"content":"plain","reasoning_content":null}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" answer"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
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

    assert not any(isinstance(event, ProviderReasoningDelta) for event in events)
    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.content == "plain answer"
    assert reply.tool_calls == ()


def test_chat_reasoning_then_text_keeps_reasoning_out_of_content():
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"Let me think"}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_content":" step by step"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Answer:"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" 42"}}]}\n\n'
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

    reasoning = "".join(
        event.delta for event in events if isinstance(event, ProviderReasoningDelta)
    )
    assert reasoning == "Let me think step by step"
    text = "".join(
        event.text for event in events if isinstance(event, ProviderTextDelta)
    )
    assert text == "Answer: 42"
    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.content == "Answer: 42"
    assert "Let me think" not in reply.content


def test_chat_reasoning_then_tool_call_emits_start_and_preserves_arguments():
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"need to read"}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
        '"type":"function","function":{"name":"read_file","arguments":""}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"path\\":\\"a"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"}"}}]}}]}\n\n'
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

    starts = [
        event for event in events if isinstance(event, ProviderToolCallStart)
    ]
    assert len(starts) == 1
    assert starts[0].index == 0
    assert starts[0].name == "read_file"
    assert starts[0].call_id == "call-1"
    deltas = [
        event for event in events if isinstance(event, ProviderToolCallDelta)
    ]
    assert "".join(event.arguments_delta for event in deltas) == '{"path":"a"}'
    first_delta_pos = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ProviderToolCallDelta)
    )
    complete_pos = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ProviderComplete)
    )
    assert first_delta_pos < complete_pos
    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.content == ""
    assert "need to read" not in reply.content
    assert reply.tool_calls == (ToolCall("call-1", "read_file", {"path": "a"}),)


def test_chat_tool_call_only_emits_start_without_text_or_reasoning():
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-7",'
        '"type":"function","function":{"name":"list_dir","arguments":""}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"path\\":\\".\\"}"}}]}}]}\n\n'
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

    assert not any(
        isinstance(event, (ProviderTextDelta, ProviderReasoningDelta))
        for event in events
    )
    starts = [
        event for event in events if isinstance(event, ProviderToolCallStart)
    ]
    assert len(starts) == 1
    assert starts[0].name == "list_dir"
    assert starts[0].call_id == "call-7"
    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.content == ""
    assert reply.tool_calls == (ToolCall("call-7", "list_dir", {"path": "."}),)


def test_responses_stream_uses_typed_items_call_id_and_usage():
    captured = {}
    body = (
        'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        'data: {"type":"response.output_item.added","output_index":1,'
        '"item":{"type":"function_call","id":"fc-1","call_id":"call-1",'
        '"name":"read_file","arguments":""}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":1,'
        '"delta":"{\\"path\\":\\"README.md\\"}"}\n\n'
        'data: {"type":"response.output_item.done","output_index":1,'
        '"item":{"type":"function_call","id":"fc-1","call_id":"call-1",'
        '"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}\n\n'
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":11,"output_tokens":4}}}\n\n'
    )

    def handler(request):
        captured["path"] = request.url.path
        captured["payload"] = __import__("json").loads(request.content)
        return httpx.Response(200, text=body)

    provider = OpenAIResponsesProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(handler),
    )
    messages = [
        Message("system", "system guidance"),
        Message("user", "inspect"),
        Message(
            "assistant",
            tool_calls=(ToolCall("old-call", "list_files", {"path": "."}),),
        ),
        Message("tool", "README.md", tool_call_id="old-call", name="list_files"),
    ]

    async def run():
        return [
            event
            async for event in provider.stream(
                messages,
                [
                    {
                        "name": "read_file",
                        "description": "read",
                        "parameters": {"type": "object"},
                    }
                ],
            )
        ]

    try:
        events = asyncio.run(run())
    finally:
        asyncio.run(provider.close())

    assert captured["path"].endswith("/responses")
    assert captured["payload"]["instructions"] == "system guidance"
    assert captured["payload"]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "old-call",
        "output": "README.md",
    }
    assert captured["payload"]["tools"][0]["type"] == "function"
    starts = [
        event for event in events if isinstance(event, ProviderToolCallStart)
    ]
    assert len(starts) == 1
    assert starts[0].index == 1
    assert starts[0].name == "read_file"
    assert starts[0].call_id == "call-1"
    assert any(isinstance(event, ProviderToolCallDelta) for event in events)
    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.content == "hello"
    assert reply.tool_calls == (ToolCall("call-1", "read_file", {"path": "README.md"}),)
    assert (reply.input_tokens, reply.output_tokens) == (11, 4)


def test_responses_function_arguments_done_is_authoritative():
    body = (
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","call_id":"call-1",'
        '"name":"read_file","arguments":""}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"delta":"{\\"path\\":\\"partial"}\n\n'
        'data: {"type":"response.function_call_arguments.done","output_index":0,'
        '"item_id":"fc-1","name":"read_file",'
        '"arguments":"{\\"path\\":\\"README.md\\"}"}\n\n'
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":3,"output_tokens":2}}}\n\n'
    )
    provider = OpenAIResponsesProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
    )
    try:
        events = collect(provider)
    finally:
        asyncio.run(provider.close())

    reply = next(event.reply for event in events if isinstance(event, ProviderComplete))
    assert reply.tool_calls == (ToolCall("call-1", "read_file", {"path": "README.md"}),)


def test_responses_enables_strict_only_for_compatible_function_schemas():
    captured = {}
    body = (
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":1,"output_tokens":1}}}\n\n'
    )

    def handler(request):
        captured["payload"] = __import__("json").loads(request.content)
        return httpx.Response(200, text=body)

    provider = OpenAIResponsesProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(handler),
    )
    tools = [
        {
            "name": "read_file",
            "description": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_files",
            "description": "list",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        },
    ]

    async def run():
        return [event async for event in provider.stream([Message("user", "inspect")], tools)]

    try:
        asyncio.run(run())
    finally:
        asyncio.run(provider.close())

    strict_tool, fallback_tool = captured["payload"]["tools"]
    assert strict_tool["strict"] is True
    assert "strict" not in fallback_tool


def test_chat_length_finish_reason_never_becomes_success():
    body = (
        'data: {"choices":[{"delta":{"content":"partial"},'
        '"finish_reason":"length"}]}\n\n'
        "data: [DONE]\n\n"
    )
    provider = OpenAICompatibleProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
    )
    try:
        with pytest.raises(ProviderOutputTruncatedError, match="finish_reason=length"):
            collect(provider)
    finally:
        asyncio.run(provider.close())


def test_responses_incomplete_max_output_tokens_never_becomes_success():
    body = (
        'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        'data: {"type":"response.incomplete","response":'
        '{"incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
    )
    provider = OpenAIResponsesProvider(
        api_key="test",
        model="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
    )
    try:
        with pytest.raises(ProviderOutputTruncatedError, match="max_output_tokens"):
            collect(provider)
    finally:
        asyncio.run(provider.close())


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


@pytest.mark.parametrize(
    "body",
    [
        '{"error":{"code":"context_length_exceeded","message":"too large"}}',
        '{"error":{"message":"maximum context length exceeded"}}',
        '{"error":{"message":"prompt is too long for this context window"}}',
    ],
)
def test_context_window_http_errors_are_typed_for_runtime_recovery(body):
    provider = OpenAIResponsesProvider(
        api_key="test",
        model="test",
        max_retries=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(400, text=body)),
    )
    try:
        with pytest.raises(ProviderContextWindowError):
            collect(provider)
    finally:
        asyncio.run(provider.close())


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
