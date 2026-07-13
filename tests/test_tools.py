from __future__ import annotations

import asyncio

from mini_openharness.tools import ToolContext, default_tools


def execute(registry, name, arguments, context):
    return asyncio.run(registry.execute(name, arguments, context))


def test_read_cannot_escape_workspace(tmp_path):
    result = execute(default_tools(), "read_file", {"path": "../secret"}, ToolContext(tmp_path))
    assert result.is_error
    assert "escapes workspace" in result.output


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
