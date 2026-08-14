from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from mini_openharness.engine import AgentLoop
from mini_openharness.hooks import (
    CallbackHook,
    CommandHook,
    HookEvent,
    HookExecutor,
    HookRegistry,
    HookResult,
    load_hook_registry,
)
from mini_openharness.models import ModelReply, ToolCall
from mini_openharness.permissions import (
    HumanApprovalHandler,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
    PermissionRules,
)
from mini_openharness.tools import ToolRegistry, ToolResult
from mini_openharness.trace import TraceStore, TraceWriter


async def approve_all(request, decision):
    del request, decision
    return True


def approve_all_handler() -> HumanApprovalHandler:
    return HumanApprovalHandler(approve_all)


def allow_all_engine(workspace: Path) -> PermissionEngine:
    return PermissionEngine(
        PermissionContext(
            mode=PermissionMode.DEFAULT,
            rules=PermissionRules(
                allow=[
                    PermissionRule(PermissionBehavior.ALLOW, tool="*", pattern="*")
                ]
            ),
            workspace=workspace,
        )
    )


class ScriptedProvider:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append((list(messages), tools))
        return self.replies.pop(0)


def collect(loop: AgentLoop, prompt: str):
    async def run():
        return [event async for event in loop.run(prompt)]

    return asyncio.run(run())


def execute(executor: HookExecutor, event: HookEvent, payload: dict):
    return asyncio.run(executor.execute(event, payload))


def test_registry_runs_priority_order_and_chains_payload_updates(tmp_path):
    seen = []

    def lower(context):
        seen.append(("lower", context.payload["prompt"]))

    def higher(context):
        seen.append(("higher", context.payload["prompt"]))
        return HookResult(updated_payload={"prompt": "rewritten"})

    registry = HookRegistry()
    registry.register(
        HookEvent.USER_PROMPT_SUBMIT,
        CallbackHook("lower", lower, priority=1),
    )
    registry.register(
        HookEvent.USER_PROMPT_SUBMIT,
        CallbackHook("higher", higher, priority=10),
    )

    result = execute(
        HookExecutor(registry, workspace=tmp_path),
        HookEvent.USER_PROMPT_SUBMIT,
        {"prompt": "original"},
    )

    assert seen == [("higher", "original"), ("lower", "rewritten")]
    assert result.payload["prompt"] == "rewritten"


def test_matcher_skips_unrelated_tool_and_block_short_circuits(tmp_path):
    seen = []
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE,
        CallbackHook(
            "write-only",
            lambda context: seen.append(context.payload["tool_name"]),
            matcher="write_*",
            priority=20,
        ),
    )
    registry.register(
        HookEvent.PRE_TOOL_USE,
        CallbackHook(
            "deny",
            lambda context: HookResult(blocked=True, reason="protected"),
            matcher="read_file",
            priority=10,
        ),
    )
    registry.register(
        HookEvent.PRE_TOOL_USE,
        CallbackHook("never", lambda context: seen.append("never")),
    )

    result = execute(
        HookExecutor(registry, workspace=tmp_path),
        HookEvent.PRE_TOOL_USE,
        {"tool_name": "read_file", "tool_input": {}},
    )

    assert result.blocked
    assert result.reason == "protected"
    assert seen == []


@pytest.mark.parametrize(
    ("failure_mode", "blocked"),
    [("block", True), ("continue", False)],
)
def test_timeout_obeys_failure_mode(tmp_path, failure_mode, blocked):
    async def slow(context):
        del context
        await asyncio.sleep(1)

    registry = HookRegistry()
    registry.register(
        HookEvent.STOP,
        CallbackHook(
            "slow",
            slow,
            timeout_seconds=0.01,
            failure_mode=failure_mode,
        ),
    )

    result = execute(
        HookExecutor(registry, workspace=tmp_path),
        HookEvent.STOP,
        {"response": "done"},
    )

    assert result.blocked is blocked
    assert result.results[0].failed
    assert "TimeoutError" in result.reason


