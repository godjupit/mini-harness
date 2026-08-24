"""Agent engine exceptions."""

from mini_openharness.errors.base import MiniOpenHarnessError


class MaxStepsExceeded(MiniOpenHarnessError):
    """Raised when an agent run does not finish within its step limit."""


class RunAlreadyActiveError(MiniOpenHarnessError):
    """Raised when an AgentLoop is asked to start a concurrent run."""
