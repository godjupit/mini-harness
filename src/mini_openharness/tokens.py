"""Pluggable token counting: heuristic fallback plus optional real tokenizer."""

from __future__ import annotations

from typing import Protocol

DEFAULT_CHARS_PER_TOKEN = 4


class TokenCounter(Protocol):
    def count_tokens(self, text: str) -> int:
        """Count tokens in plain text."""


class HeuristicCounter:
    """Chars-per-token estimate. Dependency-free and always available."""

    def __init__(self, chars_per_token: int = DEFAULT_CHARS_PER_TOKEN) -> None:
        self.chars_per_token = max(1, chars_per_token)

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // self.chars_per_token)


class TiktokenCounter:
    """Real tokenizer backed by tiktoken; imported lazily so it stays optional.

    Falls back to the heuristic when the tokenizer cannot be loaded (for example
    offline on first use), so a counting failure never breaks the run.
    """

    _cache: dict[str, object] = {}

    def __init__(self, model: str | None = None) -> None:
        self.model = model or "cl100k_base"

    def _encoding(self):
        if self.model not in self._cache:
            import tiktoken  # noqa: PLC0415

            try:
                encoding = tiktoken.encoding_for_model(self.model)
            except KeyError:
                encoding = tiktoken.get_encoding("cl100k_base")
            self._cache[self.model] = encoding
        return self._cache[self.model]

    def count_tokens(self, text: str) -> int:
        try:
            return len(self._encoding().encode(text))
        except Exception:
            return HeuristicCounter().count_tokens(text)


def build_token_counter(model: str | None = None) -> TokenCounter:
    """Return a real tiktoken counter when usable, otherwise the heuristic."""
    try:
        import tiktoken  # noqa: PLC0415

        if model:
            try:
                tiktoken.encoding_for_model(model)
            except KeyError:
                # Unknown model name; TiktokenCounter falls back to cl100k_base.
                pass
        return TiktokenCounter(model=model)
    except Exception:
        return HeuristicCounter()
