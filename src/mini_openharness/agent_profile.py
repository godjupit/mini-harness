"""Declarative agent variants built on the shared AgentLoop runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, Mapping

from mini_openharness.tools import ToolRegistry, default_tools


PromptMode = Literal["replace", "append"]
ToolFactory = Callable[[], ToolRegistry]


class PermissionPolicy(str, Enum):
    """How ASK permission decisions are resolved for this agent type."""

    INHERIT = "inherit"
    AUTO_REVIEW = "auto_review"
    HUMAN_APPROVAL = "human_approval"


@dataclass(frozen=True)
class OutputProtocol:
    """Model-facing response contract selected by an AgentProfile."""

    name: str = "markdown"
    media_type: str = "text/markdown"
    instructions: str = "Return the final answer as clear Markdown."
    json_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.media_type.strip():
            raise ValueError("output protocol name and media_type must not be empty")
        if self.json_schema is not None and self.media_type != "application/json":
            raise ValueError("json_schema requires application/json media_type")

    def prompt_fragment(self) -> str:
        parts = [f"OUTPUT PROTOCOL: {self.name} ({self.media_type})."]
        if self.instructions.strip():
            parts.append(self.instructions.strip())
        if self.json_schema is not None:
            import json

            parts.append(
                "The final response must validate against this JSON Schema: "
                + json.dumps(self.json_schema, ensure_ascii=False, separators=(",", ":"))
            )
        return "\n".join(parts)


MARKDOWN_OUTPUT = OutputProtocol()


@dataclass(frozen=True)
class AgentProfile:
    """The role-specific parts of an agent runtime.

    AgentLoop remains the reusable execution kernel. A profile selects the
    model-facing prompt and initial local tools, while the CLI continues to own
    provider, tracing, sessions, permissions, hooks, compaction, and MCP
    connection lifecycles.
    """

    name: str
    system_prompt: str
    tool_factory: ToolFactory = default_tools
    prompt_mode: PromptMode = "replace"
    mcp_config: str | None = None
    permission_policy: PermissionPolicy = PermissionPolicy.INHERIT
    permission_config: str | None = None
    max_steps: int | None = None
    output_protocol: OutputProtocol = MARKDOWN_OUTPUT
    enable_sandbox_shell: bool = False
    enable_skills: bool = False
    enable_subagents: bool = False
    enable_memory_prompt: bool = False
    skills_dir: str | None = None
    memory_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agent profile name must not be empty")
        if not self.system_prompt.strip():
            raise ValueError("agent profile system_prompt must not be empty")
        if self.prompt_mode not in {"replace", "append"}:
            raise ValueError("agent profile prompt_mode must be 'replace' or 'append'")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("agent profile max_steps must be at least 1")
        if self.skills_dir is not None and not self.skills_dir.strip():
            raise ValueError("agent profile skills_dir must not be empty")
        if self.memory_dir is not None and not self.memory_dir.strip():
            raise ValueError("agent profile memory_dir must not be empty")

    def build_tools(self) -> ToolRegistry:
        registry = self.tool_factory()
        if not isinstance(registry, ToolRegistry):
            raise TypeError("agent profile tool_factory must return ToolRegistry")
        return registry
