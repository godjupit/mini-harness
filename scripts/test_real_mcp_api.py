#!/usr/bin/env python3
"""Use a real LLM API to verify remote Streamable HTTP MCP integration.

Run from the mini-openharness directory:

    ./.venv/bin/python scripts/test_remote_mcp_api.py

Required environment:

    OPENAI_API_KEY

Optional environment:

    OPENAI_MODEL      default: gpt-4.1-mini
    OPENAI_BASE_URL   default: https://api.openai.com/v1
    OPENAI_API_MODE   responses or chat, default: responses

This script tests:

    real LLM
        ->
    Mini OpenHarness ToolRegistry
        ->
    McpTool adapter
        ->
    remote MCP over Streamable HTTP
        ->
    HelloBooks public MCP server
        ->
    tool result
        ->
    real LLM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Project setup
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """Return repository root."""
    return Path(__file__).resolve().parents[1]


def prepare_imports(root: Path) -> None:
    """Ensure src/ is imported before repository-root helper scripts.

    The repository may contain files such as mcp.py which could shadow
    the installed MCP SDK package. Remove the project root itself from
    sys.path and explicitly prepend src/.
    """
    root_text = str(root)

    while root_text in sys.path:
        sys.path.remove(root_text)

    src = str(root / "src")

    if src not in sys.path:
        sys.path.insert(0, src)


# ---------------------------------------------------------------------------
# Permission approval
# ---------------------------------------------------------------------------


async def approve_tool(_request, _decision) -> bool:
    """Automatically approve the MCP tool for this integration test."""
    return True


# ---------------------------------------------------------------------------
# LLM provider
# ---------------------------------------------------------------------------


def build_provider():
    """Create the real OpenAI/OpenAI-compatible provider."""

    from mini_openharness.provider import (
        OpenAICompatibleProvider,
        OpenAIResponsesProvider,
    )

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "未设置 OPENAI_API_KEY。\n"
            "\n"
            "例如：\n"
            "\n"
            "    export OPENAI_API_KEY='你的 API Key'\n"
            "\n"
            "也可以写入项目根目录的 .env 文件。"
        )

    mode = os.getenv(
        "OPENAI_API_MODE",
        "responses",
    ).strip().lower()

    if mode not in {"responses", "chat"}:
        raise RuntimeError(
            "OPENAI_API_MODE 只能是 responses 或 chat"
        )

    provider_class = (
        OpenAIResponsesProvider
        if mode == "responses"
        else OpenAICompatibleProvider
    )

    return provider_class(
        api_key=api_key,
        model=os.getenv(
            "OPENAI_MODEL",
            "gpt-4.1-mini",
        ),
        base_url=os.getenv(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        ),
        timeout=120,
        max_retries=2,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def run(prompt: str) -> None:
    """Run the complete remote MCP integration test."""

    from mini_openharness.mcp.mcp import McpManager
    from mini_openharness.models import Message
    from mini_openharness.permissions import HumanApprovalHandler
    from mini_openharness.tools import (
        ToolContext,
        ToolRegistry,
    )

    root = project_root()

    provider = build_provider()

    manager: McpManager | None = None

    # HelloBooks public MCP:
    #
    # - remote server
    # - Streamable HTTP
    # - no OAuth
    # - no API key
    # - read only
    #
    remote_mcp_url = "https://agents.hellobooks.ai/mcp"

    server_name = "hellobooks"

    # McpTool renames remote tools using:
    #
    #     mcp__<server_name>__<remote_tool_name>
    #
    # Remote tool:
    #
    #     list_plans
    #
    # therefore local ToolRegistry name becomes:
    #
    #     mcp__hellobooks__list_plans
    #
    expected_tool_name = "mcp__hellobooks__list_plans"

    with tempfile.TemporaryDirectory(
        prefix="mini-oh-remote-mcp-"
    ) as temp_dir:

        temp = Path(temp_dir)

        # ---------------------------------------------------------------
        # Create temporary MCP config
        # ---------------------------------------------------------------

        config = temp / "mcp.json"

        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        server_name: {
                            "url": remote_mcp_url,
                        }
                    }
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        registry = ToolRegistry()

        manager = McpManager.from_file(config)

        try:
            # ===========================================================
            # STEP 1
            # Connect to the external MCP server
            # ===========================================================

            print("=" * 72)
            print("1. Connecting remote MCP")
            print("=" * 72)

            print("Server:")
            print(f"  {remote_mcp_url}")

            registered = await manager.connect_and_register(
                registry
            )

            print("\nRegistered MCP tools:")

            for name in registered:
                print(f"  - {name}")

            if not registered:
                raise RuntimeError(
                    "远程 MCP Server 没有返回任何 tools"
                )

            # ===========================================================
            # STEP 2
            # Check expected external tool
            # ===========================================================

            print()
            print("=" * 72)
            print("2. Checking expected MCP tool")
            print("=" * 72)

            if expected_tool_name not in registered:
                raise RuntimeError(
                    "没有注册到预期工具。\n"
                    f"\nExpected:\n"
                    f"  {expected_tool_name}\n"
                    f"\nActual:\n"
                    + "\n".join(
                        f"  {name}"
                        for name in registered
                    )
                )

            print(
                "Found:",
                expected_tool_name,
            )

            # ===========================================================
            # STEP 3
            # Read converted tool schema
            # ===========================================================

            print()
            print("=" * 72)
            print("3. Tool schema exposed to LLM")
            print("=" * 72)

            all_schemas = registry.schemas()

            schemas = [
                schema
                for schema in all_schemas
                if schema["name"] == expected_tool_name
            ]

            if not schemas:
                raise RuntimeError(
                    f"ToolRegistry 中找不到 schema: "
                    f"{expected_tool_name}"
                )

            print(
                json.dumps(
                    schemas,
                    indent=2,
                    ensure_ascii=False,
                )
            )

            # ===========================================================
            # STEP 4
            # Ask real LLM to generate tool call
            # ===========================================================

            print()
            print("=" * 72)
            print("4. Calling real LLM")
            print("=" * 72)

            model = os.getenv(
                "OPENAI_MODEL",
                "gpt-4.1-mini",
            )

            print("Model:", model)

            messages = [
                Message(
                    "system",
                    (
                        "You are testing a remote MCP integration.\n"
                        "\n"
                        f"You MUST call the tool "
                        f"{expected_tool_name}.\n"
                        "\n"
                        "Do not answer the pricing question from "
                        "your own knowledge.\n"
                        "\n"
                        "After receiving the tool result, briefly "
                        "summarize it for the user."
                    ),
                ),
                Message(
                    "user",
                    prompt,
                ),
            ]

            first = await provider.complete(
                messages,
                schemas,
            )

            print("\nAssistant content before tool:")
            print(
                first.content
                if first.content
                else "(empty)"
            )

            print("\nModel tool calls:")

            if not first.tool_calls:
                print("  (none)")

            for call in first.tool_calls:
                print(
                    json.dumps(
                        {
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )

            calls = [
                call
                for call in first.tool_calls
                if call.name == expected_tool_name
            ]

            if not calls:
                raise RuntimeError(
                    "模型没有调用预期 MCP 工具。\n"
                    "\n"
                    f"Expected:\n"
                    f"  {expected_tool_name}\n"
                    "\n"
                    f"Assistant content:\n"
                    f"  {first.content!r}"
                )

            call = calls[0]

            # ===========================================================
            # STEP 5
            # Execute MCP tool through Mini OpenHarness
            # ===========================================================

            print()
            print("=" * 72)
            print("5. Executing remote MCP tool")
            print("=" * 72)

            print("Tool:")
            print(f"  {call.name}")

            print("\nArguments:")
            print(
                json.dumps(
                    call.arguments,
                    indent=2,
                    ensure_ascii=False,
                )
            )

            context = ToolContext(
                workspace=temp,
                approval_handler=HumanApprovalHandler(
                    approve_tool
                ),
                tool_timeout_seconds=30,
            )

            result = await registry.execute(
                call.name,
                call.arguments,
                context,
            )

            print("\nMCP ToolResult:")
            print(result.output)

            print("\nToolResult metadata:")
            print(
                json.dumps(
                    result.metadata,
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )
            )

            if result.is_error:
                raise RuntimeError(
                    "远程 MCP 工具执行失败:\n"
                    f"{result.output}"
                )

            # ===========================================================
            # STEP 6
            # Feed MCP result back into model
            # ===========================================================

            print()
            print("=" * 72)
            print("6. Feeding MCP result back to LLM")
            print("=" * 72)

            messages.extend(
                [
                    Message(
                        "assistant",
                        first.content,
                        tool_calls=first.tool_calls,
                    ),
                    Message(
                        "tool",
                        result.output,
                        tool_call_id=call.id,
                        name=call.name,
                    ),
                ]
            )

            final = await provider.complete(
                messages,
                schemas,
            )

            # ===========================================================
            # STEP 7
            # Final result
            # ===========================================================

            print()
            print("=" * 72)
            print("7. Final model response")
            print("=" * 72)

            print(final.content)

            print()
            print("=" * 72)
            print("PASS")
            print("=" * 72)

            print(
                "real LLM"
                " -> ToolRegistry"
                " -> McpTool"
                " -> Streamable HTTP"
                " -> external MCP"
                " -> ToolResult"
                " -> real LLM"
            )

        finally:
            # Closing McpManager closes:
            #
            # MCP Client
            # transport
            # HTTP client
            #
            if manager is not None:
                await manager.close()

            await provider.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--prompt",
        default=(
            "请通过 MCP 查询 HelloBooks 当前有哪些定价计划，"
            "然后简单总结一下。"
        ),
    )

    args = parser.parse_args()

    root = project_root()

    # Load .env if python-dotenv exists.
    try:
        from dotenv import load_dotenv

        load_dotenv(
            root / ".env",
            override=False,
        )

    except ImportError:
        pass

    prepare_imports(root)

    try:
        asyncio.run(
            run(args.prompt)
        )

    except KeyboardInterrupt:
        print(
            "\nInterrupted.",
            file=sys.stderr,
        )
        return 130

    except Exception as exc:
        print(
            f"\nFAIL: "
            f"{type(exc).__name__}: "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())