def test_command_config_supports_matcher_python_placeholder_and_json_updates(tmp_path):
    script = tmp_path / "rewrite.py"
    script.write_text(
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "value = request['payload']['tool_input']['value']\n"
        "print(json.dumps({'decision': 'allow', 'updated_payload': "
        "{'tool_input': {'value': value.upper()}}}))\n",
        encoding="utf-8",
    )
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "pre_tool_use": [
                        {
                            "name": "uppercase",
                            "type": "command",
                            "command": ["{python}", "rewrite.py"],
                            "matcher": "echo",
                            "expect_json": True,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = execute(
        HookExecutor(load_hook_registry(config), workspace=tmp_path),
        HookEvent.PRE_TOOL_USE,
        {"tool_name": "echo", "tool_input": {"value": "hello"}},
    )

    assert result.payload["tool_input"] == {"value": "HELLO"}


def test_plain_command_is_a_verification_gate_by_exit_code(tmp_path):
    registry = HookRegistry()
    registry.register(
        HookEvent.STOP,
        CommandHook(
            "verify",
            (sys.executable, "-c", "print('2 tests passed')"),
            failure_mode="block",
        ),
    )

    result = execute(
        HookExecutor(registry, workspace=tmp_path),
        HookEvent.STOP,
        {"response": "done"},
    )

    assert not result.blocked
    assert result.results[0].output == "2 tests passed"


def test_command_failure_blocks_without_inheriting_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    registry = HookRegistry()
    registry.register(
        HookEvent.STOP,
        CommandHook(
            "failed-check",
            (
                sys.executable,
                "-c",
                "import os, sys; print(os.getenv('OPENAI_API_KEY', 'not-inherited')); sys.exit(2)",
            ),
        ),
    )

    result = execute(
        HookExecutor(registry, workspace=tmp_path),
        HookEvent.STOP,
        {"response": "done"},
    )

    assert result.blocked
    assert result.results[0].failed
    assert "not-inherited" in result.reason
    assert "must-not-leak" not in result.reason


def test_command_timeout_kills_child_process(tmp_path):
    marker = tmp_path / "orphan.txt"
    registry = HookRegistry()
    registry.register(
        HookEvent.STOP,
        CommandHook(
            "slow-command",
            (
                sys.executable,
                "-c",
                "import pathlib, sys, time; time.sleep(.1); pathlib.Path(sys.argv[1]).write_text('late')",
                str(marker),
            ),
            timeout_seconds=0.01,
        ),
    )

    result = execute(
        HookExecutor(registry, workspace=tmp_path),
        HookEvent.STOP,
        {"response": "done"},
    )
    time.sleep(0.15)

    assert result.blocked
    assert not marker.exists()


def test_pre_and_post_tool_hooks_transform_the_real_execution(tmp_path):
    executed = []

    class EchoTool:
        name = "echo"
        description = "echo one value"
        parameters = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        read_only = True

        async def run(self, arguments, context):
            del context
            executed.append(arguments["value"])
            return ToolResult(arguments["value"])

    tools = ToolRegistry()
    tools.register(EchoTool())
    hooks = HookRegistry()
    hooks.register(
        HookEvent.PRE_TOOL_USE,
        CallbackHook(
            "rewrite-input",
            lambda context: HookResult(updated_payload={"tool_input": {"value": "safe"}}),
        ),
    )
    hooks.register(
        HookEvent.POST_TOOL_USE,
        CallbackHook(
            "annotate-output",
            lambda context: HookResult(
                updated_payload={"tool_output": context.payload["tool_output"] + ":checked"}
            ),
        ),
    )
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("1", "echo", {"value": "unsafe"}),)),
            ModelReply(content="done"),
        ]
    )

    collect(
        AgentLoop(
            provider=provider,
            tools=tools,
            workspace=tmp_path,
            hooks=hooks,
            permission_engine=allow_all_engine(tmp_path),
        ),
        "go",
    )

    assert executed == ["safe"]
    assert provider.requests[1][0][-1].content == "safe:checked"


