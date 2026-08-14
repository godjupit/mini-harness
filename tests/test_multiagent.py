"""Tests for the independent multi-agent loop (AgentManager / AgentDefinition)."""

from __future__ import annotations

import asyncio

import pytest

from mini_openharness.engine import AgentLoop
from mini_openharness.multiagent import (
    AgentDefinition,
    AgentManager,
    AgentRegistry,
    AgentTool,
    build_agent_tool,
    default_agents,
    explore_agent,
    plan_agent,
)
from mini_openharness.models import ModelReply, ToolCall
from mini_openharness.provider import DemoProvider, ProviderError
from mini_openharness.tools import ToolContext, ToolRegistry, default_tools


class ScriptedProvider:
    """Deterministic provider that returns pre-scripted replies in order."""

    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.requests: list[tuple[list, list]] = []

    async def complete(self, messages, tools):
        self.requests.append((list(messages), tools))
        return self.replies.pop(0)


def run_agent(manager: AgentManager, definition: AgentDefinition, prompt: str) -> str:
    return asyncio.run(manager.run_agent(definition, prompt))


def test_run_agent_drives_full_tool_loop_and_returns_final_message(tmp_path):
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("1", "read_file", {"path": "hello.txt"}),)),
            ModelReply(content="exploration complete"),
        ]
    )
    manager = AgentManager(provider=provider, tools=default_tools(), workspace=tmp_path)

    final = run_agent(manager, explore_agent, "explore the workspace")

    assert final == "exploration complete"
    # The loop asked the model twice: once to pick a tool, once to answer.
    assert len(provider.requests) == 2
    # The second request's last message is the tool result the model observed.
    assert provider.requests[1][0][-1].content == "hello world"


def test_run_agent_only_exposes_the_subset_of_tools(tmp_path):
    provider = ScriptedProvider([ModelReply(content="done")])
    manager = AgentManager(provider=provider, tools=default_tools(), workspace=tmp_path)

    run_agent(manager, explore_agent, "explore")

    exposed_names = {tool["name"] for tool in provider.requests[0][1]}
    assert exposed_names == {"read_file", "list_files"}
    assert "write_file" not in exposed_names


def test_run_agent_unknown_tool_raises_key_error(tmp_path):
    manager = AgentManager(
        provider=ScriptedProvider([ModelReply(content="done")]),
        tools=default_tools(),
        workspace=tmp_path,
    )
    definition = AgentDefinition(
        type="broken",
        system_prompt="broken",
        max_turns=3,
        tools=("read_file", "does_not_exist"),
    )

    with pytest.raises(KeyError, match="does_not_exist"):
        run_agent(manager, definition, "go")


def test_run_agent_propagates_provider_error(tmp_path):
    class FailingProvider:
        async def complete(self, messages, tools):
            del messages, tools
            raise ProviderError("provider exploded")

    manager = AgentManager(provider=FailingProvider(), tools=default_tools(), workspace=tmp_path)

    with pytest.raises(RuntimeError, match="provider exploded"):
        run_agent(manager, explore_agent, "go")


def test_run_agent_smoke_with_demo_provider_needs_no_api_key(tmp_path):
    (tmp_path / "README.md").write_text("Mini OpenHarness", encoding="utf-8")
    manager = AgentManager(provider=DemoProvider(), tools=default_tools(), workspace=tmp_path)

    final = run_agent(manager, explore_agent, "explore")

    assert final.startswith("演示完成")


# --- AgentTool ---------------------------------------------------------------


def collect_loop(loop: AgentLoop, prompt: str) -> list:
    async def run() -> list:
        return [event async for event in loop.run(prompt)]

    return asyncio.run(run())


def test_agent_tool_dispatches_to_subagent_and_returns_result(tmp_path):
    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    sub_provider = ScriptedProvider(
        [
            ModelReply(tool_calls=(ToolCall("1", "read_file", {"path": "a.txt"}),)),
            ModelReply(content="subagent finished"),
        ]
    )
    manager = AgentManager(provider=sub_provider, tools=default_tools(), workspace=tmp_path)
    tool = AgentTool(manager=manager, definitions={"explore_agent": explore_agent})

    result = asyncio.run(
        tool.run(
            {"task": "find the file", "agent_type": "explore_agent"},
            ToolContext(tmp_path),
        )
    )

    assert result.output == "subagent finished"
    assert result.is_error is False
    # the subagent loop only saw the subset of tools its definition declares
    assert {item["name"] for item in sub_provider.requests[0][1]} == {
        "read_file",
        "list_files",
    }


def test_agent_tool_selects_definition_by_agent_type(tmp_path):
    writer = AgentDefinition(
        type="writer",
        system_prompt="you write files",
        max_turns=3,
        tools=("write_file",),
    )
    sub_provider = ScriptedProvider([ModelReply(content="written")])
    manager = AgentManager(provider=sub_provider, tools=default_tools(), workspace=tmp_path)
    tool = AgentTool(
        manager=manager,
        definitions={"explore_agent": explore_agent, "writer": writer},
    )

    result = asyncio.run(
        tool.run({"task": "write a file", "agent_type": "writer"}, ToolContext(tmp_path))
    )

    assert result.output == "written"
    # dispatching to "writer" exposed only the writer's tools, not the explorer's
    assert {item["name"] for item in sub_provider.requests[0][1]} == {"write_file"}


