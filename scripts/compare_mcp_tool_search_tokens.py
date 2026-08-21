#!/usr/bin/env python3
"""A/B benchmark for direct MCP exposure versus dynamic tool_search.

Example:

    ./.venv/bin/python scripts/compare_mcp_tool_search_tokens.py \
        --mcp-config /path/to/github-mcp.json \
        --repeat 3

The benchmark writes a JSON report and a plain-text console log. Both paths
can be changed with ``--report`` and ``--log``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


TASK = """
You MUST use GitHub MCP to complete ALL of the following steps for
the repository github/github-mcp-server:

1. Use a GitHub MCP branch-listing tool to verify that the `main` branch exists.
2. Use a GitHub MCP commit tool to resolve `main` to its exact current commit SHA.
3. Use a GitHub MCP file-content tool to read `README.md`,
   but IMPORTANT: use the exact commit SHA obtained in step 2 as `ref`,
   NOT the string `main`.

Return ONLY the README.md blob SHA as exactly 40 hexadecimal characters.
Do not include Markdown, labels, explanation, or any other text.

You MUST perform all three steps. Do not skip a step even if you think you
already know the repository structure.

If the required MCP tools are hidden and `tool_search` is visible, use
tool_search to discover the required capabilities. A suitable search query is:

"GitHub list branches get commit get file contents repository owner repo branch sha path ref"
"""
SYSTEM_PROMPT = """
Complete the task using MCP tools only, never prior knowledge.

The user's requested verification steps are mandatory and must be performed
in order.

When an MCP capability is hidden, use tool_search to discover it.

Values returned from one MCP tool may be required as arguments to later MCP
tools. Use the actual tool result rather than guessing or substituting a branch
name.

