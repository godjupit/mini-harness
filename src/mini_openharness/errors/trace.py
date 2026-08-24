"""Trace persistence exceptions."""

from mini_openharness.errors.base import MiniOpenHarnessError


class TraceWriteError(MiniOpenHarnessError):
    """Raised when strict trace persistence fails."""
