"""Discover skill metadata and reveal full instructions only on demand."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_openharness.tools import ResourceAccess, ToolContext, ToolDescriptor, ToolResult


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


class SkillCatalog:
    """A progressive-disclosure catalog for ``<root>/<name>/SKILL.md`` files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._skills = self._discover()

    def _discover(self) -> dict[str, Skill]:
        if not self.root.is_dir():
            return {}
        skills: dict[str, Skill] = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            content = path.read_text(encoding="utf-8")
            metadata = _frontmatter(content)
            name = metadata.get("name", path.parent.name).strip()
            description = metadata.get("description", f"Skill: {name}").strip()
            if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                skills[name] = Skill(name, description, path.resolve())
        return skills

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def read(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            raise ValueError(f"Unknown skill: {name}")
        return skill.path.read_text(encoding="utf-8")

    def path(self, name: str) -> Path | None:
        skill = self._skills.get(name)
        return skill.path if skill else None

    def prompt(self) -> str:
        if not self._skills:
            return ""
        lines = [
            "Available skills are listed below. Call load_skill before following one; "
            "the catalog contains metadata only."
        ]
        lines.extend(f"- {skill.name}: {skill.description}" for skill in self.list())
        return "\n".join(lines)


class LoadSkillTool:
    name = "load_skill"
    description = "Load the full instructions for one available skill."
    read_only = True
    descriptor = ToolDescriptor(source="skill", effect="read")
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        return ToolResult(self.catalog.read(str(arguments["name"])))

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        del context
        path = self.catalog.path(str(arguments["name"]))
        if path is None:
            return (ResourceAccess(f"fs:{self.catalog.root}", "read", tree=True),)
        return (ResourceAccess(f"fs:{path}", "read"),)


def _frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---", 4)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in content[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            result[key.strip()] = value.strip().strip("'\"")
    return result
