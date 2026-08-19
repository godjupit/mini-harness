"""Tests for append-only session logs and Claude-Code-style resume."""

from __future__ import annotations

import asyncio
import json
import os
import signal

import pytest

import mini_openharness.cli as cli
from mini_openharness.engine import AgentLoop
from mini_openharness.hooks import CallbackHook, HookEvent, HookRegistry, HookResult
from mini_openharness.models import Message, ModelReply, ToolCall
from mini_openharness.session import (
    Interruption,
    SessionLog,
    SessionStore,
    detect_interruption,
    strip_dangling_tool_calls,
)
from mini_openharness.tools import default_tools


class ScriptedProvider:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append((list(messages), tools))
        return self.replies.pop(0)


def collect_resume(loop: AgentLoop):
    async def run():
        return [event async for event in loop.resume()]

    return asyncio.run(run())


def collect_run(loop: AgentLoop, prompt: str):
    async def run():
        return [event async for event in loop.run(prompt)]

    return asyncio.run(run())


# --- SessionLog / SessionStore ----------------------------------------------


def test_session_log_round_trips_user_assistant_and_tool_messages(tmp_path):
    log = SessionLog(tmp_path, session_id="abc", metadata={"first_prompt": "hello"})
    log.append_message(Message("user", "hello"))
    log.append_message(
        Message(
            "assistant",
            "",
            tool_calls=(ToolCall("c1", "list_files", {"path": "."}),),
        )
    )
    log.append_message(Message("tool", "result", tool_call_id="c1", name="list_files"))

    record = SessionStore(tmp_path).read("abc")

    assert record.session_id == "abc"
    assert record.meta["type"] == "meta"
    assert record.meta["first_prompt"] == "hello"
    assert [message.role for message in record.messages] == ["user", "assistant", "tool"]
    assert record.messages[1].tool_calls[0].id == "c1"
    assert record.messages[1].tool_calls[0].name == "list_files"
    assert record.messages[2].tool_call_id == "c1"
    assert record.messages[2].name == "list_files"


def test_compaction_record_rebuilds_compacted_context_on_read(tmp_path):
    log = SessionLog(tmp_path, session_id="compact")
    log.append_message(Message("system", "system"))
    log.append_message(Message("user", "old request 1"))
    log.append_message(Message("assistant", "old answer 1"))
    log.append_message(Message("user", "recent request"))
    log.append_compaction(
        summary="[Compacted conversation summary]\nold work summarized",
        replaced_messages=2,
        before_tokens=100,
        after_tokens=20,
        summary_source="model",
    )

    record = SessionStore(tmp_path).read("compact")

    assert [message.role for message in record.messages] == ["system", "system", "user"]
    assert record.messages[1].content == "[Compacted conversation summary]\nold work summarized"
    assert record.messages[-1].content == "recent request"


def test_multiple_compactions_apply_in_sequence(tmp_path):
    log = SessionLog(tmp_path, session_id="multi")
    log.append_message(Message("system", "system"))
    for index in range(4):
        log.append_message(Message("user", f"request {index}"))
        log.append_message(Message("assistant", f"answer {index}"))
    log.append_compaction(
        summary="summary one",
        replaced_messages=6,
        before_tokens=10,
        after_tokens=1,
        summary_source="model",
    )
    log.append_message(Message("user", "new request"))
    log.append_message(Message("assistant", "new answer"))
    log.append_compaction(
        summary="summary two",
        replaced_messages=3,
        before_tokens=10,
        after_tokens=1,
        summary_source="model",
    )

    record = SessionStore(tmp_path).read("multi")

    assert [message.content for message in record.messages] == [
        "system",
        "summary two",
        "new request",
        "new answer",
    ]


