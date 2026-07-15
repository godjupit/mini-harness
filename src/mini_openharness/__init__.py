"""Mini OpenHarness: the smallest useful coding-agent runtime."""

from mini_openharness.engine import AgentLoop, AgentEvent
from mini_openharness.provider import (
    ModelProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
)
from mini_openharness.sandbox import DockerSandbox, SandboxedShellTool
from mini_openharness.skills import SkillCatalog
from mini_openharness.tools import ToolRegistry, default_tools

__all__ = [
    "AgentEvent",
    "AgentLoop",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "OpenAIResponsesProvider",
    "DockerSandbox",
    "SandboxedShellTool",
    "SkillCatalog",
    "ToolRegistry",
    "default_tools",
]

__version__ = "0.6.0"
