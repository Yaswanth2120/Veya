"""Structured protocol error codes and the exception used to signal them.

Every error the worker can report to Swift goes through `ProtocolError` so
the dispatcher has exactly one place that turns "something went wrong"
into a wire-format error response — no bare exceptions ever leak their
Python traceback text to Swift.
"""

from __future__ import annotations


class ErrorCode:
    """Wire-visible error codes. Keep these stable — Swift matches on them."""

    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
    INVALID_PARAMS = "INVALID_PARAMS"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    NOT_RUNNING = "NOT_RUNNING"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TRANSCRIPTION_UNAVAILABLE = "TRANSCRIPTION_UNAVAILABLE"


class ProtocolError(Exception):
    """Raised by dispatcher handlers (or protocol parsing) to signal a
    typed, safe-to-report error. `message` must never contain transcript
    text, answers, prompts, or any other sensitive payload — it is sent
    verbatim to Swift and may be logged.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
