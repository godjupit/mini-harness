"""Tests for pluggable token counting."""

from __future__ import annotations

import builtins

import pytest

from mini_openharness.compaction import ContextCompactor, estimate_tokens
from mini_openharness.models import Message, ToolCall
from mini_openharness.tokens import (
    HeuristicCounter,
    TiktokenCounter,
    build_token_counter,
)


class CharCounter:
    """Test counter that treats every character as one token."""

    def count_tokens(self, text: str) -> int:
        return len(text)


def test_heuristic_counter_is_chars_per_four():
    counter = HeuristicCounter()

    assert counter.count_tokens("x" * 100) == 25
    assert counter.count_tokens("") == 1


def test_estimate_tokens_default_stays_heuristic():
    messages = [Message("user", "x" * 100)]

    assert estimate_tokens(messages) == max(1, (100 + len("user")) // 4)


def test_estimate_tokens_uses_provided_counter():
    messages = [
        Message("user", "hello"),
        Message(
            "assistant",
            "",
            tool_calls=(ToolCall("c1", "read_file", {"path": "a.py"}),),
        ),
    ]

    total = estimate_tokens(messages, CharCounter())

    expected = (
        len("hello")
        + len("user")
        + len("assistant")
        + len("read_file")
        + len(str({"path": "a.py"}))
    )
    assert total == expected


def test_compactor_budget_respects_provided_token_counter():
    messages = [Message("system", "system")]
    for index in range(4):
        messages.extend(
            [
                Message("user", f"big {index} " + "y" * 100),
                Message("assistant", "a" * 100),
            ]
        )
    for index in range(2):
        messages.extend([Message("user", f"recent {index}"), Message("assistant", "b")])

    result = ContextCompactor(
        threshold_tokens=1,
        keep_recent_units=1,
        keep_recent_tokens=50,
        token_counter=CharCounter(),
    ).compact(messages)

    body = " ".join(message.content for message in result.messages if message.role != "system")
    assert "recent 1" in body
    assert "big 0" not in body


def test_build_token_counter_falls_back_without_tiktoken(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("tiktoken not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    counter = build_token_counter("gpt-4o")

    assert isinstance(counter, HeuristicCounter)


def test_tiktoken_counter_uses_cached_encoding_without_installing(monkeypatch):
    class FakeEncoding:
        def encode(self, text: str):
            return list(text)

    TiktokenCounter._cache["test-model"] = FakeEncoding()
    try:
        counter = TiktokenCounter(model="test-model")
        assert counter.count_tokens("hello") == 5
    finally:
        TiktokenCounter._cache.pop("test-model", None)


def test_tiktoken_counter_real_when_installed():
    pytest.importorskip("tiktoken")

    counter = TiktokenCounter(model="gpt-4o")

    assert counter.count_tokens("hello world") > 0
