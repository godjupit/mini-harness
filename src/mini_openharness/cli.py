"""CLI for agent runs and trace inspection/replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mini_openharness.compaction import ArtifactStore, ContextCompactor
from mini_openharness.engine import AgentEvent, AgentLoop, MaxStepsExceeded
from mini_openharness.hooks import load_hook_registry
from mini_openharness.mcp import McpManager
from mini_openharness.models import Message
from mini_openharness.multiagent import build_agent_tool
from mini_openharness.permissions import (
    AgentApprovalHandler,
    HumanApprovalHandler,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    build_default_rules,
    load_rules_from_json,
)
from mini_openharness.provider import (
    DemoProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
)
from mini_openharness.sandbox import (
    BwrapShell,
    SandboxUnavailableError,
    SandboxedShellTool,
)
from mini_openharness.session import (
    Interruption,
    SessionLog,
    SessionStore,
    detect_interruption,
    strip_dangling_tool_calls,
)
from mini_openharness.skills import LoadSkillTool, SkillCatalog
from mini_openharness.tools import default_tools
from mini_openharness.trace import TraceStore, TraceWriter


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


_ACTIVE_SESSION: SessionLog | None = None


def _print_resume_hint() -> None:
    if _ACTIVE_SESSION is not None:
        print(
            f"Resume this session with: mini-oh resume {_ACTIVE_SESSION.session_id}",
            file=sys.stderr,
        )


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-oh", description="A tiny coding-agent harness")
    parser.add_argument("prompt", nargs="?", help="Task for the coding agent")
    _add_agent_arguments(parser)
    return parser


def _add_agent_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--demo", action="store_true", help="Run offline deterministic tool demo")
    parser.add_argument("--workspace", default=".", help="Agent workspace boundary")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
    parser.add_argument(
        "--api-mode",
        choices=("responses", "chat"),
        default=os.getenv("OPENAI_API_MODE", "responses"),
        help="OpenAI Responses API (default) or compatible Chat Completions",
    )
    parser.add_argument(
        "--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--auto-review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="AUTO_REVIEW mode (default): ASK decisions go to an independent reviewer agent; "
        "use --no-auto-review to ask the user",
    )
    parser.add_argument("--permission-config", help="JSON allow/deny/ask rules")
    parser.add_argument("--hooks-config", help="JSON lifecycle command hooks")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--tool-timeout", type=float, default=30.0)
    parser.add_argument("--max-repeated-tool-batches", type=int, default=3)
    parser.add_argument("--max-concurrent-tools", type=_positive_int, default=8)
    parser.add_argument(
        "--sandbox-shell",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable bwrap-sandboxed host shell (default); use --no-sandbox-shell to disable",
    )
    parser.add_argument("--context-threshold", type=int, default=800_000)
    parser.add_argument("--keep-recent", type=int, default=6)
    parser.add_argument("--max-inline-output", type=int, default=8_000)
    parser.add_argument("--input-cost", type=float, default=0.0, help="USD per million tokens")
    parser.add_argument("--output-cost", type=float, default=0.0, help="USD per million tokens")
    parser.add_argument("--skills-dir", help="Directory containing <name>/SKILL.md skills")
    parser.add_argument("--mcp-config", help="JSON file containing stdio/HTTP mcpServers")
    parser.add_argument("--trace-dir", help="JSONL trace directory")
    parser.add_argument("--no-trace", action="store_true", help="Disable run tracing")
    parser.add_argument(
        "--strict-trace",
        action="store_true",
        help="Fail the run when a local trace cannot be written",
    )
    parser.add_argument(
        "--unsafe-trace-secrets",
        action="store_true",
        help="Disable default secret redaction in local traces",
    )
    parser.add_argument("--no-session", action="store_true", help="Disable session persistence")
    parser.add_argument("--session-dir", help="JSONL conversation session directory")


def build_trace_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-oh trace", description="Inspect run traces")
    parser.add_argument("action", choices=("list", "show", "replay", "prune"))
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--trace-dir", default=".mini-oh/traces")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--older-than", type=_positive_float, metavar="DAYS", help="Prune completed runs by age"
    )
    parser.add_argument(
        "--max-runs", type=_positive_int, help="Keep at most this many completed runs"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Delete prune candidates; default is dry-run"
    )
    return parser


def build_sessions_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-oh sessions", description="List conversation sessions")
    parser.add_argument("--session-dir", help="JSONL conversation session directory")
    parser.add_argument("--json", action="store_true")
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-oh resume", description="Resume an interrupted conversation session"
    )
    parser.add_argument("session_id", nargs="?", help="Session id; defaults to the most recent")
    parser.add_argument("--latest", action="store_true", help="Resume the most recent session")
    _add_agent_arguments(parser)
    return parser


def _session_dir(args: argparse.Namespace) -> Path:
    workspace = Path(args.workspace).resolve()
    return Path(args.session_dir or workspace / ".mini-oh" / "sessions").resolve()


async def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    prompt = args.prompt
    session_log = None
    if not args.no_session:
        session_log = SessionLog(
            _session_dir(args),
            metadata={"first_prompt": prompt, "workspace": str(workspace)},
        )
        global _ACTIVE_SESSION
        _ACTIVE_SESSION = session_log
    return await _drive_session(
        args,
        session_log=session_log,
        messages=None,
        trace_prompt=prompt,
        run_events=lambda loop: loop.run(prompt),
    )


async def _interactive(args: argparse.Namespace) -> int:
    """Continuous REPL: one runtime / AgentLoop / Session for many prompts."""
    workspace = Path(args.workspace).resolve()
    session_log = None
    if not args.no_session:
        session_log = SessionLog(
            _session_dir(args),
            metadata={"first_prompt": "(interactive)", "interactive": True},
        )
        global _ACTIVE_SESSION
        _ACTIVE_SESSION = session_log

    loop = None
    tracer = None
    mcp_manager = None
    provider = None
    try:
        loop, tracer, mcp_manager, provider = await _build_runtime(
            args,
            session_log=session_log,
            trace_prompt="(interactive session)",
        )
        if mcp_manager:
            registered = await mcp_manager.connect_and_register(loop.tools)
            print(f"connected MCP tools: {', '.join(registered) or '(none)'}")

        print("mini-openharness")
        print(f"workspace: {workspace}")
        if session_log is not None:
            print(f"session: {session_log.session_id}")
        print("commands: /exit, /quit, /help")

        while True:
            # 上一轮输出可能以不带换行的流式文本结束；先开新行，
            # 避免提示符粘在残留字符后面（那些字符无法被删除）。
            print(flush=True)
            try:
                raw = _read_line("❯ ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                break
            prompt = raw.strip()
            if not prompt:
                continue
            if prompt in {"/exit", "/quit"}:
                break
            if prompt == "/help":
                print("commands: /exit, /quit, /help")
                continue
            try:
                await _consume_events(loop, loop.run(prompt), args)
            except MaxStepsExceeded as exc:
                print(f"error: {exc}", file=sys.stderr)
            except KeyboardInterrupt:
                loop.cancel()
                print("cancelled", file=sys.stderr)
    finally:
        await _close_runtime(mcp_manager, provider)
        _ACTIVE_SESSION = None
    if tracer:
        print(f"trace: {tracer.path}")
    if session_log is not None:
        print(f"session: {session_log.session_id}")
    return 0


def _read_line(
    prompt: str,
    *,
    in_fd: int | None = None,
    out_fd: int | None = None,
) -> str:
    """Read one line with full UTF-8 backspace support.

    Canonical terminal input erases one *byte* per backspace, which leaves
    partial characters behind for multibyte input. This helper switches the
    terminal to cbreak mode and redraws the line itself, so backspace always
    removes one complete character. Non-TTY input (pipes, tests) falls back to
    plain ``input()``.
    """
    if in_fd is None:
        if not sys.stdin.isatty():
            return input(prompt)
        in_fd = sys.stdin.fileno()
    if out_fd is None:
        out_fd = sys.stdout.fileno()
    try:
        import contextlib
        import tty
    except ImportError:
        return input(prompt)

    buffer = bytearray()
    try:
        with contextlib.ExitStack() as stack:
            previous = tty.tcgetattr(in_fd)
            stack.callback(tty.tcsetattr, in_fd, tty.TCSADRAIN, previous)
            tty.setcbreak(in_fd)
            os.write(out_fd, prompt.encode("utf-8"))
            while True:
                chunk = os.read(in_fd, 1)
                if not chunk:
                    raise EOFError
                byte = chunk[0]
                if byte in (13, 10):
                    os.write(out_fd, b"\r\n")
                    break
                if byte in (127, 8):  # DEL / backspace: erase one full character
                    if buffer:
                        text = buffer.decode("utf-8", errors="ignore")
                        previous_text = text
                        text = text[:-1]
                        buffer = bytearray(text.encode("utf-8"))
                        erase = " " * (len(previous_text) - len(text) + 1)
                        os.write(
                            out_fd,
                            f"\r{prompt}{text}{erase}\r{prompt}{text}".encode("utf-8"),
                        )
                    continue
                if byte == 4 and not buffer:  # Ctrl+D on an empty line = EOF
                    raise EOFError
                if byte == 27:  # ignore escape sequences (arrow keys etc.)
                    seq = os.read(in_fd, 2)
                    if seq and seq[0] == 91:
                        while True:
                            tail = os.read(in_fd, 1)
                            if not tail or 64 <= tail[0] < 127:
                                break
                    continue
                buffer.append(byte)
                os.write(out_fd, bytes([byte]))
    except KeyboardInterrupt:
        os.write(out_fd, b"\r\n")
        raise
    return buffer.decode("utf-8", errors="replace").strip()


async def _resume_command(args: argparse.Namespace) -> int:
    store = SessionStore(_session_dir(args))
    latest = store.latest()
    session_id = args.session_id or (latest.session_id if latest else None)
    if not session_id:
        print("no sessions found; run `mini-oh` with a prompt first", file=sys.stderr)
        return 1
    try:
        record = store.read(session_id)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    messages = list(record.messages)
    interruption = detect_interruption(messages)
    if interruption == Interruption.COMPLETED:
        print(f"session {session_id} already completed", file=sys.stderr)
        return 0
    if interruption == Interruption.DANGLING_TOOL_CALLS:
        messages = strip_dangling_tool_calls(messages)
    session_log = SessionLog.open_existing(_session_dir(args), session_id)
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = session_log
    first_prompt = str(record.meta.get("first_prompt", ""))
    print(f"resuming session {session_id} from: {interruption.value}", file=sys.stderr)
    return await _drive_session(
        args,
        session_log=session_log,
        messages=messages,
        trace_prompt=first_prompt or "(resumed session)",
        run_events=lambda loop: loop.resume(),
    )


async def _drive_session(
    args: argparse.Namespace,
    *,
    session_log: SessionLog | None,
    messages: list[Message] | None,
    trace_prompt: str,
    run_events: Any,
) -> int:
    global _ACTIVE_SESSION
    loop = None
    tracer = None
    mcp_manager = None
    provider = None
    exit_code = 1
    try:
        loop, tracer, mcp_manager, provider = await _build_runtime(
            args,
            session_log=session_log,
            messages=messages,
            trace_prompt=trace_prompt,
        )
        if mcp_manager:
            registered = await mcp_manager.connect_and_register(loop.tools)
            print(f"connected MCP tools: {', '.join(registered) or '(none)'}")
        exit_code = await _consume_events(loop, run_events(loop), args)
    except MaxStepsExceeded as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        if loop:
            loop.cancel()
        _print_resume_hint()
        return 130
    except asyncio.CancelledError:
        if loop:
            loop.cancel()
        if tracer:
            tracer.finish(status="cancelled", data={"reason": "CLI task cancelled"})
        _print_resume_hint()
        raise
    finally:
        await _close_runtime(mcp_manager, provider)
        _ACTIVE_SESSION = None
    if tracer:
        print(f"trace: {tracer.path}")
    if session_log is not None:
        print(f"session: {session_log.session_id}")
    return exit_code


async def _consume_events(loop: AgentLoop, events: Any, args: argparse.Namespace) -> int:
    exit_code = 1
    async for event in events:
        _print_event(event)
        if event.kind == "done":
            exit_code = 0
        elif event.kind == "cancelled":
            exit_code = 130
        elif event.kind == "error":
            _print_provider_hint(args, event)
            exit_code = 1
    return exit_code


async def _build_runtime(
    args: argparse.Namespace,
    *,
    session_log: SessionLog | None,
    messages: list[Message] | None = None,
    trace_prompt: str,
) -> tuple[AgentLoop, TraceWriter | None, McpManager | None, Any]:
    workspace = Path(args.workspace).resolve()
    skills = SkillCatalog(args.skills_dir or workspace / "skills")
    system_parts = [
        "You are a concise coding assistant. Inspect before editing.",
        (
            "Goal discipline: the latest user request is the authoritative goal. "
            "Do not inspect .mini-oh internal state unless explicitly asked. "
            "When implementation and verification satisfy the request: "
            "STOP TOOL CALLING and RETURN FINAL."
        ),
    ]
    skill_prompt = skills.prompt()
    if skill_prompt:
        system_parts.append(skill_prompt)

    if args.demo:
        provider = DemoProvider()
        provider_name = "demo"
    else:
        if not args.api_key:
            raise SystemExit("OPENAI_API_KEY is required unless --demo is used")
        provider_class = (
            OpenAIResponsesProvider if args.api_mode == "responses" else OpenAICompatibleProvider
        )
        provider = provider_class(
            api_key=args.api_key,
            model=args.model,
            base_url=args.base_url,
            timeout=args.timeout,
            max_retries=args.max_retries,
        )
        provider_name = f"openai-{args.api_mode}"

    trace_dir = Path(args.trace_dir or workspace / ".mini-oh" / "traces")
    tracer = None
    if not args.no_trace:
        tracer = TraceWriter(
            trace_dir,
            redact_secrets=not args.unsafe_trace_secrets,
            strict=args.strict_trace,
            metadata={
                "prompt": trace_prompt,
                "workspace": str(workspace),
                "provider": provider_name,
                "model": args.model if not args.demo else "demo",
            },
        )

    tools = default_tools()
    if args.sandbox_shell:
        sandbox = BwrapShell(workspace)
        try:
            sandbox.ensure_available()
        except SandboxUnavailableError as exc:
            print(f"warning: sandbox_shell disabled: {exc}", file=sys.stderr)
        else:
            tools.register(SandboxedShellTool(sandbox))
    if skills.list():
        tools.register(LoadSkillTool(skills))
    tools.register(
        build_agent_tool(
            provider=provider,
            tools=tools,
            workspace=workspace,
            parent_session=session_log,
        )
    )
    mcp_manager = McpManager.from_file(args.mcp_config) if args.mcp_config else None
    permission_engine = PermissionEngine(_permission_context(args))
    hooks = load_hook_registry(args.hooks_config) if args.hooks_config else None
    if args.auto_review:
        approval = AgentApprovalHandler(_build_reviewer(args, provider))
    else:
        approval = HumanApprovalHandler(_approval_callback(args))
    loop = AgentLoop(
        provider=provider,
        tools=tools,
        workspace=workspace,
        system_prompt="\n\n".join(system_parts),
        max_steps=args.max_steps,
        permission_engine=permission_engine,
        approval_handler=approval,
        tracer=tracer,
        compactor=ContextCompactor(
            threshold_tokens=args.context_threshold,
            keep_recent_units=args.keep_recent,
        ),
        artifact_store=ArtifactStore(
            workspace / ".mini-oh" / "artifacts",
            max_inline_chars=args.max_inline_output,
        ),
        input_cost_per_million=args.input_cost,
        output_cost_per_million=args.output_cost,
        tool_timeout_seconds=args.tool_timeout,
        max_repeated_tool_batches=args.max_repeated_tool_batches,
        max_concurrent_tools=args.max_concurrent_tools,
        hooks=hooks,
        messages=messages,
        session=session_log,
    )
    return loop, tracer, mcp_manager, provider


async def _close_runtime(
    mcp_manager: McpManager | None,
    provider: Any,
) -> None:
    if mcp_manager:
        await mcp_manager.close()
    close = getattr(provider, "close", None)
    if close is not None:
        await close()


def _sessions_command(args: argparse.Namespace) -> int:
    store = SessionStore(Path(args.session_dir or Path.cwd() / ".mini-oh" / "sessions"))
    summaries = store.list()
    if args.json:
        print(
            json.dumps(
                [asdict(item) | {"path": str(item.path)} for item in summaries], indent=2
            )
        )
    else:
        for item in summaries:
            print(
                f"{item.session_id:26} {item.created_at} {item.message_count:4} msgs  "
                f"{item.first_prompt[:60]}"
            )
    return 0


def _print_provider_hint(args: argparse.Namespace, event: AgentEvent) -> None:
    if args.api_mode == "responses" and "HTTP 404" in event.message:
        print(
            "hint: this endpoint may only support Chat Completions; retry with "
            "--api-mode chat or set OPENAI_API_MODE=chat",
            file=sys.stderr,
        )


def _permission_context(args: argparse.Namespace) -> PermissionContext:
    rules = build_default_rules()
    if args.permission_config:
        rules = load_rules_from_json(args.permission_config)
    mode = (
        PermissionMode.AUTO_REVIEW if args.auto_review else PermissionMode.DEFAULT
    )
    return PermissionContext(
        mode=mode,
        rules=rules,
        workspace=Path(args.workspace).resolve(),
    )


def _approval_callback(args: argparse.Namespace):
    if not sys.stdin.isatty():
        return None

    async def ask(request, decision) -> bool:
        answer = await asyncio.to_thread(
            input, f"Approve {request.tool_name}? {decision.reason} [y/N] "
        )
        return answer.strip().lower() in {"y", "yes"}

    return ask


def _build_reviewer(args: argparse.Namespace, provider):
    """Independent reviewer: one tool-less model call answers approve/reject."""
    workspace = Path(args.workspace).resolve()

    async def review(request, decision) -> bool:
        prompt = (
            "You are an independent permission reviewer. Decide whether to approve "
            "the following request. Reply with exactly one word: approve or reject.\n"
            f"tool: {request.tool_name}\n"
            f"source: {request.source}\n"
            f"effect: {request.effect}\n"
            f"destructive: {request.destructive}\n"
            f"path: {request.path}\n"
            f"command: {request.command}\n"
            f"workspace: {workspace}\n"
            f"input: {request.input}\n"
            f"reason: {decision.reason}\n"
        )
        try:
            reply = await provider.complete([Message("system", prompt)], [])
        except Exception:
            return False
        text = (reply.content or "").strip().lower()
        parsed = "approve" if text.startswith("approve") else "reject"
        target = request.path or request.command or ""
        detail = f" {target}" if target else ""
        print(f"⚖ reviewer: {parsed} — {request.tool_name}{detail} ({decision.reason})")
        return parsed == "approve"

    return review


def _trace_command(args: argparse.Namespace) -> int:
    store = TraceStore(args.trace_dir)
    if args.action == "list":
        summaries = store.list()
        if args.json:
            print(
                json.dumps(
                    [asdict(item) | {"path": str(item.path)} for item in summaries], indent=2
                )
            )
        else:
            for item in summaries:
                print(
                    f"{item.run_id:26} {item.status:10} {item.elapsed_ms:7}ms "
                    f"{item.event_count:4} events  {item.prompt[:60]}"
                )
        return 0
    if args.action == "prune":
        if args.older_than is None and args.max_runs is None:
            raise SystemExit("trace prune requires --older-than or --max-runs")
        candidates = store.prune(
            older_than_days=args.older_than,
            max_runs=args.max_runs,
            dry_run=not args.apply,
        )
        action = "deleted" if args.apply else "would delete"
        if not candidates:
            print(f"{action}: (none)")
        else:
            for path in candidates:
                print(f"{action}: {path}")
        return 0
    if not args.run_id:
        raise SystemExit(f"trace {args.action} requires <run-id>")
    if args.action == "show":
        events = [asdict(event) for event in store.read(args.run_id)]
        print(json.dumps(events, ensure_ascii=False, indent=2))
    else:
        print("safe replay: recorded events only; providers and tools are not executed")
        for line in store.replay(args.run_id):
            print(line)
    return 0


def _print_event(event: AgentEvent) -> None:
    if event.kind == "assistant_delta":
        print(event.message, end="", flush=True)
    elif event.kind == "assistant":
        if event.data.get("streamed"):
            print()
        else:
            print(event.message)
    elif event.kind == "provider_retry":
        print(
            f"retrying provider in {event.data['delay_seconds']:.1f}s: {event.message}",
            file=sys.stderr,
        )
    elif event.kind == "tool_start":
        print(f"→ {event.data['name']} {event.data['input']}")
    elif event.kind == "tool_end":
        marker = "✗" if event.data["is_error"] else "✓"
        print(
            f"{marker} {event.data['name']} ({event.data['elapsed_ms']}ms): "
            f"{_tool_end_summary(event)}"
        )
    elif event.kind == "compact":
        print(
            f"compacted context: {event.data['before_tokens']} → "
            f"{event.data['after_tokens']} estimated tokens"
        )
    elif event.kind == "loop_guard":
        print(f"loop guard: {event.message}", file=sys.stderr)
    elif event.kind == "hook_blocked":
        print(
            f"hook blocked {event.data.get('event', 'event')}: {event.message}",
            file=sys.stderr,
        )
    elif event.kind in {"error", "cancelled"}:
        print(f"{event.kind}: {event.message}", file=sys.stderr)
    elif event.kind == "done":
        print(f"done in {event.data['steps']} model step(s)")


def _tool_end_summary(event: AgentEvent) -> str:
    """Build a compact tool_end line; file tools never print their content."""
    name = event.data["name"]
    tool_input = event.data.get("input", {})
    if event.data["is_error"]:
        return event.message.replace("\n", " ")[:120] or "(no output)"
    if name in {"read_file", "write_file", "edit_file"}:
        path = tool_input.get("path")
        return path if isinstance(path, str) else "(no path)"
    if name == "list_files":
        count = sum(
            1
            for line in event.message.splitlines()
            if line and not line.startswith("(empty")
        )
        return f"{tool_input.get('path', '.')} ({count} files)"
    if name == "load_skill":
        skill = tool_input.get("name")
        return f"skill {skill}" if isinstance(skill, str) else "skill"
    preview = event.message.replace("\n", " ")[:80]
    return preview or "(no output)"


def main(argv: list[str] | None = None) -> int:
    _load_environment()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):
        pass
    try:
        command = arguments[0] if arguments else None
        if command == "trace":
            return _trace_command(build_trace_parser().parse_args(arguments[1:]))
        if command == "sessions":
            return _sessions_command(build_sessions_parser().parse_args(arguments[1:]))
        if command == "resume":
            return asyncio.run(_resume_command(build_resume_parser().parse_args(arguments[1:])))
        if command == "continue":
            return asyncio.run(
                _resume_command(build_resume_parser().parse_args(arguments[1:]))
            )
        args = build_run_parser().parse_args(arguments)
        if args.prompt:
            return asyncio.run(_run(args))
        return asyncio.run(_interactive(args))
    except KeyboardInterrupt:
        _print_resume_hint()
        print("cancelled", file=sys.stderr)
        return 130


def _handle_sigterm(signum: int, frame: object) -> None:
    del signum, frame
    _print_resume_hint()
    sys.exit(143)


def _load_environment() -> None:
    """Load a local .env without overriding explicit shell variables."""
    load_dotenv(Path.cwd() / ".env", override=False)


if __name__ == "__main__":
    raise SystemExit(main())
