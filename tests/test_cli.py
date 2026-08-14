import asyncio
import json
import os
import pty
import threading
import time
from types import SimpleNamespace

import pytest

import mini_openharness.cli as cli
from mini_openharness.cli import _load_environment, build_run_parser
from mini_openharness.models import ModelReply
from mini_openharness.permissions import (
    PermissionBehavior,
    PermissionDecision,
    PermissionRequest,
)
from mini_openharness.provider import ProviderError


class RecordingDemoProvider:
    """Drop-in DemoProvider replacement that records every request."""

    last = None

    def __init__(self) -> None:
        self.requests: list[list[str]] = []
        type(self).last = self

    async def complete(self, messages, tools):
        del tools
        self.requests.append(
            [message.content for message in messages if message.role in {"user", "assistant"}]
        )
        return ModelReply(content="ok")

    async def close(self):
        pass


def test_local_dotenv_is_loaded_without_overriding_shell(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=from-file\nOPENAI_MODEL=from-file-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "from-shell")

    _load_environment()

    assert os.environ["OPENAI_API_KEY"] == "from-file"
    assert os.environ["OPENAI_MODEL"] == "from-shell"


def test_cli_defaults_sandbox_shell_on_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("OPENAI_API_MODE", raising=False)

    defaults = build_run_parser().parse_args([])
    disabled = build_run_parser().parse_args(["--no-sandbox-shell"])
    strict_trace = build_run_parser().parse_args(["--strict-trace"])

    assert defaults.api_mode == "responses"
    assert defaults.sandbox_shell is True
    assert defaults.strict_trace is False
    assert disabled.sandbox_shell is False
    assert defaults.auto_review is True
    assert build_run_parser().parse_args(["--no-auto-review"]).auto_review is False
    assert defaults.sandbox_network is True
    assert defaults.sandbox_writable is True
    assert defaults.sandbox_root is True
    assert build_run_parser().parse_args(["--no-sandbox-network"]).sandbox_network is False
    assert build_run_parser().parse_args(["--no-sandbox-writable"]).sandbox_writable is False
    assert build_run_parser().parse_args(["--no-sandbox-root"]).sandbox_root is False
    assert defaults.sandbox_persistent is False
    assert build_run_parser().parse_args(["--sandbox-persistent"]).sandbox_persistent is True
    assert strict_trace.strict_trace is True


def test_cli_accepts_hook_configuration():
    args = build_run_parser().parse_args(["--hooks-config", "hooks.json"])

    assert args.hooks_config == "hooks.json"


def test_cli_accepts_only_positive_tool_concurrency():
    defaults = build_run_parser().parse_args([])
    configured = build_run_parser().parse_args(["--max-concurrent-tools", "3"])

    assert defaults.max_concurrent_tools == 8
    assert configured.max_concurrent_tools == 3
    with pytest.raises(SystemExit):
        build_run_parser().parse_args(["--max-concurrent-tools", "0"])


def test_cli_provider_error_returns_nonzero_and_hints_on_responses_404(
    tmp_path, monkeypatch, capsys
):
    class MissingResponsesEndpoint:
        def __init__(self, **kwargs):
            del kwargs

        async def complete(self, messages, tools):
            del messages, tools
            raise ProviderError("HTTP 404: not found")

        async def close(self):
            pass

    monkeypatch.setattr(cli, "OpenAIResponsesProvider", MissingResponsesEndpoint)

    exit_code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--api-key",
            "test-key",
            "--api-mode",
            "responses",
            "--no-trace",
            "probe",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error: HTTP 404" in captured.err
    assert "--api-mode chat" in captured.err


def test_cli_runtime_registers_the_agent_tool(tmp_path):
    args = build_run_parser().parse_args(
        ["--demo", "--workspace", str(tmp_path), "--no-trace"]
    )

    async def build():
        loop, tracer, mcp_manager, provider, sandbox = await cli._build_runtime(
            args,
            session_log=None,
            trace_prompt="probe",
        )
        try:
            names = {schema["name"] for schema in loop.tools.schemas()}
            assert "agent" in names
            assert loop.tools.descriptor("agent").effect == "compute"
        finally:
            await cli._close_runtime(mcp_manager, provider, sandbox)

    asyncio.run(build())


