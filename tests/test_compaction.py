from __future__ import annotations

import asyncio

from mini_openharness.compaction import ArtifactStore, ContextCompactor, SUMMARY_PREFIX
from mini_openharness.models import Message, ModelReply, ToolCall


def test_compaction_keeps_recent_tool_call_and_all_results_together():
    messages = [
        Message("system", "system"),
        Message("user", "old " + "x" * 500),
        Message("assistant", "old answer " + "y" * 500),
        Message("user", "recent request"),
        Message(
            "assistant",
            tool_calls=(ToolCall("a", "read_file", {}), ToolCall("b", "list_files", {})),
        ),
        Message("tool", "A", tool_call_id="a", name="read_file"),
        Message("tool", "B", tool_call_id="b", name="list_files"),
    ]

    result = ContextCompactor(
        threshold_tokens=10,
        keep_recent_units=2,
        keep_recent_tokens=10,
    ).compact(messages)

    assert result.compacted
    assert result.messages[1].content.startswith(SUMMARY_PREFIX)
    assistant = next(message for message in result.messages if message.tool_calls)
    result_ids = {message.tool_call_id for message in result.messages if message.role == "tool"}
    assert {call.id for call in assistant.tool_calls} == result_ids == {"a", "b"}
    assert result.after_tokens < result.before_tokens


def test_large_output_is_fully_preserved_as_artifact(tmp_path):
    output = "head-" + "x" * 10_000 + "-tail"
    inline, path = ArtifactStore(tmp_path, max_inline_chars=100).offload(
        run_id="run", tool_call_id="tool/1", output=output
    )
    assert path is not None
    assert path.read_text(encoding="utf-8") == output
    assert "offloaded" in inline
    assert len(inline) < len(output)


def test_forced_compaction_ignores_threshold_but_preserves_recent_units():
    messages = [Message("system", "system")]
    for index in range(5):
        messages.extend(
            [
                Message("user", f"request {index}"),
                Message("assistant", f"answer {index}"),
            ]
        )
    compactor = ContextCompactor(
        threshold_tokens=1_000_000,
        keep_recent_units=2,
        keep_recent_tokens=10,
    )

    normal = compactor.compact(messages)
    forced = compactor.compact(messages, force=True)

    assert not normal.compacted
    assert forced.compacted
    assert forced.messages[-2:] == messages[-2:]


def test_context_window_threshold_is_seventy_percent():
    compactor = ContextCompactor(context_window_tokens=10_000)

    assert compactor.threshold_ratio == 0.7
    assert compactor.effective_threshold == 7_000


def test_compaction_triggers_at_context_window_ratio():
    messages = [Message("system", "system")]
    for index in range(6):
        messages.extend(
            [
                Message("user", f"request {index} " + "x" * 500),
                Message("assistant", "answer " + "y" * 500),
            ]
        )
    within = ContextCompactor(
        context_window_tokens=1_000_000,
        keep_recent_units=2,
        keep_recent_tokens=10,
    )
    near = ContextCompactor(
        context_window_tokens=1_200,
        keep_recent_units=2,
        keep_recent_tokens=10,
    )

    assert not within.compact(messages).compacted
    assert near.compact(messages).compacted


def test_compaction_result_carries_summary_text():
    messages = [Message("system", "system")]
    for index in range(4):
        messages.extend([Message("user", f"request {index}"), Message("assistant", "answer")])

    result = ContextCompactor(
        threshold_tokens=1,
        keep_recent_units=2,
        keep_recent_tokens=10,
    ).compact(messages)

    assert result.summary_text.startswith(SUMMARY_PREFIX)
    assert result.summary_text == result.messages[1].content


def test_default_recent_retention_is_token_budgeted():
    compactor = ContextCompactor()

    assert compactor.keep_recent_tokens == 12_000
    assert compactor.keep_recent_units == 1


def test_recent_retention_uses_token_budget_not_unit_count():
    messages = [Message("system", "system")]
    for index in range(4):
        messages.extend(
            [
                Message("user", f"big old {index} " + "x" * 4_000),
                Message("assistant", "old answer"),
            ]
        )
    for index in range(2):
        messages.extend([Message("user", f"recent {index}"), Message("assistant", "recent answer")])

    result = ContextCompactor(
        threshold_tokens=1,
        keep_recent_units=1,
        keep_recent_tokens=1_000,
    ).compact(messages)

    body = " ".join(
        message.content for message in result.messages if message.role != "system"
    )
    assert "recent 0" in body and "recent 1" in body
    assert "big old" not in body


def test_oversized_latest_unit_is_still_kept_verbatim():
    messages = [Message("system", "system")]
    messages.extend([Message("user", "old " + "x" * 2_000), Message("assistant", "old answer")])
    big_tool = Message(
        "tool",
        "x" * 100_000,
        tool_call_id="a",
        name="read_file",
    )
    messages.extend(
        [
            Message("assistant", "", tool_calls=(ToolCall("a", "read_file", {"path": "big.py"}),)),
            big_tool,
        ]
    )

    result = ContextCompactor(
        threshold_tokens=1,
        keep_recent_units=1,
        keep_recent_tokens=100,
    ).compact(messages)

    assert any(message.tool_calls for message in result.messages)
    assert result.messages[-1].content == "x" * 100_000


def test_model_compaction_creates_structured_handoff_and_keeps_recent_units():
    class SummaryProvider:
        requests = []

        async def complete(self, messages, tools):
            self.requests.append((messages, tools))
            return ModelReply(
                content=(
                    "Primary request: update the parser.\n"
                    "Files changed: src/parser.py.\n"
                    "Pending work: add regression tests."
                ),
                input_tokens=40,
                output_tokens=20,
            )

    messages = [Message("system", "system")]
    for index in range(4):
        messages.extend(
            [
                Message("user", f"request {index}"),
                Message("assistant", f"answer {index}"),
            ]
        )
    provider = SummaryProvider()

    result = asyncio.run(
        ContextCompactor(
            threshold_tokens=1,
            keep_recent_units=2,
            keep_recent_tokens=10,
        ).compact_with_provider(messages, provider)
    )

    assert result.compacted
    assert result.summary_source == "model"
    assert result.messages[1].content.startswith(SUMMARY_PREFIX)
    assert "Pending work" in result.messages[1].content
    assert result.messages[-2:] == messages[-2:]
    assert provider.requests[0][1] == []


def test_model_compaction_falls_back_when_summary_request_fails():
    class FailingProvider:
        async def complete(self, messages, tools):
            del messages, tools
            raise RuntimeError("summary unavailable")

    messages = [Message("system", "system")]
    for index in range(4):
        messages.extend([Message("user", f"request {index}"), Message("assistant", "answer")])

    result = asyncio.run(
        ContextCompactor(
            threshold_tokens=1,
            keep_recent_units=2,
            keep_recent_tokens=10,
        ).compact_with_provider(messages, FailingProvider())
    )

    assert result.compacted
    assert result.summary_source == "deterministic_fallback"
