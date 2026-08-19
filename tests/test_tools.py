from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from mini_openharness.permissions import (
    HumanApprovalHandler,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
    PermissionRules,
)
from mini_openharness.tools import (
    DEFAULT_READ_LINES,
    FULL_READ_MAX_LINES,
    ResourceAccess,
    ResourceLockManager,
    FileSnapshotStore,
    ToolContext,
    ToolDescriptor,
    ToolFailure,
    ToolRegistry,
    ToolResult,
    default_tools,
)


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


def test_tool_timeout_becomes_recoverable_observation(tmp_path):
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
            "slow",
            {},
            ToolContext(
                tmp_path,
                tool_timeout_seconds=0.01,
                permission_engine=allow_all_engine(tmp_path),
            ),
        )
    )

    assert result.is_error
    assert result.failure == ToolFailure(
        code="timeout",
        stage="execute",
        message="slow timed out after 0.01 seconds",
        retryable=True,
    )
    assert result.metadata["timed_out"] is True
    assert "timed out" in result.output


def test_tool_timeout_zero_means_unlimited(tmp_path):
    class SlowTool:
        name = "slow_unlimited"
        description = "slow"
        parameters = {"type": "object", "additionalProperties": False}
        read_only = True

        async def run(self, arguments, context):
            del arguments, context
            await asyncio.sleep(0.05)
            return ToolResult("late but done")

    registry = ToolRegistry()
    registry.register(SlowTool())
    result = asyncio.run(
        registry.execute(
            "slow_unlimited",
            {},
            ToolContext(
                tmp_path,
                tool_timeout_seconds=0,
                permission_engine=allow_all_engine(tmp_path),
            ),
        )
    )

    assert not result.is_error
    assert result.output == "late but done"


def test_descriptor_timeout_overrides_context_timeout(tmp_path):
    class SlowTool:
        name = "slow_desc"
        description = "slow"
        parameters = {"type": "object", "additionalProperties": False}
        descriptor = ToolDescriptor(effect="read", timeout_seconds=0.5)

        async def run(self, arguments, context):
            del arguments, context
            await asyncio.sleep(0.2)
            return ToolResult("done")

    registry = ToolRegistry()
    registry.register(SlowTool())

    result = asyncio.run(
        registry.execute(
            "slow_desc",
            {},
            ToolContext(tmp_path, tool_timeout_seconds=0.05),
        )
    )

    assert not result.is_error
    assert result.output == "done"


def test_descriptor_timeout_allows_zero_for_no_timeout():
    descriptor = ToolDescriptor(effect="read", timeout_seconds=0)
    assert descriptor.timeout_seconds == 0
    with pytest.raises(ValueError, match="timeout_seconds"):
        ToolDescriptor(effect="read", timeout_seconds=-1)


def test_descriptor_timeout_zero_disables_registry_timeout(tmp_path):
    class SlowTool:
        name = "slow_no_timeout"
        description = "slow"
        parameters = {"type": "object", "additionalProperties": False}
        descriptor = ToolDescriptor(effect="read", timeout_seconds=0)

        async def run(self, arguments, context):
            del arguments, context
            await asyncio.sleep(0.05)
            return ToolResult("done")

    registry = ToolRegistry()
    registry.register(SlowTool())
    result = asyncio.run(
        registry.execute(
            "slow_no_timeout",
            {},
            ToolContext(tmp_path, tool_timeout_seconds=0.01),
        )
    )

    assert not result.is_error
    assert result.output == "done"


def test_tool_failure_factory_enforces_error_invariant():
    result = ToolResult.fail(
        "bad input",
        code="invalid_input",
        stage="validate",
        metadata={"field": "value"},
    )

    assert result.is_error
    assert result.failure is not None
    assert result.failure.to_dict() == {
        "code": "invalid_input",
        "stage": "validate",
        "message": "bad input",
        "retryable": False,
    }
    assert result.metadata == {"field": "value"}


