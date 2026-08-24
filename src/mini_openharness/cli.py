"""CLI for agent runs and trace inspection/replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mini_openharness.agent_profile import AgentProfile, PermissionPolicy
from mini_openharness.compaction import (
    DEFAULT_KEEP_RECENT_TOKENS,
    ArtifactStore,
    ContextCompactor,
)
from mini_openharness.engine import AgentEvent, AgentLoop
from mini_openharness.errors.engine import MaxStepsExceeded
from mini_openharness.errors.sandbox import SandboxUnavailableError
from mini_openharness.hooks import load_hook_registry
from mini_openharness.mcp.mcp import McpManager
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
from mini_openharness.runtime import AgentRuntimeBuilder
from mini_openharness.sandbox import (
    BwrapShell,
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
from mini_openharness.tools import MemoryReadTool, MemoryWriteTool, default_tools
from mini_openharness.trace import TraceStore, TraceWriter
from mini_openharness.utils.tokens import build_token_counter


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
_THINKING_LINE_OPEN = False
_REASONING_LINE_SHOWN = False


def _print_resume_hint() -> None:
    if _ACTIVE_SESSION is not None:
        print(
            f"Resume this session with: wqb resume {_ACTIVE_SESSION.session_id}",
            file=sys.stderr,
        )


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wqb", description="A tiny coding-agent harness")
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
    parser.add_argument(
        "--context-window",
        type=int,
        default=None,
        help=(
            "Model context window in tokens. Compaction starts when the "
            "estimated context reaches 70%% of it; when omitted, "
            "--context-threshold is used directly."
        ),
    )
    parser.add_argument(
        "--keep-recent",
        type=int,
        default=1,
        help="Minimum complete tool turns kept verbatim after compaction (floor)",
    )
    parser.add_argument(
        "--keep-recent-tokens",
        type=int,
        default=DEFAULT_KEEP_RECENT_TOKENS,
        help=(
            "Approximate token budget for the recent messages kept verbatim "
            "after compaction (default 12000)"
        ),
    )
    parser.add_argument("--max-inline-output", type=int, default=8_000)
    parser.add_argument("--artifact-dir", help="Directory for offloaded tool outputs")
    parser.add_argument("--input-cost", type=float, default=0.0, help="USD per million tokens")
    parser.add_argument("--output-cost", type=float, default=0.0, help="USD per million tokens")
    parser.add_argument("--skills-dir", help="Directory containing <name>/SKILL.md skills")
    parser.add_argument("--memory-dir", help="Directory containing this Agent App's memory")
    parser.add_argument("--mcp-config", help="JSON file containing stdio/HTTP mcpServers")
    parser.add_argument(
        "--system-prompt-file",
        help="Optional UTF-8 file appended to the built-in system prompt",
    )
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
    parser = argparse.ArgumentParser(prog="wqb trace", description="Inspect run traces")
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
    parser = argparse.ArgumentParser(prog="wqb sessions", description="List conversation sessions")
    parser.add_argument("--session-dir", help="JSONL conversation session directory")
    parser.add_argument("--json", action="store_true")
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqb resume", description="Resume an interrupted conversation session"
    )
    parser.add_argument("session_id", nargs="?", help="Session id; defaults to the most recent")
    parser.add_argument("--latest", action="store_true", help="Resume the most recent session")
    _add_agent_arguments(parser)
    return parser


def _session_dir(args: argparse.Namespace) -> Path:
    workspace = Path(args.workspace).resolve()
    return Path(args.session_dir or workspace / ".mini-oh" / "sessions").resolve()


def _memory_prompt(memory_dir: str | Path) -> str:
    """Return the memory index for the system prompt, or '' when absent."""
    memory_file = Path(memory_dir) / "MEMORY.md"
    try:
        if not memory_file.is_file():
            return ""
        return (
            "MEMORY INDEX\n\n"
            + memory_file.read_text(encoding="utf-8").strip()
            + "\n\nLoad a topic file with memory_read('<file>') only when the "
            "current question actually needs it."
        )
    except OSError:
        return ""


async def _run(args: argparse.Namespace, profile: AgentProfile | None = None) -> int:
    workspace = Path(args.workspace).resolve()
    prompt = args.prompt
    session_log = None
    session_init_ms = 0.0
    if not args.no_session:
        session_started = time.perf_counter()
        session_log = SessionLog(
            _session_dir(args),
            metadata={
                "first_prompt": prompt,
                "workspace": str(workspace),
                "agent_profile": profile.name if profile is not None else "coding-default",
            },
        )
        session_init_ms = (time.perf_counter() - session_started) * 1000
        global _ACTIVE_SESSION
        _ACTIVE_SESSION = session_log
    return await _drive_session(
        args,
        session_log=session_log,
        messages=None,
        trace_prompt=prompt,
        run_events=lambda loop: loop.run(prompt),
        session_init_ms=session_init_ms,
        profile=profile,
    )


async def _interactive(args: argparse.Namespace, profile: AgentProfile | None = None) -> int:
    """Continuous REPL: one runtime / AgentLoop / Session for many prompts."""
    workspace = Path(args.workspace).resolve()
    session_log = None
    session_init_ms = 0.0
    if not args.no_session:
        session_started = time.perf_counter()
        session_log = SessionLog(
            _session_dir(args),
            metadata={
                "first_prompt": "(interactive)",
                "interactive": True,
                "workspace": str(workspace),
                "agent_profile": profile.name if profile is not None else "coding-default",
            },
        )
        session_init_ms = (time.perf_counter() - session_started) * 1000
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
            profile=profile,
        )
        if tracer is not None:
            tracer.emit(
                "runtime_init",
                {"phase": "session_init", "elapsed_ms": round(session_init_ms, 1)},
            )
        if mcp_manager:
            mcp_started = time.perf_counter()
            registered = await mcp_manager.connect_and_register(loop.tools)
            if tracer is not None:
                tracer.emit(
                    "runtime_init",
                    {
                        "phase": "mcp_connect",
                        "elapsed_ms": round((time.perf_counter() - mcp_started) * 1000, 1),
                    },
                )
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
            stack.callback(os.write, out_fd, b"\x1b[?2004l")
            tty.setcbreak(in_fd)
            os.write(out_fd, b"\x1b[?2004h")  # 开启 bracketed paste
            os.write(out_fd, prompt.encode("utf-8"))
            in_paste = False
            while True:
                chunk = os.read(in_fd, 1)
                if not chunk:
                    raise EOFError
                byte = chunk[0]
                if byte == 27:  # escape sequences: arrows ignored, paste markers tracked
                    head = os.read(in_fd, 1)
                    if head == b"[":
                        middle = b""
                        while True:
                            tail = os.read(in_fd, 1)
                            if not tail:
                                break
                            if 64 <= tail[0] < 127:
                                break
                            middle += tail
                        if middle == b"200":
                            in_paste = True
                        elif middle == b"201":
                            in_paste = False
                    continue
                if in_paste:
                    buffer.append(byte)
                    os.write(out_fd, bytes([byte]))
                    continue
                if byte in (13, 10):
                    os.write(out_fd, b"\r\n")
                    break
                if byte in (127, 8):  # DEL / backspace: erase one full character
                    if buffer:
                        text = buffer.decode("utf-8", errors="ignore")
                        up = _visual_lines(text) - 1
                        text = text[:-1]
                        buffer = bytearray(text.encode("utf-8"))
                        _redraw(out_fd, prompt, text, up_lines=up)
                    continue
                if byte == 4 and not buffer:  # Ctrl+D on an empty line = EOF
                    raise EOFError
                buffer.append(byte)
                os.write(out_fd, bytes([byte]))
    except KeyboardInterrupt:
        os.write(out_fd, b"\r\n")
        raise
    return buffer.decode("utf-8", errors="replace").strip()


def _visual_lines(text: str) -> int:
    width = shutil.get_terminal_size((80, 24)).columns
    return sum(max(1, (len(line) + width - 1) // width) for line in (text or "").split("\n"))


def _redraw(out_fd: int, prompt: str, text: str, up_lines: int = 0) -> None:
    """Return the cursor to the start of the input and redraw it.

    A plain ``\\r`` only reaches the start of the current *visual* line, so
    long or pasted multi-line input would pile lines downward on every
    backspace. Move up by the number of visual lines first, then clear the
    rest of the screen and redraw.
    """
    if up_lines > 0:
        os.write(out_fd, f"\x1b[{up_lines}A".encode("utf-8"))
    os.write(out_fd, f"\r\x1b[J{prompt}{text}".encode("utf-8"))


async def _resume_command(args: argparse.Namespace, profile: AgentProfile | None = None) -> int:
    store = SessionStore(_session_dir(args))
    latest = store.latest()
    session_id = args.session_id or (latest.session_id if latest else None)
    if not session_id:
        print("no sessions found; run `wqb` with a prompt first", file=sys.stderr)
        return 1
    session_started = time.perf_counter()
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
    session_init_ms = (time.perf_counter() - session_started) * 1000
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
        session_init_ms=session_init_ms,
        profile=profile,
    )


async def _drive_session(
    args: argparse.Namespace,
    *,
    session_log: SessionLog | None,
    messages: list[Message] | None,
    trace_prompt: str,
    run_events: Any,
    session_init_ms: float | None = None,
    profile: AgentProfile | None = None,
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
            profile=profile,
        )
        if tracer is not None and session_init_ms is not None:
            tracer.emit(
                "runtime_init",
                {"phase": "session_init", "elapsed_ms": round(session_init_ms, 1)},
            )
        if mcp_manager:
            mcp_started = time.perf_counter()
            registered = await mcp_manager.connect_and_register(loop.tools)
            if tracer is not None:
                tracer.emit(
                    "runtime_init",
                    {
                        "phase": "mcp_connect",
                        "elapsed_ms": round((time.perf_counter() - mcp_started) * 1000, 1),
                    },
                )
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
    profile: AgentProfile | None = None,
) -> tuple[AgentLoop, TraceWriter | None, McpManager | None, Any]:
    runtime = await AgentRuntimeBuilder(profile).build(
        args,
        session_log=session_log,
        messages=messages,
        trace_prompt=trace_prompt,
        assembler=_assemble_runtime,
    )
    return runtime.loop, runtime.tracer, runtime.mcp_manager, runtime.provider


async def _assemble_runtime(
    args: argparse.Namespace,
    *,
    session_log: SessionLog | None,
    messages: list[Message] | None = None,
    trace_prompt: str,
    profile: AgentProfile | None = None,
) -> tuple[AgentLoop, TraceWriter | None, McpManager | None, Any]:
    workspace = Path(args.workspace).resolve()
    trace_dir = Path(args.trace_dir or workspace / ".mini-oh" / "traces")
    provider_name = "demo" if args.demo else f"openai-{args.api_mode}"
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
                "agent_profile": profile.name if profile is not None else "coding-default",
            },
        )

    phase_started = time.perf_counter()

    def phase(name: str) -> None:
        nonlocal phase_started
        elapsed_ms = (time.perf_counter() - phase_started) * 1000
        if tracer is not None:
            tracer.emit(
                "runtime_init",
                {"phase": name, "elapsed_ms": round(elapsed_ms, 1)},
            )
        phase_started = time.perf_counter()

    profile_skills_dir = profile.skills_dir if profile is not None else None
    skills_dir = args.skills_dir or profile_skills_dir or workspace / "skills"
    skills = SkillCatalog(skills_dir)
    profile_memory_dir = profile.memory_dir if profile is not None else None
    memory_dir = Path(args.memory_dir or profile_memory_dir or workspace / "memdir").resolve()
    system_parts = [
        (
            """You are Mini Harness, a concise coding agent operating in a workspace.

