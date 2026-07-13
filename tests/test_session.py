from mini_openharness.models import Message, ToolCall
from mini_openharness.session import SessionStore


def test_session_round_trip(tmp_path):
    store = SessionStore(tmp_path / "sessions" / "latest.json")
    messages = [
        Message("user", "inspect"),
        Message("assistant", tool_calls=(ToolCall("1", "read_file", {"path": "a"}),)),
        Message("tool", "content", tool_call_id="1", name="read_file"),
    ]
    store.save(messages)
    assert store.load() == messages
