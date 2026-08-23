from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from mini_openharness.agent_app import AgentApp
from mini_openharness.agent_profile import (
    AgentProfile,
    OutputProtocol,
    PermissionPolicy,
)
from mini_openharness.tools import ToolRegistry
from mini_openharness.runtime import AgentRuntimeBuilder


def empty_tools() -> ToolRegistry:
    return ToolRegistry()


def load_app(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profile_contains_role_tools_permissions_steps_and_output_contract():
    protocol = OutputProtocol(
        name="result",
        media_type="application/json",
        instructions="Return one result object.",
        json_schema={"type": "object", "required": ["result"]},
    )
    profile = AgentProfile(
        name="demo",
        system_prompt="Demo role",
        tool_factory=empty_tools,
        mcp_config="mcp.json",
        permission_policy=PermissionPolicy.HUMAN_APPROVAL,
        permission_config="permissions.json",
        max_steps=5,
        output_protocol=protocol,
    )

    assert profile.build_tools().items() == ()
    assert profile.permission_policy == PermissionPolicy.HUMAN_APPROVAL
    assert profile.max_steps == 5
    assert "JSON Schema" in profile.output_protocol.prompt_fragment()


def test_output_protocol_rejects_schema_for_non_json_media_type():
    with pytest.raises(ValueError, match="application/json"):
        OutputProtocol(media_type="text/plain", json_schema={"type": "object"})


@pytest.mark.parametrize("field", ["skills_dir", "memory_dir"])
def test_profile_rejects_empty_resource_directory(field):
    with pytest.raises(ValueError, match=field):
        AgentProfile("demo", "Demo role", **{field: "  "})


def test_agent_app_delegates_to_shared_cli(monkeypatch):
    captured = {}

    def fake_main(argv, *, profile):
        captured["argv"] = argv
        captured["profile"] = profile
        return 17

    monkeypatch.setattr("mini_openharness.cli.main", fake_main)
    profile = AgentProfile("demo", "Demo role", empty_tools)

    assert AgentApp(profile).run(["hello"]) == 17
    assert captured == {"argv": ["hello"], "profile": profile}


def test_runtime_builder_uses_frontend_adapter_without_importing_business_apps():
    profile = AgentProfile("demo", "Demo role", empty_tools)
    captured = {}

    async def assemble(options, **kwargs):
        captured["options"] = options
        captured.update(kwargs)
        return "loop", "trace", "mcp", "provider"

    runtime = asyncio.run(
        AgentRuntimeBuilder(profile).build(
            {"model": "demo"},
            session_log=None,
            trace_prompt="hello",
            assembler=assemble,
        )
    )

    assert runtime.loop == "loop"
    assert captured["profile"] is profile
    assert captured["trace_prompt"] == "hello"


def test_coding_app_is_an_explicit_profile():
    root = Path(__file__).resolve().parents[1]
    coding = load_app(root / "apps" / "coding_agent.py", "test_coding_agent_app")

    assert coding.APP.profile.name == "coding"
    assert coding.APP.profile.enable_sandbox_shell is True
    assert coding.APP.profile.enable_subagents is True
    assert coding.APP.profile.permission_policy == PermissionPolicy.AUTO_REVIEW
    assert coding.APP.profile.skills_dir.endswith("agent_assets/coding/skills")
    assert coding.APP.profile.memory_dir.endswith("agent_assets/coding/memory")
