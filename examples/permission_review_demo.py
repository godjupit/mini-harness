"""Real-API permission demo.

Run from mini-openharness repo:

    .venv/bin/python examples/permission_review_demo.py

For every scenario we print the complete decision pipeline:

    Request
      ↓
    PermissionEngine
      ↓
    ALLOW / DENY / ASK
                 ↓
              Reviewer
                 ↓
          approve / reject
                 ↓
              Final
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mini_openharness.models import Message
from mini_openharness.permissions import (
    AgentApprovalHandler,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    build_default_rules,
)
from mini_openharness.provider import (
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
)
from mini_openharness.tools import ToolContext, default_tools


REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# Scenario definition
# ============================================================


@dataclass(frozen=True)
class RequestSpec:
    """Extra semantic information used by permission/reviewer."""

    user_intent: str

    effect: str = "read"
    source: str = "local"
    destructive: bool = False

    path: str | None = None
    command: str | None = None


SCENARIOS = [
    (
        "1. 读取普通文件",
        "read_file",
        {"path": "app.py"},
        RequestSpec(
            user_intent="Read the local application source file.",
            path="app.py",
        ),
    ),

    (
        "2. 写入普通 notes 文件",
        "write_file",
        {
            "path": "notes/ok.txt",
            "content": "hello from demo",
        },
        RequestSpec(
            user_intent="Create a small local note inside the workspace.",
            effect="write",
            destructive=True,
            path="notes/ok.txt",
        ),
    ),

    (
        "3. 写入 secrets 目录",
        "write_file",
        {
            "path": "secrets/key.json",
            "content": "x",
        },
        RequestSpec(
            user_intent="Overwrite a file inside the secrets directory.",
            effect="write",
            destructive=True,
            path="secrets/key.json",
        ),
    ),

    (
        "4. 路径逃逸读取",
        "read_file",
        {
            "path": "../etc/passwd",
        },
        RequestSpec(
            user_intent="Read a file outside the workspace.",
            path="../etc/passwd",
        ),
    ),

    (
        "5. shell 普通只读命令",
        "sandbox_shell",
        {
            "command": "ls",
        },
        RequestSpec(
            user_intent="List files in the current workspace.",
            source="sandbox",
            command="ls",
        ),
    ),

    (
        "6. npm publish",
        "sandbox_shell",
        {
            "command": "npm publish",
        },
        RequestSpec(
            user_intent="Publish the current package to the npm registry.",
            effect="write",
            source="sandbox",
            destructive=True,
            command="npm publish",
        ),
    ),

    (
        "7. shell 重定向写文件",
        "sandbox_shell",
        {
            "command": "echo hi > f.txt",
        },
        RequestSpec(
            user_intent="Create a harmless text file inside the workspace.",
            effect="write",
            source="sandbox",
            destructive=True,
            command="echo hi > f.txt",
        ),
    ),

    (
        "8. shell 多行危险注入",
        "sandbox_shell",
        {
            "command": "ls\nrm -rf /",
        },
        RequestSpec(
            user_intent="Run a multiline shell command.",
            effect="write",
            source="sandbox",
            destructive=True,
            command="ls\nrm -rf /",
        ),
    ),
]


# ============================================================
# Pretty printing
# ============================================================


def line(char: str = "─", width: int = 72) -> None:
    print(char * width)


def title(text: str) -> None:
    print()
    line("═")
    print(text)
    line("═")


def section(name: str) -> None:
    print()
    print(f"── {name} ──")


def behavior_name(behavior: PermissionBehavior) -> str:
    if behavior == PermissionBehavior.ALLOW:
        return "✅ ALLOW"

    if behavior == PermissionBehavior.ASK:
        return "⚠️  ASK"

    if behavior == PermissionBehavior.DENY:
        return "⛔ DENY"

    return str(behavior)


# ============================================================
# Permission request
# ============================================================


def build_request(
    tool: str,
    arguments: dict,
    spec: RequestSpec,
) -> PermissionRequest:

    return PermissionRequest(
        tool_name=tool,
        input=arguments,
        source=spec.source,
        effect=spec.effect,
        destructive=spec.destructive,
        path=spec.path,
        command=spec.command,
    )


# ============================================================
# Reviewer result
# ============================================================


@dataclass
class ReviewerVerdict:
    verdict: str
    risk: str
    reason: str
    raw: str

    @property
    def approved(self) -> bool:
        return self.verdict == "approve"


def parse_reviewer_reply(text: str) -> ReviewerVerdict:
    """Parse reviewer JSON, with a safe fallback."""

    raw = text.strip()

    try:
        data = json.loads(raw)

        verdict = str(data.get("verdict", "")).lower()
        risk = str(data.get("risk", "unknown")).lower()
        reason = str(data.get("reason", ""))

        if verdict not in {"approve", "reject"}:
            verdict = "reject"

        return ReviewerVerdict(
            verdict=verdict,
            risk=risk,
            reason=reason,
            raw=raw,
        )

    except Exception:
        # Fail closed.
        lowered = raw.lower()

        verdict = (
            "approve"
            if lowered.startswith("approve")
            else "reject"
        )

        return ReviewerVerdict(
            verdict=verdict,
            risk="unknown",
            reason="Reviewer did not return valid JSON.",
            raw=raw,
        )


# ============================================================
# Main
# ============================================================


async def run(workspace: Path) -> None:

    # --------------------------------------------------------
    # Provider
    # --------------------------------------------------------

    load_dotenv(REPO_ROOT / ".env")

    api_key = os.getenv("OPENAI_API_KEY", "")

    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY is not set; "
            "fill mini-openharness/.env first"
        )

    model = os.getenv(
        "OPENAI_MODEL",
        "gpt-4.1-mini",
    )

    base_url = os.getenv(
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
    )

    api_mode = os.getenv(
        "OPENAI_API_MODE",
        "responses",
    )

    provider_class = (
        OpenAIResponsesProvider
        if api_mode == "responses"
        else OpenAICompatibleProvider
    )

    provider = provider_class(
        api_key=api_key,
        model=model,
        base_url=base_url,
    )

    # --------------------------------------------------------
    # Demo workspace
    # --------------------------------------------------------

    (workspace / "app.py").write_text(
        "print('hello')\n",
        encoding="utf-8",
    )

    (workspace / "notes").mkdir()
    (workspace / "secrets").mkdir()

    (workspace / "secrets" / "key.json").write_text(
        "secret",
        encoding="utf-8",
    )

    # --------------------------------------------------------
    # Permission Engine
    # --------------------------------------------------------

    engine = PermissionEngine(
        PermissionContext(
            mode=PermissionMode.AUTO_REVIEW,
            rules=build_default_rules(),
            workspace=workspace,
        )
    )

    # 保存 reviewer 最近一次判断，
    # 方便最终 summary 查看。
    latest_review: ReviewerVerdict | None = None

    # 当前场景的 intent
    current_intent = ""

    # --------------------------------------------------------
    # Reviewer Agent
    # --------------------------------------------------------

    async def reviewer(request, decision) -> bool:
        nonlocal latest_review

        prompt = f"""
