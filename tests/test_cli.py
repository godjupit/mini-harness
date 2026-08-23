import asyncio
import json
import os
import pty
import threading
import time
from types import SimpleNamespace

import pytest

import mini_openharness.cli as cli
from mini_openharness.agent_profile import AgentProfile, PermissionPolicy
from mini_openharness.cli import _load_environment, build_run_parser
from mini_openharness.engine import AgentEvent
from mini_openharness.models import ModelReply
from mini_openharness.permissions import (
    PermissionBehavior,
    PermissionDecision,
    PermissionEngine,
    PermissionRequest,
)
from mini_openharness.provider import ProviderError
from mini_openharness.tools import ToolRegistry, ToolSearchTool


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
    args = build_run_parser().parse_args(["--demo", "--workspace", str(tmp_path), "--no-trace"])

    async def build():
        loop, tracer, mcp_manager, provider = await cli._build_runtime(
            args,
            session_log=None,
            trace_prompt="probe",
        )
        try:
            names = {schema["name"] for schema in loop.tools.schemas()}
            assert "agent" in names
            assert loop.tools.descriptor("agent").effect == "compute"
        finally:
            await cli._close_runtime(mcp_manager, provider)

    asyncio.run(build())


def test_runtime_system_prompt_has_goal_discipline(tmp_path):
    args = build_run_parser().parse_args(
        ["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"]
    )

    async def build():
        loop, tracer, mcp_manager, provider = await cli._build_runtime(
            args,
            session_log=None,
            trace_prompt="probe",
        )
        try:
            system = loop.messages[0].content
            assert "authoritative goal" in system
            assert "STOP TOOL CALLING" in system
            assert "RETURN FINAL" in system
            assert ".mini-oh" in system
        finally:
            await cli._close_runtime(mcp_manager, provider)

    asyncio.run(build())


def test_runtime_uses_explicit_artifact_directory(tmp_path):
    artifact_dir = tmp_path / "agent-data" / "artifacts"
    args = build_run_parser().parse_args(
        [
            "--demo",
            "--workspace",
            str(tmp_path),
            "--artifact-dir",
            str(artifact_dir),
            "--no-trace",
            "--no-session",
        ]
    )

    async def build():
        loop, tracer, mcp_manager, provider = await cli._build_runtime(
            args,
            session_log=None,
            trace_prompt="probe",
        )
        try:
            assert loop.artifact_store.root == artifact_dir.resolve()
        finally:
            await cli._close_runtime(mcp_manager, provider)

    asyncio.run(build())


def test_runtime_appends_custom_system_prompt_file(tmp_path):
    prompt_file = tmp_path / "homestay.md"
    prompt_file.write_text("Use real homestay search results only.", encoding="utf-8")
    args = build_run_parser().parse_args(
        [
            "--demo",
            "--workspace",
            str(tmp_path),
            "--no-trace",
            "--no-session",
            "--system-prompt-file",
            str(prompt_file),
        ]
    )

    async def build():
        loop, tracer, mcp_manager, provider = await cli._build_runtime(
            args,
            session_log=None,
            trace_prompt="probe",
        )
        try:
            assert "Use real homestay search results only." in loop.messages[0].content
        finally:
            await cli._close_runtime(mcp_manager, provider)

    asyncio.run(build())


def test_agent_profile_replaces_coding_prompt_and_selects_tools(tmp_path):
    def profile_tools():
        registry = ToolRegistry()
        registry.register(ToolSearchTool(registry))
        return registry

    profile = AgentProfile(
        name="homestay",
        system_prompt="You are a homestay assistant.",
        tool_factory=profile_tools,
        permission_policy=PermissionPolicy.HUMAN_APPROVAL,
        max_steps=7,
    )
    args = build_run_parser().parse_args(
        ["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"]
    )

    async def build():
        loop, tracer, mcp_manager, provider = await cli._build_runtime(
            args,
            session_log=None,
            trace_prompt="probe",
            profile=profile,
        )
        try:
            system = loop.messages[0].content
            assert system.startswith("You are a homestay assistant.")
            assert "You are Mini Harness" not in system
            assert "OUTPUT PROTOCOL: markdown" in system
            assert {name for name, _ in loop.tools.items()} == {"tool_search"}
            assert loop.max_steps == 7
            assert loop.permission_engine.context.mode.value == "default"
        finally:
            await cli._close_runtime(mcp_manager, provider)

    asyncio.run(build())


