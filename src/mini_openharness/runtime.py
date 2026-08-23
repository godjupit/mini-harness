"""Public runtime builder between declarative profiles and the agent kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mini_openharness.agent_profile import AgentProfile

if TYPE_CHECKING:
    from mini_openharness.engine import AgentLoop
    from mini_openharness.mcp.mcp import McpManager
    from mini_openharness.session import SessionLog
    from mini_openharness.trace import TraceWriter


RuntimeTuple = tuple[Any, Any, Any, Any]
RuntimeAssembler = Callable[..., Awaitable[RuntimeTuple]]


@dataclass
class AgentRuntime:
    """Owned runtime components for one CLI process or interactive session."""

    loop: AgentLoop
    tracer: TraceWriter | None
    mcp_manager: McpManager | None
    provider: Any

    async def close(self) -> None:
        if self.mcp_manager is not None:
            await self.mcp_manager.close()
        close = getattr(self.provider, "close", None)
        if close is not None:
            await close()


@dataclass(frozen=True)
class AgentRuntimeBuilder:
    """Build the shared Provider/MCP/Session/Trace/Permission runtime."""

    profile: AgentProfile | None = None

    async def build(
        self,
        options: Any,
        *,
        session_log: SessionLog | None,
        messages: list[Any] | None = None,
        trace_prompt: str,
        assembler: RuntimeAssembler,
    ) -> AgentRuntime:
        # The frontend supplies only its option-translation adapter. Runtime
        # ownership stays here and this module never depends on the CLI.
        loop, tracer, mcp_manager, provider = await assembler(
            options,
            session_log=session_log,
            messages=messages,
            trace_prompt=trace_prompt,
            profile=self.profile,
        )
        return AgentRuntime(loop, tracer, mcp_manager, provider)
