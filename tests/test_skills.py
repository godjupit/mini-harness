from __future__ import annotations

import asyncio

from mini_openharness.skills import LoadSkillTool, SkillCatalog
from mini_openharness.tools import ToolContext


def test_skill_metadata_is_discovered_before_body_is_loaded(tmp_path):
    path = tmp_path / "review" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(
        "---\nname: review\ndescription: Review code carefully.\n---\n\nSECRET INSTRUCTIONS",
        encoding="utf-8",
    )
    catalog = SkillCatalog(tmp_path)

    assert "Review code carefully" in catalog.prompt()
    assert "SECRET INSTRUCTIONS" not in catalog.prompt()

    result = asyncio.run(LoadSkillTool(catalog).run({"name": "review"}, ToolContext(tmp_path)))
    assert "SECRET INSTRUCTIONS" in result.output


def test_unknown_skill_is_rejected_by_registry(tmp_path):
    catalog = SkillCatalog(tmp_path)
    try:
        catalog.read("../escape")
    except ValueError as exc:
        assert "Unknown skill" in str(exc)
    else:
        raise AssertionError("unknown skill should fail")
