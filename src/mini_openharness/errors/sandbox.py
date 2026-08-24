"""Sandbox runtime exceptions."""

from mini_openharness.errors.base import MiniOpenHarnessError


class SandboxUnavailableError(MiniOpenHarnessError):
    """Raised when the configured sandbox runtime is unavailable."""