TASK DISCIPLINE

* The latest user request is the authoritative goal. Do not expand scope, add unrelated features, or investigate adjacent topics unless they are required to complete the request.
* Once the request can be answered correctly with the evidence already available, STOP TOOL CALLING and RETURN FINAL.
* For implementation tasks, stop once the requested change is complete and the minimum necessary verification has passed.
* Do not continue investigating merely to increase confidence, completeness, or coverage.

WORKFLOW

* Inspect before editing. Read only the files needed to understand the relevant code before making changes.
* Prefer the shortest path from the user's request to the relevant implementation.
* When the target file, module, symbol, or directory is already known, start there. Do not survey the repository first.
* After modifying code, run the minimum verification necessary to establish that the requested change works.
* If verification is impossible in the current environment, state that explicitly instead of claiming success.
* Use workspace-relative paths such as `src/app.py`, not absolute paths.

FILE EDITING

* Use write_file primarily for creating new files.
* When modifying an existing file, prefer edit_file.
* Read only the relevant section before editing.
* old_text must match the existing file exactly and uniquely.
* Include enough surrounding context to make the match unique.
* Never rewrite an entire large file when a localized edit is sufficient.

EXPLORATION

* Prefer targeted search and targeted reads over broad repository exploration.
* Locate code with `grep` or `find_files` before reading: search first, then read only the matching ranges.
* Do not inspect files, tests, documentation, skills, repository history, sibling directories, or runtime artifacts merely for completeness, confirmation, or curiosity.
* Before making an additional exploratory tool call, there must be a specific unresolved question that the call is expected to answer.
* If existing evidence already answers that question, do not make the tool call.
* New files are not automatically new information. Continue exploration only when the expected result could materially change the answer, implementation, or next action.
* Do not re-read a file or equivalent stored copy unless the file may have changed or a specific missing detail is required.
* Do not repeat an investigation already completed by a subagent.
* If several relevant files have already established the cause or solution, synthesize the result instead of searching for additional confirmation.
* When sufficient evidence exists, STOP EXPLORING immediately.

