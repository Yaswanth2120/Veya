"""Deterministic, non-LLM question detection for V1 — a plain
punctuation/interrogative-form heuristic scorer, not a model call. Only
ever fed `transcript.final` text (see `orchestrator.py`); partial
transcripts are never analyzed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .models import DetectedQuestionResult

_INTERROGATIVE_STARTS = {
    "why", "how", "what", "when", "who", "whom", "whose", "where", "which",
    "is", "are", "was", "were", "do", "does", "did",
    "can", "could", "would", "should", "will", "shall", "may", "might",
}
_LEADING_FILLER_WORDS = {"so", "um", "uh", "well", "okay", "ok", "and", "but"}

# Spoken interview prompts are often statements rather than grammatical
# questions: "Tell me about yourself", "Walk me through your resume", or
# "Q1, explain the deployment policy". Whisper also frequently strips the
# question mark, so treating only written interrogatives as questions makes
# real microphone use fail in exactly the situations this product is for.
# Keep this deliberately narrow: these are request-for-an-answer phrases,
# not every imperative sentence in ordinary conversation.
_SPOKEN_PROMPT_RE = re.compile(
    r"^(?:(?:q|question)\s*\d+\s*[,.:;-]?\s*)?"
    r"(?:please\s+)?"
    r"(?:tell\s+me(?:\s+about)?|walk\s+me\s+through|talk\s+me\s+through|"
    r"explain|describe|give\s+me|help\s+me\s+understand|outline|compare|share)\b",
    re.IGNORECASE,
)

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# An interrogative word appearing *anywhere* in the turn, not just as the
# (post-filler-stripped) leading word — e.g. "the caching layer, how does
# that scale" or "you mentioned retries, what's the backoff policy". A
# genuinely weaker signal than `interrogative_start_score` (which already
# covers the leading-word case on its own): this exists specifically so
# realistic mid-sentence-interrogative turns land in the ambiguous
# [`ambiguous_low_bound`, `confidence_threshold`) band and actually reach
# the semantic classifier, rather than the additive scoring only ever
# landing on the handful of discrete values the "big" signals below
# produce (0, 0.2, 0.6, 0.65, 0.75, or their sums) — none of which
# naturally fall in that band.
_INTERROGATIVE_WORD_RE = re.compile(
    r"\b(?:why|how|what|when|who|whom|whose|where|which)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class QuestionDetectionConfig:
    confidence_threshold: float = 0.6
    ends_with_question_mark_score: float = 0.6
    interrogative_start_score: float = 0.65
    contains_question_mark_score: float = 0.2
    spoken_prompt_score: float = 0.75
    mid_sentence_interrogative_score: float = 0.4
    max_recent_questions_tracked: int = 20


class QuestionDetector:
    """Stateful per-session: remembers recently detected question texts
    (normalized) to suppress near-duplicates produced when the same
    spoken question straddles two overlapping Whisper rolling windows —
    `TranscriptionSession`'s `dedupe_overlap` already strips most literal
    repeats, this is a second, question-specific safety net."""

    def __init__(self, config: Optional[QuestionDetectionConfig] = None) -> None:
        self._config = config or QuestionDetectionConfig()
        self._recent_normalized_texts: List[str] = []

    @property
    def confidence_threshold(self) -> float:
        return self._config.confidence_threshold

    def score(self, text: str) -> float:
        """The raw deterministic score for `text`, without the confidence
        threshold or duplicate-suppression `detect` applies — used by
        `semantic_classifier.py` to decide whether a finalized turn is
        clear enough to skip the (slower) Ollama classification stage."""
        return self._score(text.strip())

    def detect(self, text: str) -> Optional[DetectedQuestionResult]:
        stripped = text.strip()
        if not stripped:
            return None

        confidence = self._score(stripped)
        if confidence < self._config.confidence_threshold:
            return None

        normalized = self._normalize(stripped)
        if not normalized or self._is_duplicate(normalized):
            return None

        self._remember(normalized)
        return DetectedQuestionResult(text=stripped, confidence=confidence)

    def _score(self, text: str) -> float:
        score = 0.0
        rstripped = text.rstrip()
        ends_with_question_mark = rstripped.endswith("?")

        if ends_with_question_mark:
            score += self._config.ends_with_question_mark_score
        elif "?" in text:
            score += self._config.contains_question_mark_score

        words = text.lower().split()
        first_word = words[0].strip(".,!?") if words else ""
        if first_word in _LEADING_FILLER_WORDS and len(words) > 1:
            first_word = words[1].strip(".,!?")
        is_leading_interrogative = first_word in _INTERROGATIVE_STARTS
        if is_leading_interrogative:
            score += self._config.interrogative_start_score

        if _SPOKEN_PROMPT_RE.match(text):
            score += self._config.spoken_prompt_score
        elif not is_leading_interrogative and _INTERROGATIVE_WORD_RE.search(text):
            # Only a weaker, additional signal when the stronger leading-
            # word/spoken-prompt checks didn't already fire — an
            # interrogative word appearing later in an already-clear
            # question ("why did it take six weeks") must not be double
            # counted on top of the leading-word score.
            score += self._config.mid_sentence_interrogative_score

        return min(score, 1.0)

    def _normalize(self, text: str) -> str:
        without_punctuation = _PUNCTUATION_RE.sub("", text.lower())
        return _WHITESPACE_RE.sub(" ", without_punctuation).strip()

    def _is_duplicate(self, normalized: str) -> bool:
        return any(
            normalized == previous or normalized in previous or previous in normalized
            for previous in self._recent_normalized_texts
        )

    def _remember(self, normalized: str) -> None:
        self._recent_normalized_texts.append(normalized)
        if len(self._recent_normalized_texts) > self._config.max_recent_questions_tracked:
            self._recent_normalized_texts.pop(0)