def test_compaction_record_with_bad_replaced_count_is_tolerated(tmp_path):
    log = SessionLog(tmp_path, session_id="bad")
    log.append_message(Message("system", "system"))
    log.append_message(Message("user", "only"))
    log.append_compaction(
        summary="s",
        replaced_messages=999,
        before_tokens=1,
        after_tokens=1,
        summary_source="model",
    )

    record = SessionStore(tmp_path).read("bad")

    assert [message.role for message in record.messages] == ["system", "system"]


def test_session_store_tolerates_trailing_half_line(tmp_path):
    log = SessionLog(tmp_path, session_id="abc")
    log.append_message(Message("user", "hello"))
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"message","message":')

    record = SessionStore(tmp_path).read("abc")

    assert len(record.messages) == 1
    assert record.messages[0].content == "hello"


def test_session_store_read_missing_session_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        SessionStore(tmp_path).read("missing")


@pytest.mark.parametrize("session_id", ["../escape", "nested/session", "/absolute"])
def test_session_ids_cannot_escape_the_session_directory(tmp_path, session_id):
    with pytest.raises(ValueError, match="session id"):
        SessionLog(tmp_path, session_id=session_id)
    with pytest.raises(ValueError, match="session id"):
        SessionStore(tmp_path).read(session_id)


def test_session_log_open_existing_appends_without_rewriting_meta(tmp_path):
    log = SessionLog(tmp_path, session_id="abc", metadata={"first_prompt": "hello"})
    log.append_message(Message("user", "hi"))

    resumed = SessionLog.open_existing(tmp_path, "abc")
    resumed.append_message(Message("assistant", "done"))

    record = SessionStore(tmp_path).read("abc")
    assert [message.role for message in record.messages] == ["user", "assistant"]
    assert record.meta["first_prompt"] == "hello"


def test_session_store_latest_orders_by_mtime(tmp_path):
    SessionLog(tmp_path, session_id="older").append_message(Message("user", "older"))
    SessionLog(tmp_path, session_id="newer").append_message(Message("user", "newer"))
    os.utime(tmp_path / "older.jsonl", (1_000, 1_000))
    os.utime(tmp_path / "newer.jsonl", (2_000, 2_000))

    latest = SessionStore(tmp_path).latest()

    assert latest is not None
    assert latest.session_id == "newer"
    assert latest.message_count == 1
    assert latest.first_prompt == "newer"


def test_session_store_list_skips_corrupt_files(tmp_path):
    SessionLog(tmp_path, session_id="good").append_message(Message("user", "hi"))
    (tmp_path / "junk.jsonl").write_text("not json at all\n", encoding="utf-8")

    summaries = SessionStore(tmp_path).list()

    assert [item.session_id for item in summaries] == ["good"]


# --- detect_interruption ----------------------------------------------------


def test_detect_completed_when_last_assistant_has_no_tool_calls():
    messages = [
        Message("system", "sys"),
        Message("user", "hi"),
        Message("assistant", "ok"),
    ]
    assert detect_interruption(messages) == Interruption.COMPLETED


def test_detect_interrupted_prompt_when_last_message_is_user():
    messages = [Message("system", "sys"), Message("user", "hi")]
    assert detect_interruption(messages) == Interruption.INTERRUPTED_PROMPT


def test_detect_interrupted_turn_when_last_message_is_tool():
    messages = [
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {}),)),
        Message("tool", "files", tool_call_id="c1", name="list_files"),
    ]
    assert detect_interruption(messages) == Interruption.INTERRUPTED_TURN


def test_detect_dangling_when_tool_result_is_missing():
    messages = [
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {}),)),
    ]
    assert detect_interruption(messages) == Interruption.DANGLING_TOOL_CALLS


def test_detect_ignores_tool_results_from_earlier_resolved_calls():
    messages = [
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {}),)),
        Message("tool", "files", tool_call_id="c1", name="list_files"),
        Message("assistant", "", tool_calls=(ToolCall("c2", "read_file", {}),)),
    ]
    assert detect_interruption(messages) == Interruption.DANGLING_TOOL_CALLS


