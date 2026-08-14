"""Real-API probe: does the model delegate to the right subagent on its own?

Run from the mini-openharness repo:

    .venv/bin/python examples/verify_agent_distinction.py
    .venv/bin/python examples/verify_agent_distinction.py --workspace /path/to/repo "your own task"

The task prompt deliberately never mentions subagents, so any ``agent`` tool
call is the model's own decision. The script records which ``agent_type`` it
picked and whether the subagent run succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from mini_openharness.engine import AgentLoop
from mini_openharness.multiagent import build_agent_tool, default_agents
from mini_openharness.provider import (
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
)
from mini_openharness.tools import default_tools

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TASK = (
    "我想给这个项目增加一个 'mini-oh doctor' 命令，用来检查运行环境是否就绪"
    "（API key、Docker、MCP 配置等）。"
    "请先调查清楚现有的 CLI 子命令结构和运行时是怎么构建的，"
    "然后给我一份具体到文件的实施方案。"
)


async def run_probe(workspace: Path, task: str) -> None:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set; fill mini-openharness/.env first")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_mode = os.getenv("OPENAI_API_MODE", "responses")
    provider_class = (
        OpenAIResponsesProvider if api_mode == "responses" else OpenAICompatibleProvider
    )
    provider = provider_class(api_key=api_key, model=model, base_url=base_url)

    tools = default_tools()
    registry = default_agents()
    tools.register(
        build_agent_tool(
            provider=provider,
            tools=tools,
            workspace=workspace,
            definitions=registry,
        )
    )

    agent_schema = next(schema for schema in tools.schemas() if schema["name"] == "agent")
    print(f"model: {model} ({api_mode})")
    print(f"workspace: {workspace}")
    print(f"agent tool description: {agent_schema['description']!r}")
    print(f"agent tool parameters: {agent_schema['parameters']['properties']}")
    print(f"registered agents: {registry.names()}")
    print("task:", task)
    print()

    loop = AgentLoop(
        provider=provider,
        tools=tools,
        workspace=workspace,
        max_steps=15,
        tool_timeout_seconds=300,
    )

    calls: list[dict] = []
    final_answer = ""
    try:
        async for event in loop.run(task):
            if event.kind == "tool_start" and event.data["name"] == "agent":
                calls.append(event.data["input"])
                print(f">>> delegate: {event.data['input']}")
            elif event.kind == "tool_end" and event.data["name"] == "agent":
                print(f"<<< subagent ({event.data['elapsed_ms']}ms): {event.message[:300]}")
            elif event.kind == "tool_start":
                print(f"→ {event.data['name']} {event.data['input']}")
            elif event.kind == "tool_end":
                marker = "✗" if event.data["is_error"] else "✓"
                print(
                    f"{marker} {event.data['name']} ({event.data['elapsed_ms']}ms): "
                    f"{event.message[:120]}"
                )
            elif event.kind == "assistant":
                final_answer = event.message
                print(f"[assistant] {event.message[:200]}")
            elif event.kind == "done":
                data = event.data
                print(
                    f"[done] steps={data.get('steps')} "
                    f"tokens={data.get('input_tokens', 0) + data.get('output_tokens', 0)} "
                    f"estimated_cost=${data.get('estimated_cost', 0):.6f}"
                )
            elif event.kind in {"error", "cancelled", "hook_blocked"}:
                print(f"[{event.kind}] {event.message}")
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    print()
    print("=== 模型最终回复（完整） ===")
    print(final_answer)
    print()
    print("=== verdict ===")
    if not calls:
        print("模型没有调用 agent 工具（这是它自己的决定，任务可能不需要委派）。")
        return
    valid = set(registry.names())
    picked = [call.get("agent_type") for call in calls]
    unknown = [name for name in picked if name not in valid]
    print(f"agent 调用次数: {len(calls)}")
    print(f"选择的 agent_type: {picked}")
    if unknown:
        print(f"注意: 模型使用了未注册的 agent_type: {unknown}")
        print("原因很可能是工具描述没有列出可选类型，模型在猜。")
    else:
        print("所有调用都用了已注册的 agent_type。")
    distinct = set(picked)
    if len(distinct) >= 2:
        print("模型区分出了多个 agent 类型。")
    else:
        print(f"模型只使用了: {distinct}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=REPO_ROOT)
    parser.add_argument("task", nargs="*", help="task prompt; defaults to a real planning task")
    args = parser.parse_args(argv)
    task = " ".join(args.task) or DEFAULT_TASK
    try:
        asyncio.run(run_probe(args.workspace, task))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
