"""Internal tool execution exceptions."""

from mini_openharness.errors.base import MiniOpenHarnessError


class FileChangedDuringEditError(MiniOpenHarnessError):
    """Raised when a file changes during an atomic edit operation."""
