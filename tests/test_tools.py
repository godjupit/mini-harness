from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from mini_openharness.permissions import (
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
    PermissionRules,
)
from mini_openharness.tools import (
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


def bypass_engine(workspace: Path) -> PermissionEngine:
    return PermissionEngine(
        PermissionContext(
            mode=PermissionMode.BYPASS,
            rules=PermissionRules(),
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
                permission_engine=bypass_engine(tmp_path),
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
        ToolContext(tmp_path, allow_write=True),
    )
    denied = execute(
        default_tools(),
        "write_file",
        {"path": "answer.txt", "content": "no"},
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
        {"filename": "src/a.py", "content": "no"},
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
        ToolContext(tmp_path, permission_engine=bypass_engine(tmp_path)),
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
        ToolContext(tmp_path, permission_engine=bypass_engine(tmp_path)),
    )
    legacy = execute(
        registry,
        "legacy_error",
        {},
        ToolContext(tmp_path, permission_engine=bypass_engine(tmp_path)),
    )

    assert (invalid.failure.code, invalid.failure.stage) == ("invalid_result", "postprocess")
    assert (legacy.failure.code, legacy.failure.stage) == ("tool_reported_error", "execute")


def test_all_builtin_tools_have_explicit_descriptors():
    registry = default_tools()

    assert registry.descriptor_inferred("read_file") is False
    assert registry.descriptor_inferred("list_files") is False
    assert registry.descriptor_inferred("write_file") is False
    assert registry.descriptor("read_file").path_argument == "path"
    assert registry.descriptor("write_file").destructive is True


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


def test_read_then_edit_replaces_one_match_and_preserves_mode(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("timeout = 10\n", encoding="utf-8")
    path.chmod(0o640)
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, allow_write=True, file_snapshots=snapshots)
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
        ToolContext(tmp_path, allow_write=True, file_snapshots=FileSnapshotStore()),
    )

    assert result.failure.code == "file_not_read"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old"


def test_edit_detects_external_change_after_read(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("original", encoding="utf-8")
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, allow_write=True, file_snapshots=snapshots)
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
    context = ToolContext(tmp_path, allow_write=True, file_snapshots=snapshots)
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
    assert path.read_text(encoding="utf-8") == "same same"


def test_edit_replace_all_preserves_unicode_and_line_endings(tmp_path):
    path = tmp_path / "values.txt"
    path.write_bytes("值=旧\r\n值=旧\r\n".encode())
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, allow_write=True, file_snapshots=snapshots)
    tools = default_tools()
    execute(tools, "read_file", {"path": "values.txt"}, context)

    result = execute(
        tools,
        "edit_file",
        {
            "path": "values.txt",
            "old_text": "旧",
            "new_text": "新",
            "replace_all": True,
        },
        context,
    )

    assert not result.is_error
    assert result.metadata["replacements"] == 2
    assert path.read_bytes() == "值=新\r\n值=新\r\n".encode()


def test_edit_atomic_replace_failure_keeps_original_and_cleans_temp(
    tmp_path, monkeypatch
):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, allow_write=True, file_snapshots=snapshots)
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
        ToolContext(tmp_path, allow_write=True),
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
        ToolContext(tmp_path, allow_write=True),
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
        ToolContext(tmp_path, allow_write=True),
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
        ToolContext(tmp_path, allow_write=True),
    )

    assert result.failure.code == "file_not_found"


def test_edit_schema_rejects_empty_match_and_invalid_hash(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")

    empty = execute(
        default_tools(),
        "edit_file",
        {"path": "app.py", "old_text": "", "new_text": "new"},
        ToolContext(tmp_path, allow_write=True),
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
        ToolContext(tmp_path, allow_write=True),
    )

    assert empty.failure.code == "invalid_input"
    assert invalid_hash.failure.code == "invalid_input"
    assert path.read_text(encoding="utf-8") == "old"


def test_write_file_refreshes_snapshot_for_followup_edit(tmp_path):
    snapshots = FileSnapshotStore()
    context = ToolContext(tmp_path, allow_write=True, file_snapshots=snapshots)
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
        ToolContext(tmp_path, allow_write=True),
    )
    assert result.is_error
    assert "Invalid arguments" in result.output
    assert not (tmp_path / "answer.txt").exists()
