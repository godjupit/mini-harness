from __future__ import annotations

import json
import stat
import threading
import time

import pytest

from mini_openharness.cli import main
from mini_openharness.trace import (
    MemoryTraceSink,
    TraceStore,
    TraceWriteError,
    TraceWriter,
)


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


def test_trace_file_is_owner_only(tmp_path):
    writer = TraceWriter(tmp_path, run_id="private")

    assert stat.S_IMODE(writer.path.stat().st_mode) == 0o600


def test_best_effort_trace_warns_once_and_disables_after_write_failure(
    tmp_path, monkeypatch
):
    errors = []
    writer = TraceWriter(tmp_path, run_id="best-effort", on_error=errors.append)

    def fail_write(line):
        del line
        raise OSError("disk full")

    monkeypatch.setattr(writer, "_append_line", fail_write)
    first = writer.emit("first")
    second = writer.emit("second")

    assert first.kind == "first" and second.kind == "second"
    assert len(errors) == 1
    assert "disk full" in errors[0]
    assert writer.disabled is True


def test_strict_trace_raises_typed_write_error(tmp_path, monkeypatch):
    writer = TraceWriter(tmp_path, run_id="strict", strict=True)

    def fail_write(line):
        del line
        raise OSError("read only filesystem")

    monkeypatch.setattr(writer, "_append_line", fail_write)

    with pytest.raises(TraceWriteError, match="read only filesystem"):
        writer.emit("failed")


def test_memory_trace_sink_implements_same_event_contract():
    sink = MemoryTraceSink(run_id="memory", metadata={"prompt": "inspect"})
    sink.emit("custom", {"value": 1})
    first_end = sink.finish(status="completed")
    second_end = sink.finish(status="failed")

    assert [event.kind for event in sink.events] == ["run_start", "custom", "run_end"]
    assert first_end is second_end
    assert first_end.data["status"] == "completed"


def test_trace_store_ignores_partial_last_line_from_active_writer(tmp_path):
    writer = TraceWriter(tmp_path, run_id="partial")
    with writer.path.open("ab") as stream:
        stream.write(b'{"sequence": 2')

    events = list(TraceStore(tmp_path).read("partial"))

    assert [event.kind for event in events] == ["run_start"]


def test_concurrent_trace_emits_produce_complete_ordered_jsonl(tmp_path):
    writer = TraceWriter(tmp_path, run_id="concurrent")
    threads = [
        threading.Thread(target=writer.emit, args=("worker", {"index": index}))
        for index in range(20)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer.finish(status="completed")

    events = list(TraceStore(tmp_path).read("concurrent"))
    assert [event.sequence for event in events] == list(range(1, 23))
    assert sum(event.kind == "worker" for event in events) == 20


def test_trace_prune_is_dry_run_by_default_and_skips_active_runs(tmp_path):
    older = TraceWriter(tmp_path, run_id="older")
    older.finish(status="completed")
    time.sleep(0.002)
    newer = TraceWriter(tmp_path, run_id="newer")
    newer.finish(status="completed")
    active = TraceWriter(tmp_path, run_id="active")

    candidates = TraceStore(tmp_path).prune(max_runs=1)

    assert candidates == [older.path]
    assert older.path.exists()
    deleted = TraceStore(tmp_path).prune(max_runs=1, dry_run=False)
    assert deleted == [older.path]
    assert not older.path.exists()
    assert newer.path.exists() and active.path.exists()


def test_cli_trace_prune_requires_filter_and_explicit_apply(tmp_path, capsys):
    first = TraceWriter(tmp_path, run_id="first")
    first.finish(status="completed")
    time.sleep(0.002)
    second = TraceWriter(tmp_path, run_id="second")
    second.finish(status="completed")

    with pytest.raises(SystemExit, match="requires"):
        main(["trace", "prune", "--trace-dir", str(tmp_path)])
    assert (
        main(
            [
                "trace",
                "prune",
                "--trace-dir",
                str(tmp_path),
                "--max-runs",
                "1",
            ]
        )
        == 0
    )
    assert "would delete" in capsys.readouterr().out
    assert first.path.exists()
    assert (
        main(
            [
                "trace",
                "prune",
                "--trace-dir",
                str(tmp_path),
                "--max-runs",
                "1",
                "--apply",
            ]
        )
        == 0
    )
    assert not first.path.exists()