EFFICIENCY

* Minimize tool calls, model turns, context growth, and repeated observations.
* Prefer one targeted search over several directory listings.
* Prefer reading the smallest relevant portion of a file when the tool supports ranges, offsets, or limits.
* Avoid loading large files when a search can first identify the relevant symbol or region.
* Parallelize independent reads or searches when doing so reduces turns without increasing unnecessary scope.
* After a command fails, diagnose the exact failure and make the smallest corrective action. Do not respond to a straightforward failure with broad exploration.
* Do not repeat equivalent tests after they already pass.
* Do not perform speculative checks that are unrelated to a concrete unresolved risk.

TOOL USE

* `read_file`, `list_dir`, `find_files`, `grep`, `write_file`, and `edit_file` are workspace file tools.
* `list_dir` shows one directory level. Use it when directory contents are genuinely unknown; do not repeatedly list directories already inspected.
* `find_files` searches recursively by name. Prefer it when you know what kind of file or symbol location you are looking for.
* `grep` searches file contents with a regular expression and returns `file:line: text` matches. Prefer it over reading whole files when locating a symbol, usage, or definition.
* `read_file` reads a file in explicit line pages. Use `offset` (0-based start line) and `limit` (maximum lines) to read only the relevant range; the result reports the returned range, total lines, whether more content exists, and the next offset. Do not re-request the same unchanged range; use the reported next offset to continue paging.
* `edit_file` applies a localized replacement to an existing file. Read the relevant section first, then provide old_text/new_text where old_text matches exactly once; the runtime rejects edits based on stale content or non-unique matches. Never rewrite a whole large file when edit_file suffices.
* `memory_write` saves long-term memory into the workspace `memdir/` folder. Call it immediately when the user explicitly asks you to remember something, or states a durable fact about their role, goals, or preferences; feedback on your working style; project background not derivable from code; or where external information lives. `type` must be one of `user`, `feedback`, `project`, or `reference`; `topic` is a short slug such as `role`, `testing`, or `release`. The runtime writes the memory to `memdir/{type}_{topic}.md` and keeps `memdir/MEMORY.md` as the index (one line per topic file). Do not save ephemeral task details.
* `memory_read` loads one topic memory file from `memdir/` (e.g. `permissions.md`, `provider.md`) on demand. Only the `memdir/MEMORY.md` index is injected at session start; when a question touches a topic listed there, call `memory_read` for that file before answering. Do not read every memory file up front.
* `agent` delegates substantial, self-contained investigation to `explore_agent` or implementation planning to `plan_agent`. Do not delegate trivial searches that can be completed directly in one or two targeted tool calls.
* After delegating an investigation, use the returned findings. Do not repeat the same searches in the main agent unless the result contains a specific unresolved gap.
* `sandbox_shell` runs host shell commands in a bubblewrap sandbox. Host Python, pytest, git, and `.venv` are available; the workspace is writable, the rest of the filesystem is read-only, `/tmp` is fresh, and the working directory persists across calls.
* Install and execute in one shell command when installation is genuinely necessary.
* `load_skill` loads optional specialized instructions. Load a skill only when it directly helps the current request and is expected to reduce work or provide required procedure.
* Do not inspect the skills directory or load a skill merely because its description loosely resembles the task.
* `mcp__*` tools are exposed by configured MCP servers.