def test_reviewer_prompt_includes_request_details(tmp_path, capsys):
    args = build_run_parser().parse_args(["--workspace", str(tmp_path)])
    captured = {}

    class FakeProvider:
        async def complete(self, messages, tools):
            del tools
            captured["prompt"] = messages[0].content
            return SimpleNamespace(content="approve")

    reviewer = cli._build_reviewer(args, FakeProvider())
    request = PermissionRequest(
        tool_name="sandbox_shell",
        input={"command": "npm publish"},
        source="sandbox",
        effect="write",
        destructive=True,
        path=None,
        command="npm publish",
    )
    decision = PermissionDecision(PermissionBehavior.ASK, "needs review")

    approved = asyncio.run(reviewer(request, decision))

    assert approved is True
    prompt = captured["prompt"]
    assert "tool: sandbox_shell" in prompt
    assert "command: npm publish" in prompt
    assert "path: None" in prompt
    assert f"workspace: {tmp_path.resolve()}" in prompt
    assert "effect: write" in prompt
    assert "reason: needs review" in prompt
    assert "⚖ reviewer: approve — sandbox_shell npm publish (needs review)" in capsys.readouterr().out


def test_single_shot_with_prompt_does_not_enter_repl(tmp_path, capsys):
    code = cli.main(
        [
            "--demo",
            "--workspace",
            str(tmp_path),
            "--no-trace",
            "--no-session",
            "single shot",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "done in" in out
    assert "workspace:" not in out  # REPL banner must not appear


def test_no_prompt_enters_interactive_repl(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DemoProvider", RecordingDemoProvider)
    monkeypatch.setattr("builtins.input", lambda prompt="": "/exit")

    code = cli.main(
        ["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "workspace:" in out
    assert RecordingDemoProvider.last.requests == []


def test_interactive_keeps_conversation_and_one_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DemoProvider", RecordingDemoProvider)
    inputs = iter(["first turn", "second turn", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    args = build_run_parser().parse_args(
        [
            "--demo",
            "--workspace",
            str(tmp_path),
            "--no-trace",
            "--session-dir",
            str(tmp_path / "sessions"),
        ]
    )

    code = asyncio.run(cli._interactive(args))

    assert code == 0
    out = capsys.readouterr().out
    assert "workspace:" in out
    assert "session:" in out

    provider = RecordingDemoProvider.last
    assert len(provider.requests) == 2
    # second turn's model request contains the first turn history
    assert provider.requests[1] == ["first turn", "ok", "second turn"]

    files = list((tmp_path / "sessions").glob("*.jsonl"))
    assert len(files) == 1
    lines = [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    user_turns = [
        line["message"]["content"]
        for line in lines
        if line["type"] == "message" and line["message"]["role"] == "user"
    ]
    assert user_turns == ["first turn", "second turn"]


def test_interactive_help_does_not_call_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DemoProvider", RecordingDemoProvider)
    inputs = iter(["/help", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    args = build_run_parser().parse_args(
        ["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"]
    )

    code = asyncio.run(cli._interactive(args))

    assert code == 0
    assert RecordingDemoProvider.last.requests == []


def test_interactive_eof_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DemoProvider", RecordingDemoProvider)

    def eof(prompt=""):
        del prompt
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    args = build_run_parser().parse_args(
        ["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"]
    )

    code = asyncio.run(cli._interactive(args))

    assert code == 0
    assert RecordingDemoProvider.last.requests == []


def test_interactive_ctrl_c_at_prompt_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DemoProvider", RecordingDemoProvider)

    def ctrl_c(prompt=""):
        del prompt
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", ctrl_c)
    args = build_run_parser().parse_args(
        ["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"]
    )

    code = asyncio.run(cli._interactive(args))

    assert code == 0
    assert RecordingDemoProvider.last.requests == []


def test_read_line_backspace_removes_full_multibyte_char():
    master, slave = pty.openpty()

    def type_input():
        time.sleep(0.05)
        os.write(master, "你好吗".encode("utf-8"))
        os.write(master, b"\x7f\x7f")  # 退格两次，应删除"吗"和"好"
        os.write(master, "!".encode("utf-8"))
        os.write(master, b"\r")

    threading.Thread(target=type_input, daemon=True).start()
    try:
        line = cli._read_line("> ", in_fd=slave, out_fd=slave)
    finally:
        os.close(master)
        os.close(slave)

    assert line == "你!"
