from __future__ import annotations

import asyncio
from pathlib import Path

from mini_openharness.permissions import (
    ApprovalHandler,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionRules,
    build_default_rules,
    load_rules_from_json,
)
from mini_openharness.sandbox import SandboxedShellTool
from mini_openharness.tools import ToolContext, ToolRegistry, ToolResult, default_tools
from mini_openharness.trace import TraceStore, TraceWriter


def make_engine(
    workspace: Path,
    *,
    mode: PermissionMode = PermissionMode.DEFAULT,
    rules: PermissionRules | None = None,
) -> PermissionEngine:
    return PermissionEngine(
        PermissionContext(
            mode=mode,
            rules=rules if rules is not None else build_default_rules(),
            workspace=workspace,
        )
    )


def request(
    tool: str,
    *,
    path: str | None = None,
    command: str | None = None,
    effect: str = "read",
) -> PermissionRequest:
    return PermissionRequest(
        tool_name=tool,
        input={"path": path} if path else {},
        path=path,
        command=command,
        effect=effect,
    )


def test_engine_rules_safety_and_defaults(tmp_path):
    engine = make_engine(tmp_path)

    assert (
        engine.authorize(request("read_file", path="docs/a.md")).behavior
        == PermissionBehavior.ALLOW
    )
    assert (
        engine.authorize(request("write_file", path="secrets/a", effect="write")).behavior
        == PermissionBehavior.DENY
    )
    assert (
        engine.authorize(request("write_file", path="src/a.py", effect="write")).behavior
        == PermissionBehavior.ASK
    )
    assert (
        engine.authorize(request("list_files", path=".")).behavior
        == PermissionBehavior.ALLOW
    )
    assert (
        engine.authorize(request("load_skill", effect="compute")).behavior
        == PermissionBehavior.ALLOW
    )
    assert (
        engine.authorize(request("custom_tool", effect="unknown")).behavior
        == PermissionBehavior.ASK
    )
    assert (
        engine.authorize(request("read_file", path="../escape")).behavior
        == PermissionBehavior.DENY
    )
    assert (
        engine.authorize(
            request("sandbox_shell", command="npm publish", effect="write")
        ).behavior
        == PermissionBehavior.ASK
    )
    assert (
        engine.authorize(
            request("sandbox_shell", command="$(rm -rf /)", effect="write")
        ).behavior
        == PermissionBehavior.ASK
    )


def test_ask_decision_goes_through_approval_and_is_traced(tmp_path):
    approvals = []

    async def approve(req, decision):
        approvals.append(req.tool_name)
        return True

    tracer = TraceWriter(tmp_path / "traces", run_id="approval")
    result = asyncio.run(
        default_tools().execute(
            "write_file",
            {"path": "approved.txt", "content": "ok"},
            ToolContext(
                tmp_path,
                permission_engine=make_engine(tmp_path),
                approval_handler=ApprovalHandler(approve),
                tracer=tracer,
            ),
        )
    )

    assert not result.is_error
    assert approvals == ["write_file"]
    event = [
        event
        for event in TraceStore(tmp_path / "traces").read("approval")
        if event.kind == "permission_decision"
    ][0]
    assert event.data["requested_action"] == "ask"
    assert event.data["allowed"] is True
    assert event.data["source"] == "local"
    assert event.data["effect"] == "write"
    assert event.data["descriptor_inferred"] is False
    assert event.data["path"] == "approved.txt"


def test_explicit_deny_overrides_accept_edits(tmp_path):
    engine = make_engine(
        tmp_path,
        mode=PermissionMode.ACCEPT_EDITS,
        rules=PermissionRules(
            deny=[PermissionRule(PermissionBehavior.DENY, tool="write_file", pattern="*")]
        ),
    )
    result = asyncio.run(
        default_tools().execute(
            "write_file",
            {"path": "blocked.txt", "content": "no"},
            ToolContext(tmp_path, allow_write=True, permission_engine=engine),
        )
    )
    assert result.is_error
    assert not (tmp_path / "blocked.txt").exists()


def test_bypass_mode_allows_unmatched_tools(tmp_path):
    engine = make_engine(tmp_path, mode=PermissionMode.BYPASS, rules=PermissionRules())

    decision = engine.authorize(request("anything", effect="write"))

    assert decision.behavior == PermissionBehavior.ALLOW


def test_explicit_ask_not_bypassed_by_mode(tmp_path):
    rules = PermissionRules(
        ask=[PermissionRule(PermissionBehavior.ASK, tool="sandbox_shell", pattern="npm publish*")]
    )
    engine = make_engine(tmp_path, mode=PermissionMode.BYPASS, rules=rules)

    decision = engine.authorize(
        request("sandbox_shell", command="npm publish", effect="write")
    )

    assert decision.behavior == PermissionBehavior.ASK


def test_bypass_auto_passes_complex_shell(tmp_path):
    engine = make_engine(tmp_path, mode=PermissionMode.BYPASS, rules=PermissionRules())

    decision = engine.authorize(
        request("sandbox_shell", command="echo hi > f.txt", effect="write")
    )

    assert decision.behavior == PermissionBehavior.ALLOW


def test_load_rules_from_json(tmp_path):
    path = tmp_path / "permissions.json"
    path.write_text(
        '{"rules":[{"tool":"write_*","path":"docs/*","action":"allow"}]}',
        encoding="utf-8",
    )
    engine = make_engine(tmp_path, rules=load_rules_from_json(path))

    allowed = engine.authorize(request("write_file", path="docs/a.md", effect="write"))
    denied = engine.authorize(request("write_file", path="src/a.py", effect="write"))

    assert allowed.behavior == PermissionBehavior.ALLOW
    assert denied.behavior == PermissionBehavior.ASK


def test_load_rules_from_json_maps_command_to_pattern(tmp_path):
    path = tmp_path / "permissions.json"
    path.write_text(
        '{"rules":[{"tool":"sandbox_shell","command":"npm test*","action":"allow"}]}',
        encoding="utf-8",
    )

    rules = load_rules_from_json(path)

    assert rules.allow[0].pattern == "npm test*"


def test_tool_registry_passes_shell_command_to_engine(tmp_path):
    class FakeSandbox:
        called = False

        async def run(self, *, workspace, command, timeout):
            del workspace, command, timeout
            self.called = True
            return ToolResult("ran")

    sandbox = FakeSandbox()
    tools = ToolRegistry()
    tools.register(SandboxedShellTool(sandbox))
    engine = make_engine(
        tmp_path,
        rules=PermissionRules(
            deny=[
                PermissionRule(
                    PermissionBehavior.DENY,
                    tool="sandbox_shell",
                    pattern="npm publish*",
                )
            ]
        ),
    )

    result = asyncio.run(
        tools.execute(
            "sandbox_shell",
            {"command": "npm publish"},
            ToolContext(tmp_path, permission_engine=engine),
        )
    )

    assert result.is_error
    assert result.failure is not None
    assert result.failure.code == "permission_denied"
    assert sandbox.called is False