SUBAGENTS

* Use a subagent only for a substantial, well-defined task that benefits from isolated investigation.
* Give the subagent a specific question or deliverable, not a vague request to explore the repository.
* Prefer direct tools for simple, directed searches.
* Treat a subagent's final findings as evidence already collected.
* Do not duplicate the subagent's investigation in the main agent.
* If the subagent returns enough evidence to proceed, proceed immediately.

VERIFICATION

* Run only verification relevant to the requested change.
* Prefer the narrowest applicable test first.
* Expand verification only when the change has broader effects or the narrow test exposes uncertainty.
* Once appropriate verification passes, do not run equivalent checks merely for reassurance.
* Never claim a command, test, or validation succeeded unless it actually ran successfully.

INTERNAL RUNTIME

* `.mini-oh` sessions, traces, and artifacts are runtime implementation details, not normal sources of repository evidence.
* Never read a `.mini-oh` artifact merely to recover content already returned by another tool.
* Treat artifact references as storage metadata, not as new evidence.
* Do not follow an artifact path simply because a tool result was truncated or offloaded.
* If a specific missing source detail is required, inspect the original workspace source directly and as narrowly as possible.
* Inspect `.mini-oh` sessions, traces, or artifacts only when the user's request specifically requires analysis of runtime sessions, traces, or artifact behavior.
* Debugging Mini Harness source code alone does not imply permission or need to inspect runtime artifacts.