Never fabricate tool results.
"""
SHA_RE = re.compile(r"(?i)\b[0-9a-f]{40}\b")


class Tee:
    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def find_root() -> Path:
    for candidate in (Path.cwd(), Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
        candidate = candidate.resolve()
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "mini_openharness"
        ).is_dir():
            return candidate
    raise RuntimeError("Cannot find Mini OpenHarness project root")


def prepare_imports(root: Path) -> None:
    root_text = str(root)
    while root_text in sys.path:
        sys.path.remove(root_text)
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def build_provider():
    from mini_openharness.provider import OpenAICompatibleProvider, OpenAIResponsesProvider

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    validate_secret("OPENAI_API_KEY", api_key)
    mode = os.getenv("OPENAI_API_MODE", "responses").strip().lower()
    if mode not in {"responses", "chat"}:
        raise RuntimeError("OPENAI_API_MODE must be 'responses' or 'chat'")
    provider_cls = OpenAIResponsesProvider if mode == "responses" else OpenAICompatibleProvider
    return provider_cls(
        api_key=api_key,
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        timeout=120,
        max_retries=2,
    )


def validate_secret(name: str, value: str) -> None:
    """Reject copied placeholder text before spawning Docker or calling an API."""
    if value.startswith("你的 ") or value.lower() in {
        "your github token",
        "your api key",
        "your model api key",
    }:
        raise RuntimeError(f"{name} still contains placeholder text; replace it with the real secret")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError(f"{name} must contain the real ASCII token, not Chinese placeholder text") from exc


async def approve_all(_request, _decision) -> bool:
    return True


def compact_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


class ExposureMixin:
    def _record_exposure(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.exposure_snapshots.append(
            {
                "tool_count": len(schemas),
                "schema_chars": compact_chars(schemas),
                "tool_names": [schema["name"] for schema in schemas],
            }
        )
        return schemas


def make_loop_classes():
    from mini_openharness.engine import AgentLoop

    class DynamicLoop(ExposureMixin, AgentLoop):
        def __init__(self, *args, **kwargs):
            self.exposure_snapshots = []
            super().__init__(*args, **kwargs)

        def _visible_tool_schemas(self, state):
            return self._record_exposure(super()._visible_tool_schemas(state))

    class DirectLoop(ExposureMixin, AgentLoop):
        def __init__(self, *args, **kwargs):
            self.exposure_snapshots = []
            super().__init__(*args, **kwargs)

        def _visible_tool_schemas(self, state):
            del state
            names = {name for name, _tool in self.tools.items() if name != "tool_search"}
            return self._record_exposure(self.tools.schemas(names))

    return DynamicLoop, DirectLoop


@dataclass
class Metrics:
    mode: str
    model_calls: int
    input_tokens: list[int]
    output_tokens: list[int]
    total_input_tokens: int
    total_output_tokens: int
    visible_tools: list[int]
    schema_chars: list[int]
    tool_calls: list[str]
    errors: list[str]
    final_answer: str
    extracted_sha: str | None
    used_tool_search: bool
    used_mcp: bool
    completed: bool

    @property
    def total_schema_chars(self) -> int:
        return sum(self.schema_chars)


async def run_one(mode: str, loop_cls, provider, registry, workspace: Path) -> Metrics:
    from mini_openharness.permissions import HumanApprovalHandler

    loop = loop_cls(
        provider=provider,
        tools=registry,
        workspace=workspace,
        system_prompt=SYSTEM_PROMPT,
        max_steps=8,
        approval_handler=HumanApprovalHandler(approve_all),
        tool_timeout_seconds=60,
    )
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    tool_calls: list[str] = []
    errors: list[str] = []
    final_answer = ""
    completed = False

    try:
        async for event in loop.run(TASK):
            if event.kind == "model_response_end":
                input_tokens.append(int(event.data.get("input_tokens", 0) or 0))
                output_tokens.append(int(event.data.get("output_tokens", 0) or 0))
            elif event.kind == "tool_start" and event.data.get("name"):
                tool_calls.append(str(event.data["name"]))
            elif event.kind == "tool_end" and event.data.get("is_error"):
                errors.append(f"{event.data.get('name')}: {event.message}")
            elif event.kind == "assistant":
                final_answer = event.message.strip()
            elif event.kind == "error":
                errors.append(f"agent_error: {event.message}")
            elif event.kind == "done":
                completed = True
    except Exception as exc:
        errors.append(f"agent_exception: {type(exc).__name__}: {exc}")

    match = SHA_RE.search(final_answer)
    return Metrics(
        mode=mode,
        model_calls=len(input_tokens),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_input_tokens=loop.input_tokens,
        total_output_tokens=loop.output_tokens,
        visible_tools=[item["tool_count"] for item in loop.exposure_snapshots],
        schema_chars=[item["schema_chars"] for item in loop.exposure_snapshots],
        tool_calls=tool_calls,
        errors=errors,
        final_answer=final_answer,
        extracted_sha=match.group(0).lower() if match else None,
        used_tool_search="tool_search" in tool_calls,
        used_mcp=any(name.startswith("mcp__") for name in tool_calls),
        completed=completed,
    )


def print_metrics(metrics: Metrics) -> None:
    print("\n" + "=" * 88)
    print(metrics.mode)
    print("=" * 88)
    print(f"completed:           {metrics.completed}")
    print(f"used tool_search:    {metrics.used_tool_search}")
    print(f"used MCP:            {metrics.used_mcp}")
    print(f"model calls:         {metrics.model_calls}")
    print(f"input tokens / call: {metrics.input_tokens}")
    print(f"total input tokens:  {metrics.total_input_tokens}")
    print(f"visible tools / call:{metrics.visible_tools}")
    print(f"schema chars / call: {metrics.schema_chars}")
    print(f"total schema chars:  {metrics.total_schema_chars}")
    print(f"tool calls:          {metrics.tool_calls}")
    print(f"final answer:        {metrics.final_answer!r}")
    print(f"extracted SHA:       {metrics.extracted_sha}")
    for error in metrics.errors:
        print(f"error:              {error}")


def compare(direct: Metrics, dynamic: Metrics) -> dict[str, Any]:
    same_sha = bool(
        direct.extracted_sha
        and dynamic.extracted_sha
        and direct.extracted_sha == dynamic.extracted_sha
    )
    saved = direct.total_input_tokens - dynamic.total_input_tokens
    return {
        "same_functional_result": same_sha,
        "direct_sha": direct.extracted_sha,
        "dynamic_sha": dynamic.extracted_sha,
        "direct_input_tokens": direct.total_input_tokens,
        "dynamic_input_tokens": dynamic.total_input_tokens,
        "input_tokens_saved": saved,
        "input_tokens_saved_pct": (saved / direct.total_input_tokens * 100)
        if direct.total_input_tokens
        else 0.0,
        "direct_schema_chars": direct.total_schema_chars,
        "dynamic_schema_chars": dynamic.total_schema_chars,
        "schema_chars_saved": direct.total_schema_chars - dynamic.total_schema_chars,
        "dynamic_used_tool_search": dynamic.used_tool_search,
    }


async def benchmark(args: argparse.Namespace, root: Path) -> int:
    from mini_openharness.mcp.mcp import McpManager
    from mini_openharness.tools import default_tools

    config_path = Path(args.mcp_config).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"MCP config not found: {config_path}. "
            "Use --mcp-config PATH or keep examples/github-mcp.json in the project."
        )

    DynamicLoop, DirectLoop = make_loop_classes()
    github_token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN") or os.getenv("GITHUB_PAT")
    if not github_token:
        raise RuntimeError("Set GITHUB_PERSONAL_ACCESS_TOKEN or GITHUB_PAT before running")
    validate_secret("GITHUB_PERSONAL_ACCESS_TOKEN", github_token)
    os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = github_token
    manager = McpManager.from_file(config_path)
    provider = build_provider()
    dynamic_registry = default_tools()
    try:
        registered = await manager.connect_and_register(dynamic_registry)
        mcp_names = [name for name in registered if name.startswith("mcp__")]
        if not mcp_names:
            raise RuntimeError("No MCP tools were registered")

        direct_registry = default_tools()
        items = dict(dynamic_registry.items())
        for name in mcp_names:
            direct_registry.register(items[name])

        comparisons: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="mini-oh-mcp-ab-") as temp:
            workspace = Path(temp)
            for index in range(args.repeat):
                print(f"\n********** REPEAT {index + 1}/{args.repeat} **********")
                if index % 2 == 0:
                    direct = await run_one("A/direct_all_mcp", DirectLoop, provider, direct_registry, workspace)
                    dynamic = await run_one("B/dynamic_tool_search", DynamicLoop, provider, dynamic_registry, workspace)
                else:
                    dynamic = await run_one("B/dynamic_tool_search", DynamicLoop, provider, dynamic_registry, workspace)
                    direct = await run_one("A/direct_all_mcp", DirectLoop, provider, direct_registry, workspace)
                print_metrics(direct)
                print_metrics(dynamic)
                runs.extend((asdict(direct), asdict(dynamic)))
                result = compare(direct, dynamic)
                print("\nA/B RESULT")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                comparisons.append(result)

        valid = [item for item in comparisons if item["same_functional_result"]]
        print("\nSUMMARY")
        print(f"valid same-result pairs: {len(valid)}/{len(comparisons)}")
        if valid:
            print(f"median input tokens saved: {statistics.median(item['input_tokens_saved'] for item in valid):.1f}")

        report = {
            "registered_mcp_tools": len(mcp_names),
            "model": getattr(provider, "model", None),
            "mcp_config": str(config_path),
            "repeat": args.repeat,
            "runs": runs,
            "comparisons": comparisons,
        }
        report_path = Path(args.report).resolve()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON report: {report_path}")
        return 0 if valid else 2
    finally:
        await manager.close()
        await provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-config",
        help="GitHub MCP config. Defaults to examples/github-mcp.json.",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--report", default="mcp_tool_search_ab_report.json")
    parser.add_argument("--log", default="mcp_tool_search_ab.log")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")

    root = find_root()
    if args.mcp_config is None:
        args.mcp_config = str(root / "examples" / "github-mcp.json")
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=False)
    except ImportError:
        pass
    prepare_imports(root)

    try:
        with Path(args.log).resolve().open("w", encoding="utf-8") as log_file:
            output = Tee(sys.__stdout__, log_file)
            with redirect_stdout(output):
                try:
                    return asyncio.run(benchmark(args, root))
                except KeyboardInterrupt:
                    message = "Interrupted."
                    print(f"\n{message}")
                    _write_failure_report(args.report, message)
                    return 130
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    print(f"\nFAIL: {message}")
                    _write_failure_report(args.report, message)
                    return 1
    except OSError as exc:
        print(f"\nFAIL: cannot write log file: {exc}", file=sys.stderr)
        return 1


def _write_failure_report(report: str, message: str) -> None:
    Path(report).resolve().write_text(
        json.dumps({"status": "failed", "error": message}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
