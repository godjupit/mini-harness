"""CLI for agent runs and trace inspection/replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from mini_openharness.compaction import ArtifactStore, ContextCompactor
from mini_openharness.engine import AgentEvent, AgentLoop, MaxStepsExceeded
from mini_openharness.hooks import load_hook_registry
from mini_openharness.mcp import McpManager
from mini_openharness.permissions import PermissionPolicy
from mini_openharness.provider import (
    DemoProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
)
from mini_openharness.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    SandboxedShellTool,
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


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-oh", description="A tiny coding-agent harness")
    parser.add_argument("prompt", nargs="?", help="Task for the coding agent")
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
    parser.add_argument("--allow-write", action="store_true", help="Allow mutating tools")
    parser.add_argument("--yes", action="store_true", help="Approve every ask decision")
    parser.add_argument("--permission-config", help="JSON allow/deny/ask rules")
    parser.add_argument("--hooks-config", help="JSON lifecycle command hooks")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--tool-timeout", type=float, default=30.0)
    parser.add_argument("--max-repeated-tool-batches", type=int, default=3)
    parser.add_argument("--max-concurrent-tools", type=_positive_int, default=8)
    parser.add_argument(
        "--sandbox-shell",
        action="store_true",
        help="Enable Docker-only sandbox_shell; never falls back to the host",
    )
    parser.add_argument("--sandbox-image", default="alpine:3.20")
    parser.add_argument("--sandbox-memory", default="512m")
    parser.add_argument("--sandbox-cpus", type=float, default=1.0)
    parser.add_argument("--sandbox-pids", type=int, default=128)
    parser.add_argument("--context-threshold", type=int, default=12_000)
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
    return parser


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


async def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    prompt = args.prompt or "Inspect this project and summarize its architecture."
    skills = SkillCatalog(args.skills_dir or workspace / "skills")
    system_parts = ["You are a concise coding assistant. Inspect before editing."]
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
                "prompt": prompt,
                "workspace": str(workspace),
                "provider": provider_name,
                "model": args.model if not args.demo else "demo",
            },
        )

    tools = default_tools()
    if args.sandbox_shell:
        sandbox = DockerSandbox(
            DockerSandboxConfig(
                image=args.sandbox_image,
                memory=args.sandbox_memory,
                cpus=args.sandbox_cpus,
                pids_limit=args.sandbox_pids,
            )
        )
        await sandbox.ensure_available()
        tools.register(SandboxedShellTool(sandbox))
    if skills.list():
        tools.register(LoadSkillTool(skills))
    mcp_manager = McpManager.from_file(args.mcp_config) if args.mcp_config else None
    policy = _permission_policy(args)
    hooks = load_hook_registry(args.hooks_config) if args.hooks_config else None
    approval = _approval_callback(args)
    loop = None
    exit_code = 1
    try:
        if mcp_manager:
            registered = await mcp_manager.connect_and_register(tools)
            print(f"connected MCP tools: {', '.join(registered) or '(none)'}")
        loop = AgentLoop(
            provider=provider,
            tools=tools,
            workspace=workspace,
            system_prompt="\n\n".join(system_parts),
            max_steps=args.max_steps,
            allow_write=args.allow_write,
            permission_policy=policy,
            approval_callback=approval,
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
        )
        async for event in loop.run(prompt):
            _print_event(event)
            if event.kind == "done":
                exit_code = 0
            elif event.kind == "cancelled":
                exit_code = 130
            elif event.kind == "error":
                _print_provider_hint(args, event)
                exit_code = 1
    except MaxStepsExceeded as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        if loop:
            loop.cancel()
        return 130
    except asyncio.CancelledError:
        if loop:
            loop.cancel()
        if tracer:
            tracer.finish(status="cancelled", data={"reason": "CLI task cancelled"})
        raise
    finally:
        if mcp_manager:
            await mcp_manager.close()
        close = getattr(provider, "close", None)
        if close is not None:
            await close()
    if tracer:
        print(f"trace: {tracer.path}")
    return exit_code


def _print_provider_hint(args: argparse.Namespace, event: AgentEvent) -> None:
    if args.api_mode == "responses" and "HTTP 404" in event.message:
        print(
            "hint: this endpoint may only support Chat Completions; retry with "
            "--api-mode chat or set OPENAI_API_MODE=chat",
            file=sys.stderr,
        )


def _permission_policy(args: argparse.Namespace) -> PermissionPolicy:
    if args.permission_config:
        configured = PermissionPolicy.from_file(args.permission_config)
        if args.allow_write:
            return PermissionPolicy(configured.rules, default_mutation="allow")
        return configured
    return PermissionPolicy(default_mutation="allow" if args.allow_write else "ask")


def _approval_callback(args: argparse.Namespace):
    if args.yes:

        async def approve_all(tool: str, reason: str) -> bool:
            del tool, reason
            return True

        return approve_all
    if not sys.stdin.isatty():
        return None

    async def ask(tool: str, reason: str) -> bool:
        answer = await asyncio.to_thread(input, f"Approve {tool}? {reason} [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    return ask


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
        preview = event.message.replace("\n", " ")[:100]
        print(f"{marker} {event.data['name']} ({event.data['elapsed_ms']}ms): {preview}")
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
        print(
            f"done in {event.data['steps']} model step(s), "
            f"tokens={event.data['input_tokens'] + event.data['output_tokens']}, "
            f"estimated_cost=${event.data['estimated_cost']:.6f}"
        )


def main(argv: list[str] | None = None) -> int:
    _load_environment()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "trace":
            return _trace_command(build_trace_parser().parse_args(arguments[1:]))
        return asyncio.run(_run(build_run_parser().parse_args(arguments)))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


def _load_environment() -> None:
    """Load a local .env without overriding explicit shell variables."""
    load_dotenv(Path.cwd() / ".env", override=False)


if __name__ == "__main__":
    raise SystemExit(main())