SAFETY

* Respect permission decisions: ALLOW, ASK, and DENY.
* Never attempt to bypass a denied operation.
* ASK decisions are reviewed automatically; do not circumvent the review process.
* Avoid destructive commands and writes to sensitive files such as `.env`, `.git`, publishing configuration, or CI configuration unless the user explicitly requests them.
* Prefer reversible, minimal changes.

RESPONSE DISCIPLINE

* Keep intermediate status concise and useful.
* Do not narrate routine internal reasoning or every obvious next step.
* Do not produce repeated "let me check", "let me confirm", or similar progress commentary when the next action is already clear.
* Final answers should directly address the user's request and include concrete file, symbol, test, or behavior references when relevant.
* When the task is complete, return the final result instead of performing additional tool calls."""
        ),
    ]
    if profile is not None:
        if profile.prompt_mode == "replace":
            system_parts.clear()
        system_parts.append(profile.system_prompt.strip())
        system_parts.append(profile.output_protocol.prompt_fragment())
    skills_enabled = profile is None or profile.enable_skills
    skill_prompt = skills.prompt() if skills_enabled else ""
    if skill_prompt:
        system_parts.append(skill_prompt)
    phase("skills")
    if args.system_prompt_file:
        prompt_path = Path(args.system_prompt_file).resolve()
        try:
            custom_prompt = prompt_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"cannot read system prompt file {prompt_path}: {exc}") from exc
        if not custom_prompt:
            raise SystemExit(f"system prompt file is empty: {prompt_path}")
        system_parts.append(custom_prompt)
    phase("custom_system_prompt")
    memory_enabled = profile is None or profile.enable_memory_prompt
    memory_prompt = _memory_prompt(memory_dir) if memory_enabled else ""
    if memory_prompt:
        system_parts.append(memory_prompt)
    phase("memory")

    token_counter = build_token_counter(args.model)
    if args.demo:
        provider = DemoProvider()
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
            context_window_tokens=args.context_window,
            token_counter=token_counter,
        )
    phase("provider")
    context_window = args.context_window
    if context_window is None:
        context_window = getattr(provider, "context_window_tokens", None)

    tools = profile.build_tools() if profile is not None else default_tools()
    if memory_enabled:
        registered_tools = {name for name, _ in tools.items()}
        if "memory_write" not in registered_tools:
            tools.register(MemoryWriteTool())
        if "memory_read" not in registered_tools:
            tools.register(MemoryReadTool())
    sandbox_enabled = args.sandbox_shell and (profile is None or profile.enable_sandbox_shell)
    if sandbox_enabled:
        sandbox = BwrapShell(workspace)
        try:
            sandbox.ensure_available()
        except SandboxUnavailableError as exc:
            print(f"warning: sandbox_shell disabled: {exc}", file=sys.stderr)
        else:
            tools.register(SandboxedShellTool(sandbox))
    if skills_enabled and skills.list():
        tools.register(LoadSkillTool(skills))
    subagents_enabled = profile is None or profile.enable_subagents
    if subagents_enabled:
        tools.register(
            build_agent_tool(
                provider=provider,
                tools=tools,
                workspace=workspace,
                parent_session=session_log,
            )
        )
    phase("tools")
    mcp_config = args.mcp_config or (profile.mcp_config if profile is not None else None)
    mcp_manager = McpManager.from_file(mcp_config) if mcp_config else None
    phase("mcp_config")
    auto_review = _auto_review_enabled(args, profile)
    permission_engine = PermissionEngine(_permission_context(args, profile, auto_review))
    hooks = load_hook_registry(args.hooks_config) if args.hooks_config else None
    if auto_review:
        approval = AgentApprovalHandler(_build_reviewer(args, provider))
    else:
        approval = HumanApprovalHandler(_approval_callback(args))
    loop = AgentLoop(
        provider=provider,
        tools=tools,
        workspace=workspace,
        memory_dir=memory_dir,
        system_prompt="\n\n".join(system_parts),
        max_steps=profile.max_steps if profile and profile.max_steps else args.max_steps,
        permission_engine=permission_engine,
        approval_handler=approval,
        tracer=tracer,
        compactor=ContextCompactor(
            threshold_tokens=args.context_threshold,
            keep_recent_units=args.keep_recent,
            keep_recent_tokens=args.keep_recent_tokens,
            context_window_tokens=context_window,
            token_counter=token_counter,
        ),
        artifact_store=ArtifactStore(
            Path(args.artifact_dir or workspace / ".mini-oh" / "artifacts").resolve(),
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
    phase("loop")
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
        print(json.dumps([asdict(item) | {"path": str(item.path)} for item in summaries], indent=2))
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


def _auto_review_enabled(
    args: argparse.Namespace,
    profile: AgentProfile | None,
) -> bool:
    if profile is None or profile.permission_policy == PermissionPolicy.INHERIT:
        return bool(args.auto_review)
    return profile.permission_policy == PermissionPolicy.AUTO_REVIEW


def _permission_context(
    args: argparse.Namespace,
    profile: AgentProfile | None = None,
    auto_review: bool | None = None,
) -> PermissionContext:
    rules = build_default_rules()
    permission_config = args.permission_config or (
        profile.permission_config if profile is not None else None
    )
    if permission_config:
        rules = load_rules_from_json(permission_config)
    if auto_review is None:
        auto_review = _auto_review_enabled(args, profile)
    mode = PermissionMode.AUTO_REVIEW if auto_review else PermissionMode.DEFAULT
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
        if request.tool_name == "sandbox_shell":
            suffix = f"shell {target}"
        else:
            suffix = f"{request.tool_name} {target}"
        if parsed == "reject":
            suffix = f"{suffix} ({decision.reason})"
        suffix = suffix.replace("\n", " ")
        if len(suffix) > 80:
            suffix = suffix[:80] + "…"
        print(f"⚖ reviewer: {parsed} — {suffix}")
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
    global _THINKING_LINE_OPEN, _REASONING_LINE_SHOWN
    if event.kind == "model_start":
        _THINKING_LINE_OPEN = True
        _REASONING_LINE_SHOWN = False
        print("⏳ model thinking...", end="", flush=True)
    elif event.kind == "reasoning_delta":
        if not _REASONING_LINE_SHOWN:
            _REASONING_LINE_SHOWN = True
            print("\r⏳ model reasoning...", end="", flush=True)
    elif event.kind == "first_token":
        _THINKING_LINE_OPEN = False
        ttft = event.data.get("ttft_ms", 0)
        status = "model reasoning" if _REASONING_LINE_SHOWN else "model thinking"
        print(f"\r⏳ {status}... {ttft / 1000:.1f}s")
    elif event.kind == "tool_call_start":
        if _THINKING_LINE_OPEN:
            _THINKING_LINE_OPEN = False
            print()
        name = event.data.get("name")
        if name:
            print(f"→ {name}")
        else:
            print(f"→ tool_call[{event.data.get('index', '?')}]")
    elif event.kind == "model_response_end":
        if _THINKING_LINE_OPEN:
            _THINKING_LINE_OPEN = False
            print()
    elif event.kind == "assistant_delta":
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
        print(f"→ {_tool_start_summary(event)}")
    elif event.kind == "tool_end":
        marker = "✗" if event.data["is_error"] else "✓"
        display_name = "shell" if event.data["name"] == "sandbox_shell" else event.data["name"]
        print(f"{marker} {display_name} ({event.data['elapsed_ms']}ms): {_tool_end_summary(event)}")
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
    if name in {"list_dir", "find_files"}:
        count = sum(
            1
            for line in event.message.splitlines()
            if line and not line.startswith("(empty") and not line.startswith("(no files")
        )
        detail = tool_input.get("path", ".")
        if name == "find_files":
            detail = f"{tool_input.get('pattern')} in {detail}"
        return f"{detail} ({count} files)"
    if name == "load_skill":
        skill = tool_input.get("name")
        return f"skill {skill}" if isinstance(skill, str) else "skill"
    if name == "sandbox_shell":
        code = event.data.get("returncode")
        return f"exit {code}" if code is not None else "ran"
    preview = event.message.replace("\n", " ")[:80]
    return preview or "(no output)"


def _tool_start_summary(event: AgentEvent) -> str:
    """Compact tool_start line; file writes never print their content."""
    name = event.data["name"]
    tool_input = event.data.get("input", {})
    if name in {"write_file", "edit_file"}:
        path = tool_input.get("path")
        detail = path if isinstance(path, str) else "(no path)"
        if name == "write_file":
            content = tool_input.get("content")
            if isinstance(content, str):
                detail += f" ({len(content)} chars)"
        return f"{name} {detail}"
    if name == "sandbox_shell":
        command = tool_input.get("command")
        preview = (command if isinstance(command, str) else str(command)).replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:80] + "…"
        return f"shell {preview}"
    return f"{name} {tool_input}"


def main(
    argv: list[str] | None = None,
    *,
    profile: AgentProfile | None = None,
) -> int:
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
            return asyncio.run(
                _resume_command(build_resume_parser().parse_args(arguments[1:]), profile)
            )
        if command == "continue":
            return asyncio.run(
                _resume_command(build_resume_parser().parse_args(arguments[1:]), profile)
            )
        args = build_run_parser().parse_args(arguments)
        if args.prompt:
            return asyncio.run(_run(args, profile))
        return asyncio.run(_interactive(args, profile))
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
