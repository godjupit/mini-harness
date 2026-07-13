"""Mini OpenHarness: the smallest useful coding-agent runtime."""

from mini_openharness.engine import AgentLoop, AgentEvent
from mini_openharness.memory import MemoryStore
from mini_openharness.provider import ModelProvider, OpenAICompatibleProvider
from mini_openharness.skills import SkillCatalog
from mini_openharness.tools import ToolRegistry, default_tools

__all__ = [
    "AgentEvent",
    "AgentLoop",
    "MemoryStore",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "SkillCatalog",
    "ToolRegistry",
    "default_tools",
]

__version__ = "0.2.0"