def test_pre_tool_block_becomes_observation_without_side_effect(tmp_path):
    calls = 0

    class MutationTool:
        name = "mutate"
        description = "mutate"
        parameters = {"type": "object", "additionalProperties": False}
        read_only = False

        async def run(self, arguments, context):
            nonlocal calls
            del arguments, context
            calls += 1
            return ToolResult("changed")

    tools = ToolRegistry()
    tools.register(MutationTool())
    hooks = HookRegistry()
    hooks.register(
        HookEvent.PRE_TOOL_USE,
        CallbackHook(
            "deny-mutation",
            lambda context: HookResult(blocked=True, reason="policy says no"),
        ),
    )
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("1", "mutate", {}),)),
            ModelReply(content="recovered"),
        ]
    )

    collect(
        AgentLoop(
            provider=provider,
            tools=tools,
            workspace=tmp_path,
            approval_handler=approve_all_handler(),
            hooks=hooks,
        ),
        "go",
    )

    assert calls == 0
    assert "Hook blocked mutate" in provider.requests[1][0][-1].content


def test_stop_hook_blocks_completion_then_agent_recovers_and_trace_proves_it(tmp_path):
    attempts = 0

    def verify(context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return HookResult(blocked=True, reason="tests failed")
        return HookResult(output="tests passed")

    hooks = HookRegistry()
    hooks.register(HookEvent.STOP, CallbackHook("verification-gate", verify))
    tracer = TraceWriter(tmp_path / "traces", run_id="hooks")
    provider = ScriptedProvider(
        [ModelReply(content="premature"), ModelReply(content="verified")]
    )
    loop = AgentLoop(
        provider=provider,
        tools=ToolRegistry(),
        workspace=tmp_path,
        hooks=hooks,
        tracer=tracer,
    )

    events = collect(loop, "build it")

    assert attempts == 2
    assert any(event.kind == "hook_blocked" for event in events)
    assert events[-1].kind == "done"
    assert "tests failed" in provider.requests[1][0][-1].content
    trace = list(TraceStore(tmp_path / "traces").read("hooks"))
    assert [event.kind for event in trace].count("hook_start") == 2
    assert [event.kind for event in trace].count("hook_end") == 2
    assert trace[-1].data["status"] == "completed"


def test_user_prompt_hook_can_rewrite_or_reject_before_provider_call(tmp_path):
    rewrite = HookRegistry()
    rewrite.register(
        HookEvent.USER_PROMPT_SUBMIT,
        CallbackHook(
            "normalize",
            lambda context: HookResult(updated_payload={"prompt": "normalized"}),
        ),
    )
    provider = ScriptedProvider([ModelReply(content="done")])
    collect(
        AgentLoop(provider=provider, tools=ToolRegistry(), workspace=tmp_path, hooks=rewrite),
        "original",
    )
    assert provider.requests[0][0][-1].content == "normalized"

    reject = HookRegistry()
    reject.register(
        HookEvent.USER_PROMPT_SUBMIT,
        CallbackHook("reject", lambda context: HookResult(blocked=True, reason="not allowed")),
    )
    rejected_provider = ScriptedProvider([ModelReply(content="unused")])
    events = collect(
        AgentLoop(
            provider=rejected_provider,
            tools=ToolRegistry(),
            workspace=tmp_path,
            hooks=reject,
        ),
        "original",
    )
    assert rejected_provider.requests == []
    assert [event.kind for event in events] == ["hook_blocked", "error"]


def test_invalid_failure_mode_is_rejected_at_registration():
    registry = HookRegistry()
    with pytest.raises(ValueError, match="failure_mode"):
        registry.register(
            HookEvent.STOP,
            CallbackHook("bad", lambda context: None, failure_mode="invalid"),  # type: ignore[arg-type]
        )
