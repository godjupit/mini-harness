"""Model provider exceptions."""

from mini_openharness.errors.base import MiniOpenHarnessError


class ProviderError(MiniOpenHarnessError):
    """Base class for normalized provider failures."""


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderNetworkError(ProviderError):
    pass


class ProviderServerError(ProviderError):
    pass


class ProviderContextWindowError(ProviderError):
    """The request input exceeded the provider context window."""


class ProviderInvalidResponseError(ProviderError):
    pass


class ProviderOutputTruncatedError(ProviderInvalidResponseError):
    """The provider stopped before producing a complete assistant turn."""


class ProviderCancelledError(ProviderError):
    pass