# --- strip_dangling_tool_calls ---------------------------------------------


def test_strip_drops_empty_unresolved_assistant_message():
    messages = [
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {}),)),
    ]
    assert strip_dangling_tool_calls(messages) == [Message("user", "hi")]


def test_strip_keeps_unresolved_assistant_content():
    messages = [
        Message("user", "hi"),
        Message("assistant", "thinking", tool_calls=(ToolCall("c1", "list_files", {}),)),
    ]
    assert strip_dangling_tool_calls(messages) == [
        Message("user", "hi"),
        Message("assistant", "thinking"),
    ]


def test_strip_removes_unresolved_calls_from_earlier_resume_attempts():
    messages = [
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("old", "list_files", {}),)),
        Message("assistant", "", tool_calls=(ToolCall("new", "read_file", {}),)),
    ]

    assert strip_dangling_tool_calls(messages) == [Message("user", "hi")]


def test_strip_leaves_resolved_conversations_untouched():
    messages = [
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {}),)),
        Message("tool", "files", tool_call_id="c1", name="list_files"),
    ]
    assert strip_dangling_tool_calls(messages) == messages


# --- engine resume ----------------------------------------------------------


def test_engine_resume_continues_after_interrupted_turn(tmp_path):
    provider = ScriptedProvider([ModelReply(content="done")])
    history = [
        Message("system", "sys"),
        Message("user", "task"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {"path": "."}),)),
        Message("tool", "ok", tool_call_id="c1", name="list_files"),
    ]
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        messages=history,
    )

    events = collect_resume(loop)

    assert events[-1].kind == "done"
    assert [message.role for message in loop.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert loop.messages[-1].content == "done"


def test_engine_resume_replans_after_stripping_dangling_tool_calls(tmp_path):
    provider = ScriptedProvider([ModelReply(content="done")])
    history = [
        Message("system", "sys"),
        Message("user", "task"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {"path": "."}),)),
    ]
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        messages=strip_dangling_tool_calls(history),
    )

    events = collect_resume(loop)

    assert events[-1].kind == "done"
    # the model must not have seen an unresolved tool call
    assert provider.requests[0][0][-1].role == "user"


def test_engine_resume_appends_new_messages_to_session_log(tmp_path):
    log = SessionLog(tmp_path, session_id="abc")
    provider = ScriptedProvider([ModelReply(content="done")])
    history = [
        Message("system", "sys"),
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {}),)),
        Message("tool", "x", tool_call_id="c1", name="list_files"),
    ]
    for message in history:
        log.append_message(message)
    loop = AgentLoop(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        messages=history,
        session=log,
    )

    collect_resume(loop)

    record = SessionStore(tmp_path).read("abc")
    assert record.messages[-1].content == "done"


def test_engine_persists_stop_hook_retry_message(tmp_path):
    attempts = 0

    def reject_once(context):
        nonlocal attempts
        del context
        attempts += 1
        if attempts == 1:
            return HookResult(blocked=True, reason="verification failed")
        return HookResult()

    hooks = HookRegistry()
    hooks.register(HookEvent.STOP, CallbackHook("verification", reject_once))
    log = SessionLog(tmp_path, session_id="hook-retry")
    loop = AgentLoop(
        provider=ScriptedProvider([ModelReply(content="first"), ModelReply(content="fixed")]),
        tools=default_tools(),
        workspace=tmp_path,
        hooks=hooks,
        session=log,
    )

    collect_run(loop, "task")

    record = SessionStore(tmp_path).read("hook-retry")
    assert "Completion was rejected" in record.messages[-2].content


# --- CLI --------------------------------------------------------------------


