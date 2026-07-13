from __future__ import annotations

import asyncio

from mini_openharness.permissions import PermissionPolicy, PermissionRule
from mini_openharness.tools import ToolContext, default_tools
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
