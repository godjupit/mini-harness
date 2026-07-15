from __future__ import annotations

from mini_openharness.compaction import ArtifactStore, ContextCompactor, SUMMARY_PREFIX
from mini_openharness.models import Message, ToolCall


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