def test_registry_failures_have_stable_codes_and_stages(tmp_path):
    unknown = execute(ToolRegistry(), "missing", {}, ToolContext(tmp_path))
    invalid = execute(
        default_tools(),
        "write_file",
        {"path": "answer.txt", "unexpected": True},
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )
    denied = execute(
        default_tools(),
        "write_file",
        {"path": ".npmrc", "content": "no"},
        ToolContext(tmp_path),
    )

    assert (unknown.failure.code, unknown.failure.stage) == ("unknown_tool", "lookup")
    assert (invalid.failure.code, invalid.failure.stage) == ("invalid_input", "validate")
    assert (denied.failure.code, denied.failure.stage) == ("permission_denied", "authorize")


def test_explicit_descriptor_controls_source_effect_and_permission_path(tmp_path):
    class NamedPathTool:
        name = "put_document"
        description = "write a named document"
        parameters = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["filename", "content"],
            "additionalProperties": False,
        }
        descriptor = ToolDescriptor(
            source="extension",
            source_id="documents",
            effect="write",
            path_argument="filename",
        )

        async def run(self, arguments, context):
            path = context.workspace / arguments["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments["content"], encoding="utf-8")
            return ToolResult("written")

    registry = ToolRegistry()
    registry.register(NamedPathTool())
    engine = PermissionEngine(
        PermissionContext(
            mode=PermissionMode.DEFAULT,
            rules=PermissionRules(
                allow=[
                    PermissionRule(
                        PermissionBehavior.ALLOW,
                        tool="put_document",
                        pattern="docs/*",
                    )
                ]
            ),
            workspace=tmp_path,
        )
    )

    allowed = execute(
        registry,
        "put_document",
        {"filename": "docs/a.md", "content": "ok"},
        ToolContext(tmp_path, permission_engine=engine),
    )
    denied = execute(
        registry,
        "put_document",
        {"filename": ".npmrc", "content": "no"},
        ToolContext(tmp_path, permission_engine=engine),
    )

    assert not allowed.is_error
    assert denied.failure.code == "permission_denied"
    assert registry.source("put_document") == "extension"
    assert registry.descriptor("put_document").source_id == "documents"
    assert registry.permission_path("put_document", {"filename": "docs/a.md"}) == "docs/a.md"


def test_legacy_tool_gets_fail_closed_inferred_descriptor(tmp_path):
    class LegacyTool:
        name = "legacy"
        description = "legacy extension"
        parameters = {"type": "object", "additionalProperties": False}
        read_only = True

        async def run(self, arguments, context):
            del arguments, context
            return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(LegacyTool())

    result = execute(
        registry,
        "legacy",
        {},
        ToolContext(tmp_path, permission_engine=allow_all_engine(tmp_path)),
    )

    assert result.output == "ok"
    assert registry.descriptor("legacy").effect == "read"
    assert registry.descriptor_inferred("legacy") is True


def test_resource_resolver_failure_fails_closed(tmp_path):
    class BrokenResourcesTool:
        name = "broken_resources"
        description = "broken resource resolver"
        parameters = {"type": "object", "additionalProperties": False}
        descriptor = ToolDescriptor(effect="read")

        def resources(self, arguments, context):
            del arguments, context
            raise RuntimeError("resolver broke")

        async def run(self, arguments, context):
            del arguments, context
            return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(BrokenResourcesTool())

    assert registry.resources("broken_resources", {}, ToolContext(tmp_path)) == (
        ResourceAccess("*", "write", tree=True),
    )


def test_invalid_and_legacy_error_results_are_normalized(tmp_path):
    class InvalidResultTool:
        name = "invalid_result"
        description = "return the wrong result type"
        parameters = {"type": "object", "additionalProperties": False}
        descriptor = ToolDescriptor(effect="read")

        async def run(self, arguments, context):
            del arguments, context
            return "not a ToolResult"

    class LegacyErrorTool:
        name = "legacy_error"
        description = "return an old-style error"
        parameters = {"type": "object", "additionalProperties": False}
        descriptor = ToolDescriptor(effect="read")

        async def run(self, arguments, context):
            del arguments, context
            return ToolResult("legacy failure", is_error=True)

    registry = ToolRegistry()
    registry.register(InvalidResultTool())
    registry.register(LegacyErrorTool())

    invalid = execute(
        registry,
        "invalid_result",
        {},
        ToolContext(tmp_path, permission_engine=allow_all_engine(tmp_path)),
    )
    legacy = execute(
        registry,
        "legacy_error",
        {},
        ToolContext(tmp_path, permission_engine=allow_all_engine(tmp_path)),
    )

    assert (invalid.failure.code, invalid.failure.stage) == ("invalid_result", "postprocess")
    assert (legacy.failure.code, legacy.failure.stage) == ("tool_reported_error", "execute")


def test_all_builtin_tools_have_explicit_descriptors():
    registry = default_tools()

    assert registry.descriptor_inferred("read_file") is False
    assert registry.descriptor_inferred("list_dir") is False
    assert registry.descriptor_inferred("find_files") is False
    assert registry.descriptor_inferred("write_file") is False
    assert registry.descriptor_inferred("memory_write") is False
    assert registry.descriptor_inferred("memory_read") is False
    assert registry.descriptor("read_file").path_argument == "path"
    assert registry.descriptor("write_file").destructive is True
    assert registry.descriptor("memory_write").effect == "write"
    assert registry.descriptor("memory_write").destructive is False
    assert registry.descriptor("memory_read").effect == "read"


def test_memory_write_saves_topic_files_and_updates_index(tmp_path):
    tools = default_tools()
    cases = [
        ("user", "role", "Backend intern candidate learning Go", "user_role.md"),
        ("feedback", "testing", "Prefer official docs over tutorials", "feedback_testing.md"),
        ("project", "release", "Release freeze begins Aug 24", "project_release.md"),
    ]
    for memory_type, topic, content, filename in cases:
        result = execute(
            tools,
            "memory_write",
            {"type": memory_type, "topic": topic, "content": content},
            ToolContext(tmp_path, approval_handler=approve_all_handler()),
        )
        assert not result.is_error
        assert result.metadata["file"] == filename
        target = tmp_path / "memdir" / filename
        assert target.is_file()
        text = target.read_text(encoding="utf-8")
        assert content in text
        assert text.startswith(
            f'---\nname: "{memory_type.title()} {topic.title()}"\n'
            f'description: "{content}"\ntype: {memory_type}\n---\n'
        )
    index = (tmp_path / "memdir" / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [User Role](user_role.md) — Backend intern candidate learning Go" in index
    assert "- [Feedback Testing](feedback_testing.md) — Prefer official docs over tutorials" in index
    assert "- [Project Release](project_release.md) — Release freeze begins Aug 24" in index


def test_memory_write_appends_content_and_upserts_index_without_duplicates(tmp_path):
    tools = default_tools()
    context = ToolContext(tmp_path, approval_handler=approve_all_handler())
    for content in ("prefers concise replies", "prefers thorough explanations"):
        result = execute(
            tools,
            "memory_write",
            {"type": "user", "topic": "response_style", "content": content},
            context,
        )
        assert not result.is_error

    topic_text = (tmp_path / "memdir" / "user_response_style.md").read_text(encoding="utf-8")
    assert topic_text.count("- 2026-08-19:") == 2
    assert 'description: "prefers thorough explanations"' in topic_text
    assert "type: user" in topic_text.split("---", 2)[1]
    index = (tmp_path / "memdir" / "MEMORY.md").read_text(encoding="utf-8")
    assert index.count("user_response_style.md") == 1
    assert "prefers thorough explanations" in index


def test_memory_write_is_allowed_by_default_rules_and_appends(tmp_path):
    first = execute(
        default_tools(),
        "memory_write",
        {"type": "user", "topic": "response_style", "content": "prefers concise replies"},
        ToolContext(tmp_path),
    )
    second = execute(
        default_tools(),
        "memory_write",
        {"type": "user", "topic": "response_style", "content": "prefers concise replies"},
        ToolContext(tmp_path),
    )

    assert not first.is_error and not second.is_error
    text = (tmp_path / "memdir" / "user_response_style.md").read_text(encoding="utf-8")
    assert text.count("- 2026-08-19: prefers concise replies") == 2


def test_memory_write_rejects_unknown_type_empty_content_and_unsafe_topic(tmp_path):
    tools = default_tools()
    bad_type = execute(
        tools,
        "memory_write",
        {"type": "todo", "topic": "role", "content": "remember this"},
        ToolContext(tmp_path),
    )
    empty = execute(
        tools,
        "memory_write",
        {"type": "user", "topic": "role", "content": "   "},
        ToolContext(tmp_path),
    )
    traversal = execute(
        tools,
        "memory_write",
        {"type": "user", "topic": "../evil", "content": "remember this"},
        ToolContext(tmp_path),
    )

    assert bad_type.failure.code == "invalid_input"
    assert bad_type.failure.stage == "validate"
    assert empty.is_error
    assert traversal.is_error
    assert not (tmp_path / "memdir").exists()


def test_memory_read_loads_topic_file_on_demand(tmp_path):
    (tmp_path / "memdir").mkdir()
    (tmp_path / "memdir" / "permissions.md").write_text(
        "ASK -> Auto Review\nhard DENY cannot be overridden\n",
        encoding="utf-8",
    )

    result = execute(
        default_tools(),
        "memory_read",
        {"file": "permissions.md"},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "ASK -> Auto Review" in result.output
    assert "hard DENY cannot be overridden" in result.output


def test_memory_read_is_allowed_by_default_rules(tmp_path):
    (tmp_path / "memdir").mkdir()
    (tmp_path / "memdir" / "provider.md").write_text("streaming notes\n", encoding="utf-8")

    result = execute(
        default_tools(),
        "memory_read",
        {"file": "provider.md"},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "streaming notes" in result.output


def test_memory_read_rejects_missing_escapes_and_non_markdown(tmp_path):
    (tmp_path / "memdir").mkdir()
    tools = default_tools()
    missing = execute(tools, "memory_read", {"file": "nope.md"}, ToolContext(tmp_path))
    escape = execute(tools, "memory_read", {"file": "../engine.py"}, ToolContext(tmp_path))
    plain = execute(tools, "memory_read", {"file": "notes.txt"}, ToolContext(tmp_path))

    assert missing.is_error and "not found" in missing.output
    assert escape.is_error
    assert plain.is_error


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


def _write_lines(path, total):
    path.write_text(
        "".join(f"line {index}\n" for index in range(1, total + 1)),
        encoding="utf-8",
    )


def test_read_file_small_file_full_read(tmp_path):
    path = tmp_path / "small.txt"
    _write_lines(path, 5)

    result = execute(default_tools(), "read_file", {"path": "small.txt"}, ToolContext(tmp_path))

    assert not result.is_error
    assert "Lines: 1-5 of 5" in result.output
    assert "More: false" in result.output
    assert "line 1\n" in result.output
    assert "line 5\n" in result.output


def test_read_file_large_file_default_first_page(tmp_path):
    path = tmp_path / "big.txt"
    _write_lines(path, 1000)

    result = execute(default_tools(), "read_file", {"path": "big.txt"}, ToolContext(tmp_path))

    assert not result.is_error
    assert f"Lines: 1-{DEFAULT_READ_LINES} of 1000" in result.output
    assert "More: true" in result.output
    assert f"Next offset: {DEFAULT_READ_LINES}" in result.output
    assert "line 1\n" in result.output
    assert f"line {DEFAULT_READ_LINES + 1}\n" not in result.output


def test_read_file_full_read_boundary(tmp_path):
    path = tmp_path / "boundary.txt"
    _write_lines(path, FULL_READ_MAX_LINES)

    full = execute(
        default_tools(),
        "read_file",
        {"path": "boundary.txt"},
        ToolContext(tmp_path),
    )
    _write_lines(path, FULL_READ_MAX_LINES + 1)
    page = execute(
        default_tools(),
        "read_file",
        {"path": "boundary.txt"},
        ToolContext(tmp_path),
    )

    assert f"Lines: 1-{FULL_READ_MAX_LINES} of {FULL_READ_MAX_LINES}" in full.output
    assert "More: false" in full.output
    assert f"Lines: 1-{DEFAULT_READ_LINES} of {FULL_READ_MAX_LINES + 1}" in page.output
    assert "More: true" in page.output


def test_read_file_offset_limit_range(tmp_path):
    path = tmp_path / "big.txt"
    _write_lines(path, 1000)

    result = execute(
        default_tools(),
        "read_file",
        {"path": "big.txt", "offset": 300, "limit": 200},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "Lines: 301-500 of 1000" in result.output
    assert "More: true" in result.output
    assert "Next offset: 500" in result.output
    assert "line 301\n" in result.output
    assert "line 500\n" in result.output
    assert "line 501\n" not in result.output


def test_read_file_offset_only_uses_default_limit(tmp_path):
    path = tmp_path / "big.txt"
    _write_lines(path, 1000)

    result = execute(
        default_tools(),
        "read_file",
        {"path": "big.txt", "offset": 100},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert f"Lines: 101-{100 + DEFAULT_READ_LINES} of 1000" in result.output
    assert f"Next offset: {100 + DEFAULT_READ_LINES}" in result.output


def test_read_file_limit_only_starts_at_zero(tmp_path):
    path = tmp_path / "big.txt"
    _write_lines(path, 1000)

    result = execute(
        default_tools(),
        "read_file",
        {"path": "big.txt", "limit": 25},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "Lines: 1-25 of 1000" in result.output
    assert "Next offset: 25" in result.output


def test_read_file_limit_one(tmp_path):
    path = tmp_path / "big.txt"
    _write_lines(path, 1000)

    result = execute(
        default_tools(),
        "read_file",
        {"path": "big.txt", "offset": 5, "limit": 1},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "Lines: 6-6 of 1000" in result.output
    assert "line 6\n" in result.output
    assert "line 7\n" not in result.output


def test_read_file_last_page_more_false(tmp_path):
    path = tmp_path / "big.txt"
    _write_lines(path, 1000)

    result = execute(
        default_tools(),
        "read_file",
        {"path": "big.txt", "offset": 900, "limit": 200},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "Lines: 901-1000 of 1000" in result.output
    assert "More: false" in result.output
    assert "Next offset: 1000" in result.output
    assert "line 1000\n" in result.output


def test_read_file_offset_beyond_eof(tmp_path):
    path = tmp_path / "big.txt"
    _write_lines(path, 1000)

    result = execute(
        default_tools(),
        "read_file",
        {"path": "big.txt", "offset": 1500},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "Lines: (none) of 1000" in result.output
    assert "More: false" in result.output
    assert "beyond the end of the file" in result.output


def test_read_file_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    result = execute(default_tools(), "read_file", {"path": "empty.txt"}, ToolContext(tmp_path))

    assert not result.is_error
    assert "Lines: (none) of 0" in result.output
    assert "file is empty" in result.output


def test_read_file_invalid_offset_and_limit(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("x\n", encoding="utf-8")

    for bad in (
        {"path": "app.py", "offset": -1},
        {"path": "app.py", "limit": 0},
        {"path": "app.py", "limit": -3},
    ):
        result = execute(default_tools(), "read_file", bad, ToolContext(tmp_path))
        assert result.is_error
        assert result.failure.code == "invalid_input"


def test_read_file_same_range_not_repeated(tmp_path):
    path = tmp_path / "app.py"
    _write_lines(path, 100)
    context = ToolContext(tmp_path)
    tools = default_tools()

    first = execute(
        tools,
        "read_file",
        {"path": "app.py", "offset": 10, "limit": 20},
        context,
    )
    second = execute(
        tools,
        "read_file",
        {"path": "app.py", "offset": 10, "limit": 20},
        context,
    )

    assert "Lines: 11-30 of 100" in first.output
    assert second.output.startswith(
        "File unchanged. Lines 11-30 were already returned earlier."
    )
    assert "line 11" not in second.output


def test_read_file_different_range_is_normal_page(tmp_path):
    path = tmp_path / "app.py"
    _write_lines(path, 100)
    context = ToolContext(tmp_path)
    tools = default_tools()

    first = execute(
        tools,
        "read_file",
        {"path": "app.py", "offset": 0, "limit": 10},
        context,
    )
    second = execute(
        tools,
        "read_file",
        {"path": "app.py", "offset": 20, "limit": 10},
        context,
    )

    assert "Lines: 1-10 of 100" in first.output
    assert "already returned" not in second.output
    assert "Lines: 21-30 of 100" in second.output
    assert "line 21\n" in second.output


def test_read_file_cache_invalidates_after_file_change(tmp_path):
    path = tmp_path / "app.py"
    _write_lines(path, 10)
    context = ToolContext(tmp_path)
    tools = default_tools()

    first = execute(
        tools,
        "read_file",
        {"path": "app.py", "offset": 0, "limit": 10},
        context,
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace("line 1", "CHANGED 1"),
        encoding="utf-8",
    )
    second = execute(
        tools,
        "read_file",
        {"path": "app.py", "offset": 0, "limit": 10},
        context,
    )

    assert "already returned" not in first.output
    assert "already returned" not in second.output
    assert "Lines: 1-10 of 10" in second.output
    assert "CHANGED 1" in second.output


def test_grep_returns_file_line_matches(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text(
        "def helper():\n    pass\n\nclass PermissionEngine:\n    pass\n",
        encoding="utf-8",
    )
    (src / "b.py").write_text(
        "from a import PermissionEngine\n\nengine = PermissionEngine()\n",
        encoding="utf-8",
    )

    result = execute(
        default_tools(),
        "grep",
        {"pattern": "PermissionEngine", "path": "src"},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert "src/a.py:4: class PermissionEngine:" in result.output
    assert "src/b.py:1: from a import PermissionEngine" in result.output
    assert "src/b.py:3: engine = PermissionEngine()" in result.output


def test_grep_single_file_and_include_filter(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("TODO: fix me\n", encoding="utf-8")
    (src / "notes.md").write_text("TODO: docs\n", encoding="utf-8")

    single = execute(
        default_tools(),
        "grep",
        {"pattern": "TODO", "path": "src/a.py"},
        ToolContext(tmp_path),
    )
    filtered = execute(
        default_tools(),
        "grep",
        {"pattern": "TODO", "path": "src", "include": "*.md"},
        ToolContext(tmp_path),
    )

    assert single.output == "src/a.py:1: TODO: fix me"
    assert filtered.output == "src/notes.md:1: TODO: docs"


def test_grep_skips_hidden_and_binary_files(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("PermissionEngine=hidden\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01PermissionEngine\x00")

    result = execute(
        default_tools(),
        "grep",
        {"pattern": "PermissionEngine", "path": "."},
        ToolContext(tmp_path),
    )

    assert not result.is_error
    assert ".git" not in result.output
    assert "blob.bin" not in result.output


def test_grep_invalid_regex_is_an_error(tmp_path):
    result = execute(
        default_tools(),
        "grep",
        {"pattern": "([unclosed", "path": "."},
        ToolContext(tmp_path),
    )

    assert result.is_error
    assert "Invalid regex" in result.output


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
    listing = execute(tools, "list_dir", {}, ToolContext(tmp_path))

    assert env_result.is_error and token_result.is_error
    assert "Lines: 1-1 of 1" in example_result.output
    assert "OPENAI_API_KEY=" in example_result.output
    assert ".env\n" not in listing.output + "\n"
    assert "remote.json" not in listing.output


def test_sensitive_write_requires_explicit_permission(tmp_path):
    result = execute(
        default_tools(),
        "write_file",
        {"path": ".npmrc", "content": "registry=https://x"},
        ToolContext(tmp_path),
    )
    assert result.is_error
    assert not (tmp_path / ".npmrc").exists()


def test_write_stays_inside_workspace(tmp_path):
    result = execute(
        default_tools(),
        "write_file",
        {"path": "notes/answer.txt", "content": "42"},
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )
    assert not result.is_error
    assert (tmp_path / "notes/answer.txt").read_text(encoding="utf-8") == "42"


def test_read_then_edit_replaces_one_match_and_preserves_mode(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("timeout = 10\n", encoding="utf-8")
    path.chmod(0o640)
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, approval_handler=approve_all_handler(), file_snapshots=snapshots)
    tools = default_tools()

    read = execute(tools, "read_file", {"path": "app.py"}, context)
    edited = execute(
        tools,
        "edit_file",
        {"path": "app.py", "old_text": "10", "new_text": "30"},
        context,
    )

    assert not read.is_error and not edited.is_error
    assert path.read_text(encoding="utf-8") == "timeout = 30\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert edited.metadata["replacements"] == 1
    assert snapshots.get(path).sha256 == edited.metadata["sha256"]


def test_edit_requires_read_snapshot_or_expected_hash(tmp_path):
    (tmp_path / "app.py").write_text("old", encoding="utf-8")
    result = execute(
        default_tools(),
        "edit_file",
        {"path": "app.py", "old_text": "old", "new_text": "new"},
        ToolContext(tmp_path, approval_handler=approve_all_handler(), file_snapshots=FileSnapshotStore()),
    )

    assert result.failure.code == "file_not_read"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old"


def test_edit_detects_external_change_after_read(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("original", encoding="utf-8")
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, approval_handler=approve_all_handler(), file_snapshots=snapshots)
    tools = default_tools()
    execute(tools, "read_file", {"path": "app.py"}, context)
    path.write_text("user changed", encoding="utf-8")

    result = execute(
        tools,
        "edit_file",
        {"path": "app.py", "old_text": "original", "new_text": "agent changed"},
        context,
    )

    assert result.failure.code == "file_changed"
    assert path.read_text(encoding="utf-8") == "user changed"


def test_edit_rejects_missing_or_ambiguous_match_without_writing(tmp_path):
    path = tmp_path / "values.txt"
    path.write_text("same same", encoding="utf-8")
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, approval_handler=approve_all_handler(), file_snapshots=snapshots)
    tools = default_tools()
    execute(tools, "read_file", {"path": "values.txt"}, context)

    missing = execute(
        tools,
        "edit_file",
        {"path": "values.txt", "old_text": "absent", "new_text": "new"},
        context,
    )
    ambiguous = execute(
        tools,
        "edit_file",
        {"path": "values.txt", "old_text": "same", "new_text": "new"},
        context,
    )

    assert missing.failure.code == "match_not_found"
    assert ambiguous.failure.code == "ambiguous_match"
    assert "Re-read the relevant section" in missing.output
    assert "matches exactly once" in ambiguous.output
    assert path.read_text(encoding="utf-8") == "same same"


def test_edit_unique_match_preserves_unicode_and_line_endings(tmp_path):
    path = tmp_path / "values.txt"
    path.write_bytes("值=旧\r\n值=旧\r\n".encode())
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, approval_handler=approve_all_handler(), file_snapshots=snapshots)
    tools = default_tools()
    execute(tools, "read_file", {"path": "values.txt"}, context)

    result = execute(
        tools,
        "edit_file",
        {
            "path": "values.txt",
            "old_text": "旧\r\n值=旧",
            "new_text": "新\r\n值=新",
        },
        context,
    )

    assert not result.is_error
    assert result.metadata["replacements"] == 1
    assert path.read_bytes() == "值=新\r\n值=新\r\n".encode()


def test_edit_local_change_keeps_unrelated_content_intact(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("a = 1\nkeep me\nb = 2\n", encoding="utf-8")
    snapshots = FileSnapshotStore()
    context = ToolContext(
        tmp_path,
        approval_handler=approve_all_handler(),
        file_snapshots=snapshots,
    )
    tools = default_tools()
    execute(tools, "read_file", {"path": "app.py"}, context)

    result = execute(
        tools,
        "edit_file",
        {"path": "app.py", "old_text": "a = 1", "new_text": "a = 10"},
        context,
    )

    assert not result.is_error
    assert path.read_text(encoding="utf-8") == "a = 10\nkeep me\nb = 2\n"


def test_edit_large_file_only_changes_local_region(tmp_path):
    lines = [f"line {index:04d} - marker={index % 7}" for index in range(1, 2001)]
    path = tmp_path / "big.py"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    snapshots = FileSnapshotStore()
    context = ToolContext(
        tmp_path,
        approval_handler=approve_all_handler(),
        file_snapshots=snapshots,
    )
    tools = default_tools()
    execute(tools, "read_file", {"path": "big.py"}, context)

    result = execute(
        tools,
        "edit_file",
        {
            "path": "big.py",
            "old_text": "line 1000 - marker=6",
            "new_text": "line 1000 - marker=CHANGED",
        },
        context,
    )

    assert not result.is_error
    updated_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(updated_lines) == len(lines)
    assert updated_lines[999] == "line 1000 - marker=CHANGED"
    assert updated_lines[0] == lines[0]
    assert updated_lines[1999] == lines[1999]


def test_edit_atomic_replace_failure_keeps_original_and_cleans_temp(
    tmp_path, monkeypatch
):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, approval_handler=approve_all_handler(), file_snapshots=snapshots)
    tools = default_tools()
    execute(tools, "read_file", {"path": "app.py"}, context)

    def fail_replace(source, target):
        del source, target
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    result = execute(
        tools,
        "edit_file",
        {"path": "app.py", "old_text": "old", "new_text": "new"},
        context,
    )

    assert result.failure.code == "atomic_replace_failed"
    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".app.py.*.tmp")) == []


def test_edit_accepts_explicit_expected_hash_without_prior_read(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    expected = FileSnapshotStore.snapshot(path, path.read_bytes()).sha256

    result = execute(
        default_tools(),
        "edit_file",
        {
            "path": "app.py",
            "old_text": "old",
            "new_text": "new",
            "expected_sha256": expected,
        },
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )

    assert not result.is_error
    assert path.read_text(encoding="utf-8") == "new"


def test_edit_rejects_path_escape(tmp_path):
    outside = tmp_path.parent / "outside-edit.txt"
    outside.write_text("old", encoding="utf-8")

    result = execute(
        default_tools(),
        "edit_file",
        {
            "path": "../outside-edit.txt",
            "old_text": "old",
            "new_text": "new",
            "expected_sha256": FileSnapshotStore.snapshot(outside, outside.read_bytes()).sha256,
        },
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )

    assert result.failure.code == "permission_denied"
    assert outside.read_text(encoding="utf-8") == "old"


def test_edit_rejects_runtime_secret(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TOKEN=old", encoding="utf-8")
    expected = FileSnapshotStore.snapshot(path, path.read_bytes()).sha256

    result = execute(
        default_tools(),
        "edit_file",
        {
            "path": ".env",
            "old_text": "old",
            "new_text": "new",
            "expected_sha256": expected,
        },
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )

    assert result.failure.code == "protected_file"
    assert path.read_text(encoding="utf-8") == "TOKEN=old"


def test_edit_reports_missing_file(tmp_path):
    result = execute(
        default_tools(),
        "edit_file",
        {
            "path": "missing.txt",
            "old_text": "old",
            "new_text": "new",
            "expected_sha256": "0" * 64,
        },
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )

    assert result.failure.code == "file_not_found"


def test_edit_schema_rejects_empty_match_and_invalid_hash(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")

    empty = execute(
        default_tools(),
        "edit_file",
        {"path": "app.py", "old_text": "", "new_text": "new"},
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )
    invalid_hash = execute(
        default_tools(),
        "edit_file",
        {
            "path": "app.py",
            "old_text": "old",
            "new_text": "new",
            "expected_sha256": "short",
        },
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )

    assert empty.failure.code == "invalid_input"
    assert invalid_hash.failure.code == "invalid_input"
    assert path.read_text(encoding="utf-8") == "old"


def test_write_file_refreshes_snapshot_for_followup_edit(tmp_path):
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, approval_handler=approve_all_handler(), file_snapshots=snapshots)
    tools = default_tools()

    execute(
        tools,
        "write_file",
        {"path": "created.txt", "content": "old"},
        context,
    )
    edited = execute(
        tools,
        "edit_file",
        {"path": "created.txt", "old_text": "old", "new_text": "new"},
        context,
    )

    assert not edited.is_error
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "new"


def test_edit_file_declares_exact_write_resource(tmp_path):
    registry = default_tools()
    context = ToolContext(tmp_path)

    first = registry.resources("edit_file", {"path": "a.txt"}, context)
    second = registry.resources("edit_file", {"path": "b.txt"}, context)

    assert first == (ResourceAccess(f"fs:{tmp_path / 'a.txt'}", "write"),)
    assert second == (ResourceAccess(f"fs:{tmp_path / 'b.txt'}", "write"),)
    assert first != second


def test_json_schema_is_enforced_before_tool_execution(tmp_path):
    result = execute(
        default_tools(),
        "write_file",
        {"path": "answer.txt", "unexpected": True},
        ToolContext(tmp_path, approval_handler=approve_all_handler()),
    )
    assert result.is_error
    assert "Invalid arguments" in result.output
    assert not (tmp_path / "answer.txt").exists()
