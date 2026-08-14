from __future__ import annotations

import asyncio
from pathlib import Path

from mini_openharness.permissions import (
    AgentApprovalHandler,
    ApprovalResult,
    HumanApprovalHandler,
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
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


def ask_decision() -> PermissionDecision:
    return PermissionDecision(PermissionBehavior.ASK, "needs approval")


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
        == PermissionBehavior.ALLOW
    )
    assert (
        engine.authorize(request("list_dir", path=".")).behavior
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


def test_workspace_edits_allowed_and_sensitive_writes_ask(tmp_path):
    engine = make_engine(tmp_path)

    assert (
        engine.authorize(
            request("write_file", path="src/app.py", effect="write")
        ).behavior
        == PermissionBehavior.ALLOW
    )
    assert (
        engine.authorize(
            request("write_file", path="tests/test_x.py", effect="write")
        ).behavior
        == PermissionBehavior.ALLOW
    )
    assert (
        engine.authorize(request("write_file", path=".env", effect="write")).behavior
        == PermissionBehavior.ASK
    )
    assert (
        engine.authorize(request("edit_file", path=".git/config", effect="write")).behavior
        == PermissionBehavior.ASK
    )
    assert (
        engine.authorize(request("write_file", path=".npmrc", effect="write")).behavior
        == PermissionBehavior.ASK
    )


def test_engine_decisions_are_mode_independent(tmp_path):
    cases = [
        request("read_file", path="a.py"),
        request("write_file", path="x.txt", effect="write"),
        request("read_file", path="../escape"),
        request("sandbox_shell", command="echo hi > f.txt", effect="write"),
    ]
    default_engine = make_engine(tmp_path, mode=PermissionMode.DEFAULT)
    review_engine = make_engine(tmp_path, mode=PermissionMode.AUTO_REVIEW)

    for case in cases:
        assert (
            default_engine.authorize(case).behavior
            == review_engine.authorize(case).behavior
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
            {"path": ".npmrc", "content": "ok"},
            ToolContext(
                tmp_path,
                permission_engine=make_engine(tmp_path),
                approval_handler=HumanApprovalHandler(approve),
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
    assert event.data["path"] == ".npmrc"


def test_explicit_deny_overrides_rules_and_default(tmp_path):
    engine = make_engine(
        tmp_path,
        rules=PermissionRules(
            deny=[PermissionRule(PermissionBehavior.DENY, tool="write_file", pattern="*")]
        ),
    )
    result = asyncio.run(
        default_tools().execute(
            "write_file",
            {"path": "blocked.txt", "content": "no"},
            ToolContext(tmp_path, permission_engine=engine),
        )
    )
    assert result.is_error
    assert not (tmp_path / "blocked.txt").exists()


def test_explicit_ask_rule_always_asks(tmp_path):
    rules = PermissionRules(
        ask=[PermissionRule(PermissionBehavior.ASK, tool="sandbox_shell", pattern="npm publish*")]
    )
    engine = make_engine(tmp_path, rules=rules)

    decision = engine.authorize(
        request("sandbox_shell", command="npm publish", effect="write")
    )

    assert decision.behavior == PermissionBehavior.ASK


def test_complex_shell_is_ask(tmp_path):
    engine = make_engine(tmp_path, rules=PermissionRules())

    decision = engine.authorize(
        request("sandbox_shell", command="echo hi > f.txt", effect="write")
    )

    assert decision.behavior == PermissionBehavior.ASK


def test_shell_routine_commands_are_allowed(tmp_path):
    engine = make_engine(tmp_path)
    allowed = [
        "ls",
        "pwd",
        "cd src && ls",
        "cd src && git status",
        "cd tests && pytest",
        "python -m pytest tests/test_x.py",
        "git diff",
        "git branch",
        "python --version",
        "npm run test",
        "grep abc main.py",
        "find . -name '*.py'",
        "file README.md",
        "git status | head -20",
        "ls /usr/bin | grep python",
        "cat file | grep hello",
        "cd src\nls",
    ]
    for command in allowed:
        decision = engine.authorize(
            request("sandbox_shell", command=command, effect="write")
        )
        assert decision.behavior == PermissionBehavior.ALLOW, command


def test_shell_unsafe_or_uncertain_commands_ask(tmp_path):
    engine = make_engine(tmp_path)
    cases = [
        "cd src && rm -rf *",
        "git reset --hard",
        "git push",
        "pip install requests",
        "rm file.txt",
        "cd .. && ls",
        "pytest && rm file",
        "echo hello > file.txt",
        "cmd1 || cmd2",
    ]
    for command in cases:
        decision = engine.authorize(
            request("sandbox_shell", command=command, effect="write")
        )
        assert decision.behavior == PermissionBehavior.ASK, command


def test_shell_destructive_commands_deny(tmp_path):
    engine = make_engine(tmp_path)
    cases = [
        "ls && rm -rf /",
        "rm -rf /*",
        "cd src && rm -rf /",
    ]
    for command in cases:
        decision = engine.authorize(
            request("sandbox_shell", command=command, effect="write")
        )
        assert decision.behavior == PermissionBehavior.DENY, command


def test_shell_multiline_command_is_denied(tmp_path):
    engine = make_engine(tmp_path)

    decision = engine.authorize(
        request("sandbox_shell", command="ls\nrm -rf /", effect="write")
    )

    assert decision.behavior == PermissionBehavior.DENY


def test_human_approval_handler():
    req = request("write_file", effect="write")

    async def approve(r, d):
        del r, d
        return True

    async def deny(r, d):
        del r, d
        return False

    async def run():
        approved = await HumanApprovalHandler(approve).request(req, ask_decision())
        denied = await HumanApprovalHandler(deny).request(req, ask_decision())
        no_callback = await HumanApprovalHandler().request(req, ask_decision())
        not_ask = await HumanApprovalHandler().request(
            req, PermissionDecision(PermissionBehavior.ALLOW, "ok")
        )
        return approved, denied, no_callback, not_ask

    approved, denied, no_callback, not_ask = asyncio.run(run())

    assert approved == ApprovalResult(approved=True)
    assert denied == ApprovalResult(approved=False)
    assert no_callback == ApprovalResult(approved=False)
    assert not_ask == ApprovalResult(approved=True)


def test_agent_approval_handler():
    req = request("write_file", effect="write")

    async def approve(r, d):
        del r, d
        return True

    async def reject(r, d):
        del r, d
        return False

    async def explode(r, d):
        del r, d
        raise RuntimeError("reviewer down")

    async def slow(r, d):
        del r, d
        await asyncio.sleep(5)
        return True

    async def invalid(r, d):
        del r, d
        return "maybe"  # type: ignore[return-value]

    async def run():
        approved = await AgentApprovalHandler(approve).request(req, ask_decision())
        rejected = await AgentApprovalHandler(reject).request(req, ask_decision())
        crashed = await AgentApprovalHandler(explode).request(req, ask_decision())
        timed_out = await AgentApprovalHandler(slow, timeout=0.05).request(
            req, ask_decision()
        )
        unparseable = await AgentApprovalHandler(invalid).request(req, ask_decision())
        not_ask = await AgentApprovalHandler(reject).request(
            req, PermissionDecision(PermissionBehavior.ALLOW, "ok")
        )
        return approved, rejected, crashed, timed_out, unparseable, not_ask

    approved, rejected, crashed, timed_out, unparseable, not_ask = asyncio.run(run())

    assert approved == ApprovalResult(approved=True)
    assert rejected == ApprovalResult(approved=False)
    assert crashed == ApprovalResult(approved=False)
    assert timed_out == ApprovalResult(approved=False)
    assert unparseable == ApprovalResult(approved=False)
    assert not_ask == ApprovalResult(approved=True)


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
    assert denied.behavior == PermissionBehavior.ALLOW


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

        async def run(self, *, command, timeout):
            del command, timeout
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