def test_cli_sessions_lists_sessions_as_json(tmp_path, capsys):
    log = SessionLog(tmp_path, session_id="abc", metadata={"first_prompt": "hello"})
    log.append_message(Message("user", "hello"))

    code = cli.main(["sessions", "--session-dir", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["session_id"] == "abc"
    assert payload[0]["first_prompt"] == "hello"


def test_cli_run_no_session_creates_no_session_file(tmp_path):
    code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--demo",
            "--no-trace",
            "--no-session",
            "hello",
        ]
    )

    assert code == 0
    assert not list((tmp_path / ".mini-oh" / "sessions").glob("*.jsonl"))


def test_cli_run_session_dir_places_session_file(tmp_path):
    session_dir = tmp_path / "sessions"
    code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--demo",
            "--no-trace",
            "--session-dir",
            str(session_dir),
            "hello",
        ]
    )

    assert code == 0
    files = list(session_dir.glob("*.jsonl"))
    assert len(files) == 1
    record = SessionStore(session_dir).read(files[0].stem)
    assert record.messages[0].role == "user"
    assert record.messages[0].content == "hello"
    assert cli._ACTIVE_SESSION is None


def test_cli_resume_reports_completed_session(tmp_path, capsys):
    log = SessionLog(tmp_path, session_id="abc", metadata={"first_prompt": "hello"})
    log.append_message(Message("user", "hi"))
    log.append_message(Message("assistant", "done"))

    code = cli.main(["resume", "--session-dir", str(tmp_path), "--demo", "--no-trace", "abc"])

    assert code == 0
    assert "already completed" in capsys.readouterr().err


def test_cli_resume_continues_interrupted_turn(tmp_path):
    log = SessionLog(tmp_path, session_id="abc", metadata={"first_prompt": "hi"})
    log.append_message(Message("user", "hi"))
    log.append_message(
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {"path": "."}),))
    )
    log.append_message(Message("tool", "ok", tool_call_id="c1", name="list_files"))

    code = cli.main(["resume", "--session-dir", str(tmp_path), "--demo", "--no-trace", "abc"])

    assert code == 0
    record = SessionStore(tmp_path).read("abc")
    assert record.messages[-1].role == "assistant"
    assert record.messages[-1].content.startswith("演示完成")


def test_cli_resume_defaults_to_latest_session(tmp_path):
    log = SessionLog(tmp_path, session_id="abc", metadata={"first_prompt": "hi"})
    log.append_message(Message("user", "hi"))
    log.append_message(
        Message("assistant", "", tool_calls=(ToolCall("c1", "list_files", {"path": "."}),))
    )
    log.append_message(Message("tool", "ok", tool_call_id="c1", name="list_files"))

    code = cli.main(["resume", "--session-dir", str(tmp_path), "--demo", "--no-trace"])

    assert code == 0
    record = SessionStore(tmp_path).read("abc")
    assert record.messages[-1].role == "assistant"


def test_cli_resume_with_no_sessions_returns_nonzero(tmp_path, capsys):
    code = cli.main(["resume", "--session-dir", str(tmp_path), "--demo", "--no-trace"])

    assert code == 1
    assert "no sessions found" in capsys.readouterr().err


def test_cli_interrupt_prints_resume_hint_and_returns_130(tmp_path, monkeypatch, capsys):
    async def interrupted(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_drive_session", interrupted)

    code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--demo",
            "--no-trace",
            "--session-dir",
            str(tmp_path),
            "hello",
        ]
    )

    assert code == 130
    output = capsys.readouterr()
    assert "Resume this session with: wqb resume" in output.out + output.err


def test_handle_sigterm_prints_hint_and_exits_143(tmp_path, monkeypatch, capsys):
    log = SessionLog(tmp_path, session_id="sigterm-test")
    monkeypatch.setattr(cli, "_ACTIVE_SESSION", log)
    exits: dict[str, int] = {}
    monkeypatch.setattr(cli.sys, "exit", lambda code: exits.setdefault("code", code))

    cli._handle_sigterm(signal.SIGTERM, None)

    assert exits["code"] == 143
    assert "sigterm-test" in capsys.readouterr().err
