from __future__ import annotations

import asyncio

from mini_openharness.permissions import PermissionPolicy, PermissionRule
from mini_openharness.sandbox import SandboxedShellTool
from mini_openharness.tools import ToolContext, ToolRegistry, ToolResult, default_tools
from mini_openharness.trace import TraceStore, TraceWriter


def test_rules_match_tool_and_path_before_default():
    policy = PermissionPolicy(
        [
            PermissionRule("deny", tool="write_file", path="secrets/*"),
            PermissionRule("allow", tool="write_file", path="docs/*"),
        ],
        default_mutation="ask",
    )
    assert (
        policy.evaluate(tool_name="write_file", read_only=False, path="docs/a.md").action == "allow"
    )
    assert (
        policy.evaluate(tool_name="write_file", read_only=False, path="secrets/a").action == "deny"
    )
    assert policy.evaluate(tool_name="write_file", read_only=False, path="src/a.py").action == "ask"
    assert policy.evaluate(tool_name="read_file", read_only=True, path="src/a.py").action == "allow"
    assert policy.evaluate(tool_name="mcp__demo__send", read_only=False).action == "ask"


def test_ask_callback_and_decision_are_traced(tmp_path):
    decisions = []

    async def approve(tool, reason):
        decisions.append((tool, reason))
        return True

    tracer = TraceWriter(tmp_path / "traces", run_id="approval")
    result = asyncio.run(
        default_tools().execute(
            "write_file",
            {"path": "approved.txt", "content": "ok"},
            ToolContext(
                tmp_path,
                permission_policy=PermissionPolicy(default_mutation="ask"),
                approval_callback=approve,
                tracer=tracer,
            ),
        )
    )

    assert not result.is_error
    assert decisions[0][0] == "write_file"
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


def test_explicit_deny_overrides_allow_write(tmp_path):
    policy = PermissionPolicy(
        [PermissionRule("deny", tool="write_file", path="*")],
        default_mutation="allow",
    )
    result = asyncio.run(
        default_tools().execute(
            "write_file",
            {"path": "blocked.txt", "content": "no"},
            ToolContext(tmp_path, allow_write=True, permission_policy=policy),
        )
    )
    assert result.is_error
    assert not (tmp_path / "blocked.txt").exists()


def test_permission_policy_loads_json_rules(tmp_path):
    path = tmp_path / "permissions.json"
    path.write_text(
        '{"default":"deny","rules":[{"tool":"write_*","path":"docs/*","action":"allow"}]}',
        encoding="utf-8",
    )
    policy = PermissionPolicy.from_file(path)
    assert policy.default_mutation == "deny"
    assert (
        policy.evaluate(tool_name="write_file", read_only=False, path="docs/a.md").action == "allow"
    )


def test_shell_rules_check_each_compound_subcommand_and_deny_wins():
    policy = PermissionPolicy(
        [
            PermissionRule("allow", tool="sandbox_shell", command="npm *"),
            PermissionRule("allow", tool="sandbox_shell", command="echo *"),
            PermissionRule("deny", tool="sandbox_shell", command="npm publish*"),
        ]
    )

    assert (
        policy.evaluate(
            tool_name="sandbox_shell",
            read_only=False,
            command="npm install && echo done",
        ).action
        == "allow"
    )
    assert (
        policy.evaluate(
            tool_name="sandbox_shell",
            read_only=False,
            command="npm install && npm publish",
        ).action
        == "deny"
    )


def test_shell_rules_ask_for_unknown_or_complex_commands():
    policy = PermissionPolicy(
        [PermissionRule("allow", tool="sandbox_shell", command="npm *")],
        default_mutation="ask",
    )

    unknown = policy.evaluate(
        tool_name="sandbox_shell",
        read_only=False,
        command="npm install && curl example.com",
    )
    substitution = policy.evaluate(
        tool_name="sandbox_shell",
        read_only=False,
        command="npm install $(cat package-name)",
    )

    assert unknown.action == "ask"
    assert "curl example.com" in unknown.reason
    assert substitution.action == "ask"
    assert "too complex" in substitution.reason


def test_exact_shell_rule_can_allow_intentionally_complex_command():
    command = "printf ok > result.txt"
    policy = PermissionPolicy(
        [PermissionRule("allow", tool="sandbox_shell", command=command)]
    )

    decision = policy.evaluate(
        tool_name="sandbox_shell",
        read_only=False,
        command=command,
    )

    assert decision.action == "allow"


def test_tool_registry_passes_shell_command_to_permission_policy(tmp_path):
    class FakeSandbox:
        called = False

        async def run(self, *, workspace, command, timeout):
            del workspace, command, timeout
            self.called = True
            return ToolResult("ran")

    sandbox = FakeSandbox()
    tools = ToolRegistry()
    tools.register(SandboxedShellTool(sandbox))
    policy = PermissionPolicy(
        [PermissionRule("deny", tool="sandbox_shell", command="npm publish*")],
        default_mutation="allow",
    )

    result = asyncio.run(
        tools.execute(
            "sandbox_shell",
            {"command": "npm publish"},
            ToolContext(tmp_path, permission_policy=policy),
        )
    )

    assert result.is_error
    assert result.failure is not None
    assert result.failure.code == "permission_denied"
    assert sandbox.called is False


def test_permission_policy_loads_command_rules(tmp_path):
    path = tmp_path / "permissions.json"
    path.write_text(
        '{"default":"ask","rules":['
        '{"tool":"sandbox_shell","command":"npm test*","action":"allow"}]}',
        encoding="utf-8",
    )

    policy = PermissionPolicy.from_file(path)

    assert policy.rules[0].command == "npm test*"
