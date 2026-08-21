#!/usr/bin/env python3
"""
Mini OpenHarness MCP v2 end-to-end verifier.

默认执行：
  1. 检查项目、Python、mcp 2.x、httpx2
  2. 自动兼容两种 MCP 代码布局：
       A) src/mini_openharness/mcp.py
       B) src/mini_openharness/mcp/mcp.py
  3. 运行 tests/test_mcp.py
  4. 启动真实 stdio MCP Server 子进程并进行：
       MCP Client -> tools/list -> ToolRegistry -> tools/call
  5. 启动真实 Streamable HTTP MCP Server 子进程并进行：
       MCP Client -> HTTP -> tools/list -> ToolRegistry -> tools/call
  6. 如果存在 OPENAI_API_KEY：
       真实 LLM -> tool call -> MCP Server -> ToolResult -> LLM 最终回复
  7. 如果存在 OPENAI_API_KEY：
       真实 AgentLoop -> LLM -> MCP Tool -> MCP Server -> LLM -> done

可选：
  --third-party
      使用 npx 启动官方 filesystem MCP Server 做第三方互操作测试。

环境变量：
  OPENAI_API_KEY      必需（真实模型测试）
  OPENAI_MODEL        默认 gpt-4.1-mini
  OPENAI_BASE_URL     默认 https://api.openai.com/v1
  OPENAI_API_MODE     responses 或 chat，默认 responses

运行：
  python verify_mcp_e2e.py

只测 MCP，不调用真实模型：
  python verify_mcp_e2e.py --skip-llm

跳过 pytest：
  python verify_mcp_e2e.py --skip-pytest

附加第三方 MCP Server 测试：
  python verify_mcp_e2e.py --third-party
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


RESULTS: list[Check] = []


def section(title: str) -> None:
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def passed(name: str, detail: str = "") -> None:
    RESULTS.append(Check(name, "PASS", detail))
    suffix = f" — {detail}" if detail else ""
    print(f"[PASS] {name}{suffix}")


def skipped(name: str, detail: str = "") -> None:
    RESULTS.append(Check(name, "SKIP", detail))
    suffix = f" — {detail}" if detail else ""
    print(f"[SKIP] {name}{suffix}")


def failed(name: str, detail: str) -> None:
    RESULTS.append(Check(name, "FAIL", detail))
    print(f"[FAIL] {name} — {detail}")
    raise RuntimeError(f"{name}: {detail}")


def summary() -> None:
    section("Summary")
    width = max((len(x.name) for x in RESULTS), default=10)
    for item in RESULTS:
        suffix = f"  {item.detail}" if item.detail else ""
        print(f"{item.status:<4}  {item.name:<{width}}{suffix}")

    p = sum(x.status == "PASS" for x in RESULTS)
    s = sum(x.status == "SKIP" for x in RESULTS)
    f = sum(x.status == "FAIL" for x in RESULTS)
    print()
    print(f"PASS={p}  SKIP={s}  FAIL={f}")


# ---------------------------------------------------------------------------
# Repository / imports
# ---------------------------------------------------------------------------


def find_repo_root(explicit: str | None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / "pyproject.toml").is_file():
            raise RuntimeError(f"找不到 pyproject.toml: {root}")
        return root

    starts = [Path.cwd().resolve(), Path(__file__).resolve().parent]
    visited: set[Path] = set()

    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in visited:
                continue
            visited.add(candidate)
            if (
                (candidate / "pyproject.toml").is_file()
                and (candidate / "src" / "mini_openharness").exists()
            ):
                return candidate

    raise RuntimeError(
        "无法自动找到 Mini OpenHarness 项目根目录。"
        "请在项目内执行，或使用 --repo /path/to/mini-openharness。"
    )


def add_src_to_path(repo: Path) -> None:
    src = str((repo / "src").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)


def remove_verifier_dir_from_path() -> None:
    """Prevent this verifier file (named mcp.py) from shadowing the SDK."""
    verifier_dir = str(Path(__file__).resolve().parent)
    while verifier_dir in sys.path:
        sys.path.remove(verifier_dir)


def load_dotenv_if_available(repo: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(repo / ".env", override=False)


def import_mcp_runtime():
    """
    自动兼容：
      mini_openharness/mcp.py
    和
      mini_openharness/mcp/mcp.py
    """

    errors: list[str] = []

    candidates = (
        "mini_openharness.mcp",
        "mini_openharness.mcp.mcp",
    )

    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue

        manager = getattr(module, "McpManager", None)
        tool = getattr(module, "McpTool", None)

        if manager is not None and tool is not None:
            return module_name, manager, tool

        errors.append(
            f"{module_name}: module imported, but McpManager/McpTool not exported"
        )

    raise ImportError(
        "无法找到 McpManager。尝试过：\n  - "
        + "\n  - ".join(errors)
    )


def version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------


async def approve_everything(request, decision) -> bool:
    del request, decision
    return True


def make_approval_handler():
    from mini_openharness.permissions import HumanApprovalHandler

    return HumanApprovalHandler(approve_everything)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(repo: Path) -> None:
    section("1. Preflight")

    print(f"repo:   {repo}")
    print(f"python: {sys.executable}")
    print(f"ver:    {sys.version.split()[0]}")

    mcp_version = version("mcp")
    if mcp_version is None:
        failed("MCP SDK", "没有安装 mcp")
    try:
        mcp_major = int(mcp_version.split(".", 1)[0])
    except ValueError:
        mcp_major = -1

    if mcp_major != 2:
        failed("MCP SDK v2", f"当前 mcp={mcp_version}，要求 >=2,<3")
    passed("MCP SDK v2", f"mcp={mcp_version}")

    httpx2_version = version("httpx2")
    if httpx2_version is None:
        failed("httpx2", "没有安装 httpx2")
    passed("httpx2", f"httpx2={httpx2_version}")

    module_name, McpManager, McpTool = import_mcp_runtime()
    del McpManager, McpTool
    passed("MCP runtime import", module_name)

    try:
        from mini_openharness.tools import ToolContext, ToolRegistry
        from mini_openharness.engine import AgentLoop
        from mini_openharness.provider import (
            OpenAICompatibleProvider,
            OpenAIResponsesProvider,
        )
        del ToolContext, ToolRegistry, AgentLoop
        del OpenAICompatibleProvider, OpenAIResponsesProvider
    except Exception as exc:
        failed("Mini OpenHarness core imports", f"{type(exc).__name__}: {exc}")
    passed("Mini OpenHarness core imports")

    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        passed("OPENAI_API_KEY", "已设置；不会打印 key")
        print(f"       OPENAI_MODEL={os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')}")
        print(f"       OPENAI_API_MODE={os.getenv('OPENAI_API_MODE', 'responses')}")
        print(
            "       OPENAI_BASE_URL="
            f"{os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')}"
        )
    else:
        skipped("OPENAI_API_KEY", "未设置；真实模型与 AgentLoop 测试将跳过")


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------


def run_mcp_pytest(repo: Path) -> None:
    section("2. Existing pytest")

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(repo / "tests" / "test_mcp.py"),
        "-q",
    ]
    print("$ " + " ".join(cmd))

    env = os.environ.copy()
    src = str((repo / "src").resolve())
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (src, env.get("PYTHONPATH", "")) if item
    )
    result = subprocess.run(
        cmd,
        cwd=repo.parent,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    print(result.stdout.rstrip())

    if result.returncode != 0:
        failed("tests/test_mcp.py", f"pytest exit={result.returncode}")

    passed("tests/test_mcp.py")


# ---------------------------------------------------------------------------
# Temporary MCP servers
# ---------------------------------------------------------------------------


def write_test_servers(tmp: Path) -> tuple[Path, Path]:
    stdio_server = tmp / "mcp_stdio_server.py"
    stdio_server.write_text(
        textwrap.dedent(
            """
            import os
            from mcp.server import MCPServer

            mcp = MCPServer("mini-openharness-real-stdio-test")


            @mcp.tool()
            def verification_probe(text: str) -> dict[str, object]:
                nonce = os.environ["MCP_VERIFY_NONCE"]
                return {
                    "marker": f"MCP_STDIO_OK:{nonce}",
                    "words": len(text.split()),
                    "echo": text,
                    "transport": "stdio",
                }


            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    http_server = tmp / "mcp_http_server.py"
    http_server.write_text(
        textwrap.dedent(
            """
            import os
            from mcp.server import MCPServer

            mcp = MCPServer("mini-openharness-real-http-test")


            @mcp.tool()
            def verification_probe(text: str) -> dict[str, object]:
                nonce = os.environ["MCP_VERIFY_NONCE"]
                return {
                    "marker": f"MCP_HTTP_OK:{nonce}",
                    "words": len(text.split()),
                    "echo": text,
                    "transport": "streamable-http",
                }


            if __name__ == "__main__":
                mcp.run(
                    transport="streamable-http",
                    host="127.0.0.1",
                    port=int(os.environ["PORT"]),
                    stateless_http=True,
                    json_response=True,
                )
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    return stdio_server, http_server


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def protocol_version(manager, server_name: str) -> str:
    clients = getattr(manager, "_clients", None)

    if isinstance(clients, dict):
        client = clients.get(server_name)
        if client is not None:
            value = getattr(client, "protocol_version", None)
            if value is not None:
                return str(value)

    return "unknown"


def marker_from_result(output: str, metadata: dict[str, Any], prefix: str) -> str | None:
    structured = metadata.get("structured_content")

    if isinstance(structured, dict):
        marker = structured.get("marker")
        if isinstance(marker, str) and marker.startswith(prefix):
            return marker

    match = re.search(
        re.escape(prefix) + r"[A-Za-z0-9_-]+",
        output,
    )
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Direct Mini OpenHarness MCP test
# ---------------------------------------------------------------------------


async def direct_call(
    *,
    config: Path,
    server_name: str,
    tool_name: str,
    workspace: Path,
    text: str,
    expected_marker: str,
) -> tuple[list[str], str, str]:
    _, McpManager, _ = import_mcp_runtime()

    from mini_openharness.tools import ToolContext, ToolRegistry

    registry = ToolRegistry()
    manager = McpManager.from_file(config)

    try:
        registered = await manager.connect_and_register(registry)

        if tool_name not in registered:
            raise AssertionError(
                f"没有发现预期工具 {tool_name!r}；实际：{registered!r}"
            )

        proto = protocol_version(manager, server_name)

        result = await registry.execute(
            tool_name,
            {"text": text},
            ToolContext(
                workspace=workspace,
                approval_handler=make_approval_handler(),
                tool_timeout_seconds=30,
            ),
        )

        if result.is_error:
            failure = result.failure.to_dict() if result.failure else None
            raise AssertionError(
                f"MCP tool 调用失败：output={result.output!r}, failure={failure!r}"
            )

        prefix = expected_marker.split(":", 1)[0] + ":"
        actual_marker = marker_from_result(
            result.output,
            result.metadata,
            prefix,
        )

        if actual_marker != expected_marker:
            raise AssertionError(
                "MCP Server 返回值不符合预期。\n"
                f"expected={expected_marker!r}\n"
                f"actual={actual_marker!r}\n"
                f"output={result.output!r}\n"
                f"metadata={result.metadata!r}"
            )

        return registered, proto, result.output

    finally:
        await manager.close()


async def test_real_stdio(
    tmp: Path,
    stdio_server: Path,
    nonce: str,
) -> Path:
    section("3. Real stdio MCP")

    config = tmp / "stdio_mcp.json"

    write_json(
        config,
        {
            "mcpServers": {
                "stdio": {
                    "command": sys.executable,
                    "args": [str(stdio_server)],
                    "env": {
                        "MCP_VERIFY_NONCE": nonce,
                    },
                }
            }
        },
    )

    tool_name = "mcp__stdio__verification_probe"
    expected = f"MCP_STDIO_OK:{nonce}"

    registered, proto, output = await direct_call(
        config=config,
        server_name="stdio",
        tool_name=tool_name,
        workspace=tmp,
        text="alpha beta gamma delta",
        expected_marker=expected,
    )

    print("registered:")
    for name in registered:
        print("  -", name)

    print("protocol:", proto)
    print("output:  ", output)

    if proto != "2026-07-28":
        raise AssertionError(
            f"stdio 实际协商协议为 {proto!r}，不是 '2026-07-28'"
        )

    passed("Real stdio MCP", f"protocol={proto}")
    return config


# ---------------------------------------------------------------------------
# Streamable HTTP
# ---------------------------------------------------------------------------


def get_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(
    port: int,
    process: subprocess.Popen,
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"HTTP MCP Server 提前退出，exit={process.returncode}"
            )

        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return

        time.sleep(0.05)

    raise RuntimeError(f"HTTP MCP Server 未在 {timeout}s 内监听端口 {port}")


async def test_real_http(
    tmp: Path,
    http_server: Path,
    nonce: str,
) -> None:
    section("4. Real Streamable HTTP MCP")

    port = get_free_port()
    endpoint = f"http://127.0.0.1:{port}/mcp"

    log_path = tmp / "mcp_http_server.log"
    log_file = log_path.open("w+", encoding="utf-8")

    process = subprocess.Popen(
        [sys.executable, str(http_server)],
        env={
            **os.environ,
            "PORT": str(port),
            "MCP_VERIFY_NONCE": nonce,
        },
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        wait_for_port(port, process)

        config = tmp / "http_mcp.json"
        write_json(
            config,
            {
                "mcpServers": {
                    "http": {
                        "url": endpoint,
                    }
                }
            },
        )

        tool_name = "mcp__http__verification_probe"
        expected = f"MCP_HTTP_OK:{nonce}"

        registered, proto, output = await direct_call(
            config=config,
            server_name="http",
            tool_name=tool_name,
            workspace=tmp,
            text="one two three four five",
            expected_marker=expected,
        )

        print("endpoint:", endpoint)
        print("registered:")
        for name in registered:
            print("  -", name)
        print("protocol:", proto)
        print("output:  ", output)

        if proto != "2026-07-28":
            raise AssertionError(
                f"HTTP 实际协商协议为 {proto!r}，不是 '2026-07-28'"
            )

        passed("Real Streamable HTTP MCP", f"protocol={proto}")

    except Exception:
        log_file.flush()
        log_file.seek(0)
        log = log_file.read()

        if log.strip():
            print()
            print("--- HTTP MCP Server log ---")
            print(log[-6000:])
            print("--- end log ---")

        raise

    finally:
        process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

        log_file.close()


# ---------------------------------------------------------------------------
# Real model provider
# ---------------------------------------------------------------------------


def build_real_provider():
    from mini_openharness.provider import (
        OpenAICompatibleProvider,
        OpenAIResponsesProvider,
    )

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    base_url = os.getenv(
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
    )
    mode = os.getenv("OPENAI_API_MODE", "responses").strip().lower()

    if mode not in {"responses", "chat"}:
        raise RuntimeError(
            f"OPENAI_API_MODE={mode!r} 非法，只支持 responses/chat"
        )

    cls = (
        OpenAIResponsesProvider
        if mode == "responses"
        else OpenAICompatibleProvider
    )

    provider = cls(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout=120,
        max_retries=2,
    )

    return provider


async def test_real_llm_tool_call(
    tmp: Path,
    stdio_config: Path,
    nonce: str,
) -> None:
    section("5. Real LLM -> MCP Tool -> LLM")

    if not os.getenv("OPENAI_API_KEY"):
        skipped(
            "Real LLM -> MCP",
            "OPENAI_API_KEY 未设置",
        )
        return

    _, McpManager, _ = import_mcp_runtime()

    from mini_openharness.models import Message
    from mini_openharness.tools import ToolContext, ToolRegistry

    provider = build_real_provider()
    assert provider is not None

    manager = McpManager.from_file(stdio_config)
    registry = ToolRegistry()

    tool_name = "mcp__stdio__verification_probe"
    expected = f"MCP_STDIO_OK:{nonce}"

    try:
        registered = await manager.connect_and_register(registry)

        if tool_name not in registered:
            raise AssertionError(
                f"真实 LLM 测试前没有注册到 {tool_name}"
            )

        schemas = [
            schema
            for schema in registry.schemas()
            if schema["name"] == tool_name
        ]

        if len(schemas) != 1:
            raise AssertionError(
                f"没有拿到唯一 MCP tool schema：{schemas!r}"
            )

        messages = [
            Message(
                "system",
                (
                    "You are performing an integration test. "
                    f"You MUST call the tool named {tool_name}. "
                    "Do not answer from your own knowledge. "
                    "After receiving the tool result, output only the marker field."
                ),
            ),
            Message(
                "user",
                (
                    "Call the verification tool with text exactly equal to "
                    "'alpha beta gamma delta'."
                ),
            ),
        ]

        first = await provider.complete(messages, schemas)

        print("first model content:", repr(first.content))
        print(
            "tool calls:",
            [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in first.tool_calls
            ],
        )

        calls = [
            call
            for call in first.tool_calls
            if call.name == tool_name
        ]

        if not calls:
            raise AssertionError(
                "真实模型没有调用 MCP tool。"
                f"content={first.content!r}, calls={first.tool_calls!r}"
            )

        call = calls[0]

        if call.arguments.get("text") != "alpha beta gamma delta":
            raise AssertionError(
                f"模型修改了测试参数：{call.arguments!r}"
            )

        result = await registry.execute(
            call.name,
            call.arguments,
            ToolContext(
                workspace=tmp,
                approval_handler=make_approval_handler(),
                tool_timeout_seconds=30,
            ),
        )

        if result.is_error:
            raise AssertionError(
                f"MCP tool 实际执行失败：{result.output}"
            )

        actual_marker = marker_from_result(
            result.output,
            result.metadata,
            "MCP_STDIO_OK:",
        )

        if actual_marker != expected:
            raise AssertionError(
                f"真实 MCP Server marker 不匹配："
                f"expected={expected!r}, actual={actual_marker!r}"
            )

        messages.append(
            Message(
                "assistant",
                first.content,
                tool_calls=first.tool_calls,
            )
        )
        messages.append(
            Message(
                "tool",
                result.output,
                tool_call_id=call.id,
                name=call.name,
            )
        )

        final = await provider.complete(messages, schemas)

        print("final model content:", repr(final.content))

        if final.tool_calls:
            raise AssertionError(
                f"第二轮模型又请求了工具：{final.tool_calls!r}"
            )

        if expected not in final.content:
            raise AssertionError(
                "模型最终回答中没有真实 MCP Server 生成的随机 marker。\n"
                f"expected={expected!r}\n"
                f"final={final.content!r}"
            )

        passed(
            "Real LLM -> MCP",
            (
                f"model={os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')}, "
                f"mode={os.getenv('OPENAI_API_MODE', 'responses')}"
            ),
        )

    finally:
        await manager.close()
        await provider.close()


# ---------------------------------------------------------------------------
# Real AgentLoop
# ---------------------------------------------------------------------------


async def test_real_agent_loop(
    tmp: Path,
    stdio_config: Path,
    nonce: str,
) -> None:
    section("6. Real AgentLoop -> LLM -> MCP -> LLM")

    if not os.getenv("OPENAI_API_KEY"):
        skipped(
            "Real AgentLoop MCP E2E",
            "OPENAI_API_KEY 未设置",
        )
        return

    _, McpManager, _ = import_mcp_runtime()

    from mini_openharness.engine import AgentLoop
    from mini_openharness.tools import ToolRegistry

    provider = build_real_provider()
    assert provider is not None

    manager = McpManager.from_file(stdio_config)
    registry = ToolRegistry()

    tool_name = "mcp__stdio__verification_probe"
    expected = f"MCP_STDIO_OK:{nonce}"

    try:
        registered = await manager.connect_and_register(registry)

        if tool_name not in registered:
            raise AssertionError(
                f"AgentLoop 前没有注册到 {tool_name}"
            )

        loop = AgentLoop(
            provider=provider,
            tools=registry,
            workspace=tmp,
            system_prompt=(
                "You are executing a strict end-to-end MCP integration test. "
                f"When asked, you MUST use the tool {tool_name}. "
                "Never fabricate the tool result. "
                "After the tool returns, respond with only its marker field."
            ),
            max_steps=6,
            approval_handler=make_approval_handler(),
            tool_timeout_seconds=30,
            max_concurrent_tools=4,
        )

        prompt = (
            f"Call {tool_name} exactly once with "
            'text="alpha beta gamma delta". '
            "Then output only the marker returned by the tool."
        )

        saw_tool_start = False
        saw_tool_end = False
        saw_done = False
        final_assistant = ""
        tool_output = ""

        async for event in loop.run(prompt):
            if event.kind == "tool_start":
                print(
                    "[AgentLoop] tool_start:",
                    event.data.get("name"),
                    event.data.get("input"),
                )
                if event.data.get("name") == tool_name:
                    saw_tool_start = True

            elif event.kind == "tool_end":
                print(
                    "[AgentLoop] tool_end:",
                    event.data.get("name"),
                    repr(event.message),
                )
                if event.data.get("name") == tool_name:
                    saw_tool_end = True
                    tool_output = event.message

            elif event.kind == "assistant":
                print("[AgentLoop] assistant:", repr(event.message))
                final_assistant = event.message

            elif event.kind == "done":
                print(
                    "[AgentLoop] done:",
                    event.data,
                )
                saw_done = True

            elif event.kind == "error":
                print("[AgentLoop] error:", event.message)

        if not saw_tool_start:
            raise AssertionError(
                "AgentLoop 中没有观察到 MCP tool_start"
            )

        if not saw_tool_end:
            raise AssertionError(
                "AgentLoop 中没有观察到 MCP tool_end"
            )

        if expected not in tool_output:
            raise AssertionError(
                "AgentLoop tool_end 中没有真实 MCP marker。\n"
                f"expected={expected!r}\n"
                f"tool_output={tool_output!r}"
            )

        if not saw_done:
            raise AssertionError(
                "AgentLoop 没有正常产生 done 事件"
            )

        if expected not in final_assistant:
            raise AssertionError(
                "AgentLoop 最终模型回答没有包含真实 MCP marker。\n"
                f"expected={expected!r}\n"
                f"assistant={final_assistant!r}"
            )

        passed(
            "Real AgentLoop MCP E2E",
            "AgentLoop -> real model -> MCP -> real model -> done",
        )

    finally:
        await manager.close()
        await provider.close()


# ---------------------------------------------------------------------------
# Third-party MCP interoperability
# ---------------------------------------------------------------------------


async def test_third_party_filesystem(tmp: Path) -> None:
    section("7. Third-party filesystem MCP")

    npx = shutil.which("npx")

    if not npx:
        skipped(
            "Third-party filesystem MCP",
            "系统没有 npx",
        )
        return

    _, McpManager, _ = import_mcp_runtime()

    from mini_openharness.tools import ToolContext, ToolRegistry

    fs_root = tmp / "external_fs"
    fs_root.mkdir()

    proof_nonce = uuid.uuid4().hex
    proof = f"THIRD_PARTY_MCP_OK:{proof_nonce}"

    proof_file = fs_root / "proof.txt"
    proof_file.write_text(proof, encoding="utf-8")

    config = tmp / "third_party_mcp.json"
    write_json(
        config,
        {
            "mcpServers": {
                "filesystem": {
                    "command": npx,
                    "args": [
                        "-y",
                        "@modelcontextprotocol/server-filesystem",
                        str(fs_root),
                    ],
                    "trustToolAnnotations": True,
                }
            }
        },
    )

    manager = McpManager.from_file(config)
    registry = ToolRegistry()

    try:
        registered = await asyncio.wait_for(
            manager.connect_and_register(registry),
            timeout=120,
        )

        print("registered:")
        for name in registered:
            print("  -", name)

        read_candidates = [
            name
            for name in registered
            if name.endswith("__read_text_file")
            or name.endswith("__read_file")
        ]

        if not read_candidates:
            raise AssertionError(
                "第三方 filesystem MCP 已连接，"
                "但没有发现 read_text_file/read_file。\n"
                f"registered={registered!r}"
            )

        tool_name = read_candidates[0]

        result = await registry.execute(
            tool_name,
            {"path": str(proof_file)},
            ToolContext(
                workspace=tmp,
                approval_handler=make_approval_handler(),
                tool_timeout_seconds=60,
            ),
        )

        print("third-party output:", result.output)

        if result.is_error:
            raise AssertionError(
                f"第三方 filesystem tool 执行失败：{result.output}"
            )

        if proof not in result.output:
            raise AssertionError(
                "第三方 MCP 没有返回随机 proof。\n"
                f"expected={proof!r}\n"
                f"output={result.output!r}"
            )

        passed(
            "Third-party filesystem MCP",
            f"protocol={protocol_version(manager, 'filesystem')}",
        )

    finally:
        await manager.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_async_tests(
    args: argparse.Namespace,
) -> None:
    nonce = uuid.uuid4().hex

    with tempfile.TemporaryDirectory(
        prefix="mini-openharness-mcp-e2e-"
    ) as raw_tmp:
        tmp = Path(raw_tmp).resolve()

        print()
        print("temporary workspace:", tmp)
        print("verification nonce:", nonce)

        stdio_server, http_server = write_test_servers(tmp)

        stdio_config = await test_real_stdio(
            tmp,
            stdio_server,
            nonce,
        )

        await test_real_http(
            tmp,
            http_server,
            nonce,
        )

        if args.skip_llm:
            skipped(
                "Real LLM -> MCP",
                "--skip-llm",
            )
            skipped(
                "Real AgentLoop MCP E2E",
                "--skip-llm",
            )
        else:
            await test_real_llm_tool_call(
                tmp,
                stdio_config,
                nonce,
            )
            await test_real_agent_loop(
                tmp,
                stdio_config,
                nonce,
            )

        if args.third_party:
            await test_third_party_filesystem(tmp)
        else:
            skipped(
                "Third-party filesystem MCP",
                "未启用；使用 --third-party",
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mini OpenHarness MCP v2 real E2E verifier"
    )

    parser.add_argument(
        "--repo",
        help="项目根目录；默认自动探测",
    )

    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="跳过 tests/test_mcp.py",
    )

    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="跳过真实 API 和 AgentLoop 测试",
    )

    parser.add_argument(
        "--third-party",
        action="store_true",
        help="额外使用官方 filesystem MCP Server 做互操作测试",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        repo = find_repo_root(args.repo)
        remove_verifier_dir_from_path()
        add_src_to_path(repo)
        load_dotenv_if_available(repo)

        preflight(repo)

        if args.skip_pytest:
            skipped(
                "tests/test_mcp.py",
                "--skip-pytest",
            )
        else:
            run_mcp_pytest(repo)

        asyncio.run(
            run_async_tests(args)
        )

        summary()

        if any(x.status == "FAIL" for x in RESULTS):
            return 1

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        summary()
        return 130

    except Exception as exc:
        print()
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        summary()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
