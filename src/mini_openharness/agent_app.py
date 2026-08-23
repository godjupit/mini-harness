"""Application entrypoint abstraction for concrete agent products."""

from __future__ import annotations

from dataclasses import dataclass

from mini_openharness.agent_profile import AgentProfile


@dataclass(frozen=True)
class AgentApp:
    """Bind one declarative profile to the shared CLI/runtime lifecycle."""

    profile: AgentProfile

    def run(self, argv: list[str] | None = None) -> int:
        # Imported lazily so profile declarations do not initialize CLI state.
        from mini_openharness.cli import main

        return main(argv, profile=self.profile)
