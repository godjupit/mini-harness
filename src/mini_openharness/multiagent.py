
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_openharness.engine import AgentLoop
from mini_openharness.provider import ModelProvider
from mini_openharness.tools import (
    ToolContext,
    ToolDescriptor,
    ToolRegistry,
    ToolResult,
)



@dataclass(frozen=True)
class AgentDefinition:
    type: str  
    system_prompt: str
    max_turns: int
    tools: tuple[str, ...]
    


class AgentManager:
    def __init__(self, provider, tools, workspace):
        self.provider = provider
        self.tools = tools
        self.workspace = workspace
        
    async def run_agent(
        self,
        definition: AgentDefinition,
        task_prompt: str
    ) -> str:
        agent_tools = self.tools.subset(definition.tools)
        
        loop = AgentLoop(
            provider=self.provider,
            workspace=self.workspace,
            tools=agent_tools,
            system_prompt=definition.system_prompt,
            max_steps=definition.max_turns
        )
        
        final_response = ""
        
        async for event in loop.run(task_prompt):
            if event.kind == "assistant":
                final_response = event.message
            elif event.kind == "error":
                raise RuntimeError(event.message)
        
        return final_response
    
    


class AgentTool:
    name = "agent"
    description = "use subagent to solve problems"
    # Subagents run read-only AgentLoops (write tools default to ask/deny), so
    # the delegating tool itself is a read effect. Revisit if subagents are
    # ever granted write access.
    descriptor = ToolDescriptor(effect="read", destructive=False)
    
    parameters = {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "agent_type": {"type": "string"}
            },
            "required": ["task", "agent_type"],
            "additionalProperties": False,
    }
    
    def __init__(
        self,
        manager: AgentManager,
        definitions: AgentRegistry | dict[str, AgentDefinition],
    ):
        self._manager = manager
        self._definitions = definitions

        
    async def run(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        agent_type = arguments["agent_type"]
        task = arguments["task"]
        definition = self._definitions.get(agent_type)
        if definition is None:
            return ToolResult.fail(
                f"Unknown agent_type: {agent_type}; "
                f"available: {', '.join(sorted(self._definitions))}",
                code="unknown_agent",
                stage="execute",
            )
        result = await self._manager.run_agent(definition=definition, task_prompt=task)
        return ToolResult(result)


def build_agent_tool(
    provider: ModelProvider,
    tools: ToolRegistry,
    workspace: str | Path,
    definitions: AgentRegistry | dict[str, AgentDefinition] | None = None,
) -> AgentTool:
    """Compose an AgentTool wired to the runtime provider/registry/workspace.

    When ``definitions`` is omitted, :func:`default_agents` (explore_agent and
    plan_agent) is used. The tool keeps a reference to the given registry, so
    later ``registry.register(...)`` calls are visible without rebuilding.
    """
    manager = AgentManager(provider=provider, tools=tools, workspace=workspace)
    if definitions is None:
        definitions = default_agents()
    return AgentTool(manager=manager, definitions=definitions)
    

explore_agent = AgentDefinition(
    type="explore_agent",
    system_prompt="you are an explore agent that explores the codebase",
    max_turns=40,
    tools=("read_file", "list_files")
)


plan_agent = AgentDefinition(
    type="plan_agent",
    system_prompt="you are a plan agent that plan how to write code next step",
    max_turns=40,
    tools=("read_file", "list_files"),
)    


    
class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, AgentDefinition] = {}
        
    def register(self, definition: AgentDefinition):
        if definition.type in self._agents:
            raise ValueError(f"same type agent")
        self._agents[definition.type] = definition

    def get(self, agent_type: str) -> AgentDefinition | None:
        return self._agents.get(agent_type)
        
    def names(self) -> tuple[str, ...]:
        return tuple(self._agents.keys())

    def __iter__(self):
        return iter(self._agents)
    
    
    
def default_agents() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(explore_agent)
    registry.register(plan_agent)
    return registry
        