def test_agent_profile_uses_isolated_skills_and_memory(tmp_path):
    skills_dir = tmp_path / "assets" / "skills"
    skill_dir = skills_dir / "booking"
    memory_dir = tmp_path / "assets" / "memory"
    skill_dir.mkdir(parents=True)
    memory_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: booking\ndescription: Confirm a booking.\n---\n\n# Booking\n",
        encoding="utf-8",
    )
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Guest](user_guest.md) — Prefers quiet rooms.\n",
        encoding="utf-8",
    )

    profile = AgentProfile(
        name="homestay",
        system_prompt="You are a homestay assistant.",
        tool_factory=ToolRegistry,
        enable_skills=True,
        enable_memory_prompt=True,
        skills_dir=str(skills_dir),
        memory_dir=str(memory_dir),
    )
    args = build_run_parser().parse_args(
        ["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"]
    )

    async def build():
        loop, tracer, mcp_manager, provider = await cli._build_runtime(
            args,
            session_log=None,
            trace_prompt="probe",
            profile=profile,
        )
        try:
            system = loop.messages[0].content
            assert "booking: Confirm a booking." in system
            assert "Prefers quiet rooms." in system
            assert loop.memory_dir == memory_dir.resolve()
            assert {name for name, _ in loop.tools.items()} == {
                "load_skill",
                "memory_read",
                "memory_write",
            }
        finally:
            await cli._close_runtime(mcp_manager, provider)

    asyncio.run(build())


def test_profile_permission_config_allows_order_creation_after_business_confirmation(tmp_path):
    config = tmp_path / "homestay-permissions.json"
    config.write_text(
        """{
  "rules": [
    {"tool": "mcp__homestay__create_homestay_order", "action": "allow"},
    {"tool": "*", "action": "allow"}
  ]
}
""",
        encoding="utf-8",
    )
    profile = AgentProfile(
        name="homestay",
        system_prompt="You are a homestay assistant.",
        tool_factory=ToolRegistry,
        permission_policy=PermissionPolicy.HUMAN_APPROVAL,
        permission_config=str(config),
    )
    args = build_run_parser().parse_args(["--demo", "--workspace", str(tmp_path)])
    context = cli._permission_context(args, profile)
    engine = PermissionEngine(context)

    create = engine.authorize(
        PermissionRequest(
            tool_name="mcp__homestay__create_homestay_order",
            input={},
            source="mcp",
            effect="remote",
        )
    )
    payment = engine.authorize(
        PermissionRequest(
            tool_name="mcp__homestay__confirm_demo_payment",
            input={},
            source="mcp",
            effect="remote",
        )
    )
    search = engine.authorize(
        PermissionRequest(
            tool_name="mcp__homestay__search_homestays",
            input={},
            source="mcp",
            effect="remote",
        )
    )

    assert create.behavior == PermissionBehavior.ALLOW
    assert payment.behavior == PermissionBehavior.ALLOW
    assert search.behavior == PermissionBehavior.ALLOW


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
    assert "⚖ reviewer: approve — shell npm publish" in capsys.readouterr().out


def test_reviewer_verdict_line_truncates_long_commands(tmp_path, capsys):
    args = build_run_parser().parse_args(["--workspace", str(tmp_path)])

    class FakeProvider:
        async def complete(self, messages, tools):
            del messages, tools
            return SimpleNamespace(content="approve")

    reviewer = cli._build_reviewer(args, FakeProvider())
    long_command = "python - <<'EOF'\n" + "x" * 500 + "\nEOF"
    request = PermissionRequest(
        tool_name="sandbox_shell",
        input={"command": long_command},
        command=long_command,
        effect="write",
    )
    decision = PermissionDecision(PermissionBehavior.ASK, "needs review")

    asyncio.run(reviewer(request, decision))

    out = capsys.readouterr().out
    assert "…" in out
    assert out.count("\n") == 1  # 只有结尾换行，命令本身不换行
    assert "x" * 500 not in out


