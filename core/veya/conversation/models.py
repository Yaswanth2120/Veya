"""Small, dependency-free data shapes shared across `conversation/`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class SessionContext:
    """The subset of Swift's `Session` fields relevant to answer
    generation, sent once via `session.start`. Every field defaults to
    `""` so older/minimal `session.start` calls (existing Section 6/7
    tests, the mock feed) keep working without sending any of this."""

    title: str = ""
    company: str = ""
    role_or_topic: str = ""
    description: str = ""
    notes: str = ""
    preferred_answer_style: str = ""
    preferred_programming_language: str = ""
    custom_instructions: str = ""
    session_type: str = ""


@dataclass(frozen=True)
class DetectedQuestionResult:
    text: str
    confidence: float


@dataclass(frozen=True)
class ParsedAnswer:
    """The result of parsing a completed LLM response into the overlay's
    shape. `caveat` is optional and folded into talking points on the
    Swift side (no schema change needed there) — kept separate here so
    `parse_answer_text` stays a pure, easily-testable function."""

    short_answer: str
    talking_points: List[str] = field(default_factory=list)
    caveat: str = ""
