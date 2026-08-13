"""Typed LLM errors. Every failure mode a provider can hit becomes one of
these — callers never see a bare `requests`/`urllib`/OS-level exception,
mirroring `transcription/engine.py`'s `TranscriptionSetupError` pattern.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for all typed LLM errors."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LLMUnavailableError(LLMError):
    """The provider isn't usable at all right now — not configured,
    unreachable, or the configured model isn't present locally. Callers
    treat this as "answer intelligence unavailable," never as a reason to
    abandon real transcription (see docs/QUESTION_AND_ANSWER_INTELLIGENCE.md)."""


class LLMTimeoutError(LLMError):
    """A request to the provider did not complete within its timeout."""


class LLMProviderError(LLMError):
    """The provider was reachable but returned an error or malformed
    response for a specific request (as opposed to being unavailable
    entirely)."""