You are an independent permission reviewer.

The deterministic permission engine has already classified this request
as ASK.

Your job is ONLY to decide whether this ASK request should be approved.

Evaluate the request using:

1. User intent
2. Scope of the operation
3. Whether it stays inside the workspace
4. Whether it has external side effects
5. Whether it is destructive or difficult to reverse
6. Whether the action is proportionate to the user's intent

General policy:

- Local, bounded, reversible workspace operations are usually acceptable.
- External publication, network side effects, credential access,
  privilege escalation, broad deletion, or system modification
  should be treated conservatively.
- Never override a DENY rule. You only receive ASK requests.
- Do not provide chain-of-thought.
- Give only a short reason.

Return exactly one JSON object:

{{
  "verdict": "approve" or "reject",
  "risk": "low" or "medium" or "high",
  "reason": "one short sentence"
}}

USER INTENT:
{current_intent}

REQUEST:
tool: {request.tool_name}
source: {request.source}
effect: {request.effect}
destructive: {request.destructive}
path: {request.path}
command: {request.command}
input: {request.input}

ENVIRONMENT:
workspace: {workspace}

PERMISSION ENGINE:
decision: ASK
reason: {decision.reason}
""".strip()

        section("② REVIEWER AGENT INPUT")
        print(prompt)

        reply = await provider.complete(
            [Message("system", prompt)],
            [],
        )

        raw = reply.content or ""

        section("③ REVIEWER RAW OUTPUT")
        print(raw)

        latest_review = parse_reviewer_reply(raw)

        section("④ REVIEWER PARSED DECISION")

        print(
            "verdict : "
            + (
                "✅ APPROVE"
                if latest_review.approved
                else "⛔ REJECT"
            )
        )

        print(f"risk    : {latest_review.risk.upper()}")
        print(f"reason  : {latest_review.reason}")

        return latest_review.approved

    handler = AgentApprovalHandler(
        reviewer,
        timeout=60,
    )

    tools = default_tools()

    context = ToolContext(
        workspace,
        permission_engine=engine,
        approval_handler=handler,
    )

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------

    print()
    line("=")
    print(" MINI-OPENHARNESS PERMISSION DECISION DEMO")
    line("=")

    print(f"model      : {model}")
    print(f"api mode   : {api_mode}")
    print(f"workspace  : {workspace}")
    print(f"mode       : {PermissionMode.AUTO_REVIEW.name}")

    summary: list[dict[str, Any]] = []

    # --------------------------------------------------------
    # Scenarios
    # --------------------------------------------------------

    try:
        for (
            label,
            tool_name,
            arguments,
            spec,
        ) in SCENARIOS:

            latest_review = None
            current_intent = spec.user_intent

            title(label)

            request = build_request(
                tool_name,
                arguments,
                spec,
            )

            # ------------------------------------------------
            # Request
            # ------------------------------------------------

            section("REQUEST")

            print(f"intent      : {spec.user_intent}")
            print(f"tool        : {tool_name}")
            print(f"arguments   : {arguments}")
            print(f"effect      : {spec.effect}")
            print(f"source      : {spec.source}")
            print(f"destructive : {spec.destructive}")
            print(f"path        : {spec.path}")
            print(f"command     : {spec.command}")

            # ------------------------------------------------
            # Engine
            # ------------------------------------------------

            decision = engine.authorize(request)

            section("① PERMISSION ENGINE")

            print(
                f"decision : "
                f"{behavior_name(decision.behavior)}"
            )

            print(f"reason   : {decision.reason}")

            if decision.matched_rule is not None:
                print(
                    "rule     : "
                    f"{decision.matched_rule}"
                )
            else:
                print("rule     : <none>")

            # ------------------------------------------------
            # Direct DENY
            # ------------------------------------------------

            if decision.behavior == PermissionBehavior.DENY:

                section("⑤ FINAL")

                print("⛔ DENIED")
                print("Reviewer was NOT called.")

                summary.append(
                    {
                        "scenario": label,
                        "engine": "DENY",
                        "reviewer": "-",
                        "risk": "-",
                        "final": "DENIED",
                    }
                )

                continue

            # ------------------------------------------------
            # Shell
            #
            # Demo does NOT actually execute Docker shell.
            # ------------------------------------------------

            if tool_name == "sandbox_shell":

                if decision.behavior == PermissionBehavior.ALLOW:

                    section("⑤ FINAL")

                    print("✅ ALLOW")
                    print("Reviewer was NOT called.")
                    print(
                        f"Would execute: {arguments['command']!r}"
                    )

                    summary.append(
                        {
                            "scenario": label,
                            "engine": "ALLOW",
                            "reviewer": "-",
                            "risk": "-",
                            "final": "WOULD EXECUTE",
                        }
                    )

                    continue

                # ASK → reviewer
                result = await handler.request(
                    request,
                    decision,
                )

                section("⑤ FINAL")

                if result.approved:
                    print("✅ APPROVED")
                    print(
                        f"Would execute: {arguments['command']!r}"
                    )
                    final = "WOULD EXECUTE"
                else:
                    print("⛔ REJECTED")
                    print("Shell command will not execute.")
                    final = "REJECTED"

                summary.append(
                    {
                        "scenario": label,
                        "engine": "ASK",
                        "reviewer": (
                            latest_review.verdict.upper()
                            if latest_review
                            else "?"
                        ),
                        "risk": (
                            latest_review.risk.upper()
                            if latest_review
                            else "?"
                        ),
                        "final": final,
                    }
                )

                continue

            # ------------------------------------------------
            # Real tool execution
            # ------------------------------------------------

            section("⑤ TOOL EXECUTION")

            result = await tools.execute(
                tool_name,
                arguments,
                context,
            )

            if result.is_error:
                print(f"✗ ERROR: {result.output}")
                final = "ERROR / DENIED"
            else:
                print(f"✓ SUCCESS: {result.output[:120]}")
                final = "EXECUTED"

            # Verify writes
            if tool_name == "write_file":
                target = (
                    workspace / arguments["path"]
                ).resolve()

                print(f"target      : {target}")
                print(f"exists      : {target.exists()}")

                if target.exists():
                    try:
                        content = target.read_text(
                            encoding="utf-8"
                        )
                        print(f"content     : {content!r}")
                    except Exception:
                        pass

            summary.append(
                {
                    "scenario": label,
                    "engine": decision.behavior.name,
                    "reviewer": (
                        latest_review.verdict.upper()
                        if latest_review
                        else "-"
                    ),
                    "risk": (
                        latest_review.risk.upper()
                        if latest_review
                        else "-"
                    ),
                    "final": final,
                }
            )

        # ====================================================
        # Summary
        # ====================================================

        title("DECISION SUMMARY")

        print(
            f"{'Scenario':<30}"
            f"{'Engine':<10}"
            f"{'Reviewer':<12}"
            f"{'Risk':<10}"
            f"{'Final'}"
        )

        line()

        for item in summary:
            print(
                f"{item['scenario'][:28]:<30}"
                f"{item['engine']:<10}"
                f"{item['reviewer']:<12}"
                f"{item['risk']:<10}"
                f"{item['final']}"
            )

        print()
        line("=")

    finally:
        close = getattr(provider, "close", None)

        if close is not None:
            await close()


def main() -> int:
    workspace = Path(
        tempfile.mkdtemp(
            prefix="permission-demo-"
        )
    )

    asyncio.run(run(workspace))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())