def test_agent_tool_wired_into_main_loop_delegates_to_subagent(tmp_path):
    sub_provider = ScriptedProvider([ModelReply(content="subagent result")])
    manager = AgentManager(provider=sub_provider, tools=default_tools(), workspace=tmp_path)
    tool = AgentTool(manager=manager, definitions={"explore_agent": explore_agent})

    registry = ToolRegistry()
    registry.register(tool)

    main_provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=(
                    ToolCall("m1", "agent", {"task": "go", "agent_type": "explore_agent"}),
                )
            ),
            ModelReply(content="main done"),
        ]
    )
    loop = AgentLoop(provider=main_provider, tools=registry, workspace=tmp_path)

    events = collect_loop(loop, "delegate this")

    assert events[-1].kind == "done"
    # the subagent actually ran and its answer reached the main model as an observation
    assert "subagent result" in main_provider.requests[1][0][-1].content
    assert sub_provider.requests  # subagent loop was invoked at least once


def test_build_agent_tool_defaults_to_explore_agent(tmp_path):
    provider = ScriptedProvider([ModelReply(content="explored")])
    tool = build_agent_tool(provider=provider, tools=default_tools(), workspace=tmp_path)

    result = asyncio.run(
        tool.run({"task": "go", "agent_type": "explore_agent"}, ToolContext(tmp_path))
    )

    assert result.output == "explored"
    assert result.is_error is False
    assert {item["name"] for item in provider.requests[0][1]} == {"read_file", "list_files"}


def test_build_agent_tool_accepts_custom_definitions(tmp_path):
    writer = AgentDefinition(
        type="writer",
        system_prompt="you write files",
        max_turns=3,
        tools=("write_file",),
    )
    provider = ScriptedProvider([ModelReply(content="written")])
    tool = build_agent_tool(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        definitions={"writer": writer},
    )

    result = asyncio.run(
        tool.run({"task": "write", "agent_type": "writer"}, ToolContext(tmp_path))
    )

    assert result.output == "written"
    assert {item["name"] for item in provider.requests[0][1]} == {"write_file"}


def test_build_agent_tool_defaults_include_registered_agents(tmp_path):
    provider = ScriptedProvider([ModelReply(content="planned")])
    tool = build_agent_tool(provider=provider, tools=default_tools(), workspace=tmp_path)

    result = asyncio.run(
        tool.run({"task": "plan", "agent_type": "plan_agent"}, ToolContext(tmp_path))
    )

    assert result.output == "planned"
    assert {item["name"] for item in provider.requests[0][1]} == {
        "read_file",
        "list_files",
    }


def test_registry_can_be_passed_to_build_agent_tool(tmp_path):
    researcher = AgentDefinition(
        type="researcher",
        system_prompt="you research",
        max_turns=5,
        tools=("read_file",),
    )
    registry = default_agents()
    registry.register(researcher)
    provider = ScriptedProvider([ModelReply(content="found")])
    tool = build_agent_tool(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        definitions=registry,
    )

    result = asyncio.run(
        tool.run({"task": "research", "agent_type": "researcher"}, ToolContext(tmp_path))
    )

    assert result.output == "found"
    assert {item["name"] for item in provider.requests[0][1]} == {"read_file"}


def test_registry_registration_visible_without_rebuilding(tmp_path):
    researcher = AgentDefinition(
        type="researcher",
        system_prompt="you research",
        max_turns=5,
        tools=("read_file",),
    )
    registry = default_agents()
    provider = ScriptedProvider([ModelReply(content="found")])
    tool = build_agent_tool(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        definitions=registry,
    )

    # Register after the tool was built; the shared registry makes it live.
    registry.register(researcher)
    result = asyncio.run(
        tool.run({"task": "research", "agent_type": "researcher"}, ToolContext(tmp_path))
    )

    assert result.output == "found"


def test_registry_defaults_and_unknown_lookup():
    registry = default_agents()

    assert registry.names() == ("explore_agent", "plan_agent")
    assert registry.get("explore_agent") is explore_agent
    assert registry.get("plan_agent") is plan_agent
    assert registry.get("nope") is None


def test_agent_tool_parameters_expose_registered_agent_types(tmp_path):
    registry = default_agents()
    provider = ScriptedProvider([ModelReply(content="done")])
    tool = build_agent_tool(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        definitions=registry,
    )

    agent_type_schema = tool.parameters["properties"]["agent_type"]
    assert agent_type_schema["type"] == "string"
    assert agent_type_schema["enum"] == ["explore_agent", "plan_agent"]
    assert "searches and understands the codebase" in agent_type_schema["description"]
    assert "produces an implementation plan" in agent_type_schema["description"]


def test_agent_tool_parameters_follow_custom_registry(tmp_path):
    writer = AgentDefinition(
        type="writer",
        system_prompt="you write files",
        max_turns=3,
        tools=("write_file",),
        description="writes files from a plan",
    )
    registry = AgentRegistry()
    registry.register(writer)
    provider = ScriptedProvider([ModelReply(content="ok")])
    tool = build_agent_tool(
        provider=provider,
        tools=default_tools(),
        workspace=tmp_path,
        definitions=registry,
    )

    agent_type_schema = tool.parameters["properties"]["agent_type"]
    assert agent_type_schema["enum"] == ["writer"]
    assert "writes files from a plan" in agent_type_schema["description"]
