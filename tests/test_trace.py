from __future__ import annotations

import json

from mini_openharness.cli import main
from mini_openharness.trace import TraceStore, TraceWriter


def test_trace_jsonl_list_show_and_safe_replay(tmp_path, capsys):
    writer = TraceWriter(tmp_path, run_id="run-1", metadata={"prompt": "inspect"})
    writer.emit("model_request", {"messages": ["hello"]})
    writer.emit("tool_start", {"name": "read_file", "input": {"path": "README.md"}})
    writer.finish(status="completed", data={"steps": 1})

    store = TraceStore(tmp_path)
    summaries = store.list()
    assert summaries[0].status == "completed"
    assert summaries[0].event_count == 4
    assert [event.kind for event in store.read("run-1")] == [
        "run_start",
        "model_request",
        "tool_start",
        "run_end",
    ]

    assert main(["trace", "show", "run-1", "--trace-dir", str(tmp_path)]) == 0
    shown = capsys.readouterr().out
    assert json.loads(shown)[1]["kind"] == "model_request"

    assert main(["trace", "replay", "run-1", "--trace-dir", str(tmp_path)]) == 0
    replayed = capsys.readouterr().out
    assert "recorded events only" in replayed
    assert "tool_start" in replayed


def test_trace_rejects_path_traversal_run_id(tmp_path):
    store = TraceStore(tmp_path)
    try:
        list(store.read("../secret"))
    except ValueError as exc:
        assert "Invalid run id" in str(exc)
    else:
        raise AssertionError("path traversal run id should be rejected")


def test_finish_is_idempotent(tmp_path):
    writer = TraceWriter(tmp_path, run_id="once")
    writer.finish(status="completed")
    writer.finish(status="cancelled")
    events = list(TraceStore(tmp_path).read("once"))
    assert [event.kind for event in events].count("run_end") == 1
    assert events[-1].data["status"] == "completed"


def test_trace_redacts_secret_keys_and_common_credentials_by_default(tmp_path):
    writer = TraceWriter(
        tmp_path,
        run_id="redacted",
        metadata={
            "api_key": "top-secret",
            "OPENAI_API_KEY": "sk-should-not-survive",
            "input_tokens": 7,
        },
    )
    writer.emit(
        "request",
        {"authorization": "Bearer abc.def", "text": "use sk-abcdefghijklmnop"},
    )

    events = list(TraceStore(tmp_path).read("redacted"))

    assert events[0].data["api_key"] == "[REDACTED]"
    assert events[0].data["OPENAI_API_KEY"] == "[REDACTED]"
    assert events[0].data["input_tokens"] == 7
    assert events[1].data["authorization"] == "[REDACTED]"
    assert events[1].data["text"] == "use sk-[REDACTED]"
