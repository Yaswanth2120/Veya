"""The `LLMProvider` abstraction every concrete provider implements.
`answer_generation.py` depends only on this — never on `ollama_provider`
directly — so tests can substitute a fake and a later provider can be
added without touching question detection, orchestration, or Swift IPC.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol


class LLMProvider(Protocol):
    async def check_availability(self) -> None:
        """Raises `LLMUnavailableError` if this provider cannot currently
        serve requests (not configured, unreachable, model missing).
        Returns normally if it can. Called once per Live Session, at
        `transcription.start` time — never logs prompt/response content."""
        ...

    def generate_stream(self, prompt: str, *, timeout: float) -> AsyncIterator[str]:
        """Streams incremental text deltas for `prompt`. Raises
        `LLMTimeoutError` if `timeout` elapses before the stream
        completes, or `LLMProviderError` for any other request-level
        failure. Never logs `prompt` or any yielded delta."""
        ...
