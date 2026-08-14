
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from mini_openharness.engine import AgentLoop
from mini_openharness.provider import ModelProvider
from mini_openharness.session import SessionLog
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
    description: str = ""
    


class AgentManager:
    def __init__(
        self,
        provider,
        tools,
        workspace,
        parent_session: SessionLog | None = None,
    ):
        self.provider = provider
        self.tools = tools
        self.workspace = workspace
        self.parent_session = parent_session
        
    async def run_agent(
        self,
        definition: AgentDefinition,
        task_prompt: str
    ) -> str:
        agent_tools = self.tools.subset(definition.tools)

        subagent_session = None
        agent_id = f"agent-{uuid4().hex}"
        if self.parent_session is not None:
            root = (
                self.parent_session.root
                / self.parent_session.session_id
                / "subagents"
            )
            subagent_session = SessionLog(
                root,
                session_id=agent_id,
                metadata={
                    "kind": "subagent",
                    "agent_id": agent_id,
                    "agent_type": definition.type,
                    "parent_session_id": self.parent_session.session_id,
                    "task": task_prompt,
                },
            )
            self.parent_session.append_event(
                "subagent_start",
                {
                    "agent_id": agent_id,
                    "agent_type": definition.type,
                    "task": task_prompt,
                },
            )
        
        loop = AgentLoop(
            provider=self.provider,
            workspace=self.workspace,
            tools=agent_tools,
            system_prompt=definition.system_prompt,
            max_steps=definition.max_turns,
            session=subagent_session,
        )
        
        final_response = ""
        status = "completed"
        try:
            async for event in loop.run(task_prompt):
                if event.kind == "assistant":
                    final_response = event.message
                elif event.kind == "error":
                    raise RuntimeError(event.message)
        except Exception:
            status = "failed"
            raise
        finally:
            if self.parent_session is not None:
                self.parent_session.append_event(
                    "subagent_end",
                    {
                        "agent_id": agent_id,
                        "agent_type": definition.type,
                        "status": status,
                    },
                )
        
        return final_response
    
    


class AgentTool:
    name = "agent"
    description = """
        Delegate a task to a specialized subagent.

        Use explore_agent for codebase investigation, locating implementations,
        tracing architecture, and gathering information across multiple files.

        Use plan_agent after enough context is available when a task requires
        an implementation plan.

        Prefer delegation for substantial self-contained investigation or planning
        tasks that can be performed independently.
    """
    # Subagents run read-only AgentLoops (write tools default to ask/deny), so
    # the delegating tool itself is a read effect. Revisit if subagents are
    # ever granted write access.
    descriptor = ToolDescriptor(effect="compute", destructive=False)
    
    def __init__(
        self,
        manager: AgentManager,
        definitions: AgentRegistry | dict[str, AgentDefinition],
    ):
        self._manager = manager
        self._definitions = definitions
        self.parameters = self._build_parameters()

    def _iter_definitions(self) -> tuple[AgentDefinition, ...]:
        if isinstance(self._definitions, AgentRegistry):
            return self._definitions.all()
        return tuple(self._definitions.values())

    def _build_parameters(self) -> dict[str, Any]:
        agents = self._iter_definitions()
        purpose_parts = [
            f"{agent.type} {agent.description or agent.system_prompt}"
            for agent in agents
        ]
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "agent_type": {
                    "type": "string",
                    "enum": [agent.type for agent in agents],
                    "description": "Type of subagent to use. " + " ".join(purpose_parts),
                },
            },
            "required": ["task", "agent_type"],
            "additionalProperties": False,
        }

        
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
        try:
            result = await self._manager.run_agent(definition=definition, task_prompt=task)
        except Exception as exc:
            return ToolResult.fail(
                f"subagent {agent_type} failed: {type(exc).__name__}: {exc}",
                code="subagent_error",
                stage="execute",
                retryable=True,
                detail={"agent_type": agent_type, "exception_type": type(exc).__name__},
            )
        return ToolResult(result)


def build_agent_tool(
    provider: ModelProvider,
    tools: ToolRegistry,
    workspace: str | Path,
    definitions: AgentRegistry | dict[str, AgentDefinition] | None = None,
    parent_session: SessionLog | None = None,
) -> AgentTool:
    """Compose an AgentTool wired to the runtime provider/registry/workspace.

    When ``definitions`` is omitted, :func:`default_agents` (explore_agent and
    plan_agent) is used. The tool keeps a reference to the given registry, so
    later ``registry.register(...)`` calls are visible without rebuilding.
    """
    manager = AgentManager(
        provider=provider,
        tools=tools,
        workspace=workspace,
        parent_session=parent_session,
    )
    if definitions is None:
        definitions = default_agents()
    return AgentTool(manager=manager, definitions=definitions)
    

explore_agent = AgentDefinition(
    type="explore_agent",
    system_prompt="you are an explore agent that explores the codebase",
    max_turns=40,
    tools=("read_file", "list_files"),
    description="searches and understands the codebase",
)


plan_agent = AgentDefinition(
    type="plan_agent",
    system_prompt="you are a plan agent that plan how to write code next step",
    max_turns=40,
    tools=("read_file", "list_files"),
    description="analyzes a task and produces an implementation plan",
)    


    
class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, AgentDefinition] = {}
        
    def register(self, definition: AgentDefinition):
        if definition.type in self._agents:
            raise ValueError("same type agent")
        self._agents[definition.type] = definition

    def get(self, agent_type: str) -> AgentDefinition | None:
        return self._agents.get(agent_type)
        
    def all(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._agents.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._agents.keys())

    def __iter__(self):
        return iter(self._agents)
    
    
    
def default_agents() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(explore_agent)
    registry.register(plan_agent)
    return registry
        
