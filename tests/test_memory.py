from __future__ import annotations

import asyncio

from mini_openharness.memory import MemoryStore, RememberTool
from mini_openharness.tools import ToolContext, ToolRegistry


def test_memory_persists_and_retrieves_english_and_chinese(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    store.add("The project uses PostgreSQL", ["database"])
    store.add("用户喜欢简洁的中文回答", ["preference"])

    reloaded = MemoryStore(tmp_path / "memory.json")
    assert reloaded.search("Which database does the project use?")[0].text.endswith("PostgreSQL")
    assert reloaded.search("请用简洁中文回答")[0].text.startswith("用户喜欢")


def test_remember_is_guarded_as_a_mutation(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    registry = ToolRegistry()
    registry.register(RememberTool(store))

    blocked = asyncio.run(
        registry.execute("remember", {"text": "a durable fact"}, ToolContext(tmp_path))
    )
    allowed = asyncio.run(
        registry.execute(
            "remember",
            {"text": "a durable fact"},
            ToolContext(tmp_path, allow_write=True),
        )
    )

    assert blocked.is_error
    assert not allowed.is_error
    assert store.all()[0].text == "a durable fact"
