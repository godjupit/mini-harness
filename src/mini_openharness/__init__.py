"""Mini OpenHarness: the smallest useful coding-agent runtime."""

from mini_openharness.agent_app import AgentApp
from mini_openharness.agent_profile import (
    MARKDOWN_OUTPUT,
    AgentProfile,
    OutputProtocol,
    PermissionPolicy,
)
from mini_openharness.engine import AgentEvent, AgentLoop, RunAlreadyActiveError
from mini_openharness.multiagent import (
    AgentDefinition,
    AgentManager,
    AgentRegistry,
    AgentTool,
    build_agent_tool,
    default_agents,
)
from mini_openharness.provider import (
    ModelProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
)
from mini_openharness.runtime import AgentRuntime, AgentRuntimeBuilder
from mini_openharness.sandbox import BwrapShell, SandboxedShellTool
from mini_openharness.skills import SkillCatalog
from mini_openharness.tools import (
    EditFileTool,
    FileSnapshot,
    FileSnapshotStore,
    ToolDescriptor,
    ToolFailure,
    ToolRegistry,
    ToolResult,
    default_tools,
)
from mini_openharness.trace import (
    LocalJsonlTraceSink,
    MemoryTraceSink,
    TraceSink,
    TraceWriteError,
    TraceWriter,
)

__all__ = [
    "AgentEvent",
    "AgentApp",
    "AgentProfile",
    "AgentRuntime",
    "AgentRuntimeBuilder",
    "MARKDOWN_OUTPUT",
    "OutputProtocol",
    "PermissionPolicy",
    "AgentLoop",
    "RunAlreadyActiveError",
    "AgentDefinition",
    "AgentManager",
    "AgentRegistry",
    "AgentTool",
    "build_agent_tool",
    "default_agents",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "OpenAIResponsesProvider",
    "BwrapShell",
    "SandboxedShellTool",
    "SkillCatalog",
    "EditFileTool",
    "FileSnapshot",
    "FileSnapshotStore",
    "ToolDescriptor",
    "ToolFailure",
    "ToolRegistry",
    "ToolResult",
    "default_tools",
    "TraceSink",
    "LocalJsonlTraceSink",
    "MemoryTraceSink",
    "TraceWriteError",
    "TraceWriter",
]

__version__ = "0.6.0"
