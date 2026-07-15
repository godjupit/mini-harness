from __future__ import annotations

import asyncio

from mini_openharness.tools import (
    ResourceAccess,
    ResourceLockManager,
    ToolContext,
    default_tools,
)


def test_tool_timeout_becomes_recoverable_observation(tmp_path):
    from mini_openharness.tools import ToolRegistry, ToolResult

    class SlowTool:
        name = "slow"
        description = "slow"
        parameters = {"type": "object", "additionalProperties": False}
        read_only = True

        async def run(self, arguments, context):
            del arguments, context
            await asyncio.sleep(1)
            return ToolResult("late")

    registry = ToolRegistry()
    registry.register(SlowTool())
    result = asyncio.run(
        registry.execute(
            "slow", {}, ToolContext(tmp_path, tool_timeout_seconds=0.01)
        )
    )

    assert result.is_error
    assert result.metadata["timed_out"] is True
    assert "timed out" in result.output


def test_tree_read_lock_blocks_child_write_until_release():
    async def exercise():
        manager = ResourceLockManager()
        reader_acquired = asyncio.Event()
        release_reader = asyncio.Event()
        writer_acquired = asyncio.Event()

        async def reader():
            async with manager.acquire((ResourceAccess("fs:/workspace", "read", tree=True),)):
                reader_acquired.set()
                await release_reader.wait()

        async def writer():
            await reader_acquired.wait()
            async with manager.acquire((ResourceAccess("fs:/workspace/a.txt", "write"),)):
                writer_acquired.set()

        reader_task = asyncio.create_task(reader())
        writer_task = asyncio.create_task(writer())
        await reader_acquired.wait()
        await asyncio.sleep(0)
        blocked = not writer_acquired.is_set()
        release_reader.set()
        await asyncio.gather(reader_task, writer_task)
        return blocked, writer_acquired.is_set()

    assert asyncio.run(exercise()) == (True, True)


def execute(registry, name, arguments, context):
    return asyncio.run(registry.execute(name, arguments, context))


def test_read_cannot_escape_workspace(tmp_path):
    result = execute(default_tools(), "read_file", {"path": "../secret"}, ToolContext(tmp_path))
    assert result.is_error
    assert "escapes workspace" in result.output


def test_runtime_secrets_are_hidden_from_file_tools(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret", encoding="utf-8")
    oauth = tmp_path / ".mini-oh" / "oauth"
    oauth.mkdir(parents=True)
    (oauth / "remote.json").write_text('{"tokens":"secret"}', encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=", encoding="utf-8")
    tools = default_tools()

    env_result = execute(tools, "read_file", {"path": ".env"}, ToolContext(tmp_path))
    token_result = execute(
        tools,
        "read_file",
        {"path": ".mini-oh/oauth/remote.json"},
        ToolContext(tmp_path),
    )
    example_result = execute(
        tools, "read_file", {"path": ".env.example"}, ToolContext(tmp_path)
    )
    listing = execute(tools, "list_files", {}, ToolContext(tmp_path))

    assert env_result.is_error and token_result.is_error
    assert example_result.output == "OPENAI_API_KEY="
    assert ".env\n" not in listing.output + "\n"
    assert "remote.json" not in listing.output


def test_write_requires_explicit_permission(tmp_path):
    result = execute(
        default_tools(),
        "write_file",
        {"path": "answer.txt", "content": "42"},
        ToolContext(tmp_path, allow_write=False),
    )
    assert result.is_error
    assert not (tmp_path / "answer.txt").exists()


def test_write_stays_inside_workspace(tmp_path):
    result = execute(
        default_tools(),
        "write_file",
        {"path": "notes/answer.txt", "content": "42"},
        ToolContext(tmp_path, allow_write=True),
    )
    assert not result.is_error
    assert (tmp_path / "notes/answer.txt").read_text(encoding="utf-8") == "42"


def test_json_schema_is_enforced_before_tool_execution(tmp_path):
    result = execute(
        default_tools(),
        "write_file",
        {"path": "answer.txt", "unexpected": True},
        ToolContext(tmp_path, allow_write=True),
    )
    assert result.is_error
    assert "Invalid arguments" in result.output
    assert not (tmp_path / "answer.txt").exists()
