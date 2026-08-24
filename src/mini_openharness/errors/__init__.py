"""Categorized exception types for mini-openharness."""

from mini_openharness.errors.base import MiniOpenHarnessError
from mini_openharness.errors.engine import MaxStepsExceeded, RunAlreadyActiveError
from mini_openharness.errors.provider import (
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderContextWindowError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderNetworkError,
    ProviderOutputTruncatedError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from mini_openharness.errors.sandbox import SandboxUnavailableError
from mini_openharness.errors.tools import FileChangedDuringEditError
from mini_openharness.errors.trace import TraceWriteError

__all__ = [
    "MaxStepsExceeded",
    "MiniOpenHarnessError",
    "FileChangedDuringEditError",
    "ProviderAuthenticationError",
    "ProviderCancelledError",
    "ProviderContextWindowError",
    "ProviderError",
    "ProviderInvalidResponseError",
    "ProviderNetworkError",
    "ProviderOutputTruncatedError",
    "ProviderRateLimitError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "RunAlreadyActiveError",
    "SandboxUnavailableError",
    "TraceWriteError",
]
