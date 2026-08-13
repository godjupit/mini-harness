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

    result = ContextCompactor(threshold_tokens=10, keep_recent_units=2).compact(messages)

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
    compactor = ContextCompactor(threshold_tokens=1_000_000, keep_recent_units=2)

    normal = compactor.compact(messages)
    forced = compactor.compact(messages, force=True)

    assert not normal.compacted
    assert forced.compacted
    assert forced.messages[-2:] == messages[-2:]


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
        ContextCompactor(threshold_tokens=1, keep_recent_units=2).compact_with_provider(
            messages, provider
        )
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
        ContextCompactor(threshold_tokens=1, keep_recent_units=2).compact_with_provider(
            messages, FailingProvider()
        )
    )

    assert result.compacted
    assert result.summary_source == "deterministic_fallback"