def test_reviewer_reject_line_includes_reason(tmp_path, capsys):
    args = build_run_parser().parse_args(["--workspace", str(tmp_path)])

    class FakeProvider:
        async def complete(self, messages, tools):
            del messages, tools
            return SimpleNamespace(content="reject")

    reviewer = cli._build_reviewer(args, FakeProvider())
    request = PermissionRequest(
        tool_name="sandbox_shell",
        input={"command": "git push"},
        command="git push",
        effect="write",
    )
    decision = PermissionDecision(PermissionBehavior.ASK, "subcommand is not routine-safe")

    asyncio.run(reviewer(request, decision))

    out = capsys.readouterr().out
    assert "⚖ reviewer: reject — shell git push (subcommand is not routine-safe)" in out


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

    code = cli.main(["--demo", "--workspace", str(tmp_path), "--no-trace", "--no-session"])

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


def test_tool_start_does_not_print_write_content(capsys):
    cli._print_event(
        AgentEvent(
            "tool_start",
            "",
            {
                "name": "write_file",
                "input": {"path": "notes/ok.txt", "content": "SECRET CONTENT"},
            },
        )
    )

    out = capsys.readouterr().out
    assert "SECRET CONTENT" not in out
    assert "write_file notes/ok.txt (14 chars)" in out


def test_print_event_reasoning_flips_status_and_tool_call_start_is_immediate(capsys):
    cli._THINKING_LINE_OPEN = False
    cli._REASONING_LINE_SHOWN = False
    cli._print_event(AgentEvent("model_start", data={"step": 1, "attempt": 1}))
    cli._print_event(AgentEvent("reasoning_delta", "hidden reasoning", {"step": 1}))
    cli._print_event(
        AgentEvent(
            "tool_call_start",
            data={"step": 1, "index": 0, "name": "read_file", "call_id": "call-1"},
        )
    )

    out = capsys.readouterr().out
    assert "model reasoning" in out
    assert "→ read_file" in out
    assert "hidden reasoning" not in out


def test_print_event_first_token_after_reasoning_keeps_reasoning_label(capsys):
    cli._THINKING_LINE_OPEN = False
    cli._REASONING_LINE_SHOWN = False
    cli._print_event(AgentEvent("model_start", data={"step": 1, "attempt": 1}))
    cli._print_event(AgentEvent("reasoning_delta", "r", {"step": 1}))
    cli._print_event(AgentEvent("first_token", data={"ttft_ms": 1234.0}))

    out = capsys.readouterr().out
    assert "model reasoning... 1.2s" in out


def test_print_event_tool_call_start_without_name_shows_index(capsys):
    cli._THINKING_LINE_OPEN = False
    cli._REASONING_LINE_SHOWN = False
    cli._print_event(
        AgentEvent(
            "tool_call_start",
            data={"step": 1, "index": 2, "name": None, "call_id": None},
        )
    )

    out = capsys.readouterr().out
    assert "→ tool_call[2]" in out


def test_sandbox_shell_output_is_hidden(capsys):
    cli._print_event(
        AgentEvent(
            "tool_end",
            "huge output that should not be shown\nmore lines",
            {
                "name": "sandbox_shell",
                "input": {"command": "ls"},
                "is_error": False,
                "elapsed_ms": 5,
                "returncode": 0,
            },
        )
    )

    out = capsys.readouterr().out
    assert "huge output" not in out
    assert "exit 0" in out


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


def test_read_line_bracketed_paste_keeps_multiline():
    master, slave = pty.openpty()

    def type_paste():
        time.sleep(0.05)
        os.write(
            master,
            b"\x1b[200~first line\nsecond line\x1b[201~\r",
        )

    threading.Thread(target=type_paste, daemon=True).start()
    try:
        line = cli._read_line("> ", in_fd=slave, out_fd=slave)
    finally:
        os.close(master)
        os.close(slave)

    assert line == "first line\nsecond line"
