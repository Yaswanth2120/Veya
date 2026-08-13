"""Two-stage answer-request classification for one finalized spoken turn.

Stage 1 (always runs, fast, local, deterministic): `QuestionDetector`'s
existing punctuation/interrogative/spoken-prompt heuristic. Clear cases
(obvious questions, obvious ordinary statements) are decided here without
ever calling the LLM — this is what keeps latency low for the common
case.

Stage 2 (only for turns stage 1 can't confidently decide): a local Ollama
call asking for structured JSON. Never trusted blindly — the response is
parsed and schema-validated; anything unavailable, malformed, or slow
falls back to stage 1's own (lower-confidence) verdict rather than ever
crashing transcription or hanging indefinitely. Never logs turn text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

from .question_detector import QuestionDetector
from ..llm.provider import LLMProvider

logger = logging.getLogger("veya.classifier")

# Below this raw score, stage 1 is confident enough to reject outright
# (ordinary statements, greetings, acknowledgements) without spending a
# round trip to Ollama.
LOW_CONFIDENCE_REJECT_BOUND = 0.35

_CLASSIFIER_TIMEOUT_SECONDS = 6.0

_PROMPT_TEMPLATE = '''You are classifying one finalized spoken turn from a live interview/meeting transcript. Decide whether the speaker is asking the candidate to answer or explain something (a question, or a statement-form prompt like "tell me about yourself" or "walk me through your resume"), as opposed to an ordinary statement, greeting, acknowledgement, or the candidate's own answer.

Return ONLY JSON with exactly these keys:
{{"is_answer_request": boolean, "confidence": number between 0 and 1, "normalized_question": string, "reason_category": string}}

"normalized_question" is the turn rewritten as a single clean question/request if is_answer_request is true, else "".
"reason_category" is a short label such as "direct_question", "statement_prompt", "follow_up", "coding_prompt", "system_design_prompt", "not_a_request", "greeting", "acknowledgement".

TURN: {turn_text}'''


@dataclass(frozen=True)
class ClassificationResult:
    is_answer_request: bool
    confidence: float
    normalized_question: str
    reason_category: str
    used_semantic_stage: bool


async def classify_turn(
    turn_text: str,
    detector: QuestionDetector,
    llm_provider: Optional[LLMProvider],
) -> ClassificationResult:
    detected = detector.detect(turn_text)
    if detected is not None:
        return ClassificationResult(
            is_answer_request=True, confidence=detected.confidence,
            normalized_question=detected.text, reason_category="deterministic", used_semantic_stage=False,
        )

    raw_score = detector.score(turn_text)
    if raw_score < LOW_CONFIDENCE_REJECT_BOUND or llm_provider is None:
        return ClassificationResult(
            is_answer_request=False, confidence=raw_score,
            normalized_question="", reason_category="deterministic", used_semantic_stage=False,
        )

    semantic_result = await _classify_with_ollama(turn_text, llm_provider)
    if semantic_result is not None:
        return semantic_result

    # Ollama unavailable/malformed/timed out — fall back to the
    # deterministic gate's own (ambiguous, sub-threshold) verdict rather
    # than ever blocking or crashing.
    return ClassificationResult(
        is_answer_request=False, confidence=raw_score,
        normalized_question="", reason_category="fallback_after_semantic_unavailable", used_semantic_stage=False,
    )


async def _classify_with_ollama(turn_text: str, llm_provider: LLMProvider) -> Optional[ClassificationResult]:
    prompt = _PROMPT_TEMPLATE.format(turn_text=turn_text)
    try:
        parts = []

        async def collect() -> None:
            async for delta in llm_provider.generate_stream(prompt, timeout=_CLASSIFIER_TIMEOUT_SECONDS):
                parts.append(delta)

        await asyncio.wait_for(collect(), timeout=_CLASSIFIER_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - any provider/timeout failure falls back, never propagates
        logger.info("Semantic classifier unavailable (%s); falling back to deterministic gate.", type(exc).__name__)
        return None

    raw = "".join(parts).strip()
    parsed = _parse_classification_json(raw)
    if parsed is None:
        logger.info("Semantic classifier returned unparseable output; falling back to deterministic gate.")
        return None
    return parsed


def _parse_classification_json(raw: str) -> Optional[ClassificationResult]:
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    is_answer_request = payload.get("is_answer_request")
    confidence = payload.get("confidence")
    normalized_question = payload.get("normalized_question")
    reason_category = payload.get("reason_category")

    if not isinstance(is_answer_request, bool):
        return None
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    if not isinstance(normalized_question, str) or not isinstance(reason_category, str):
        return None

    return ClassificationResult(
        is_answer_request=is_answer_request,
        confidence=max(0.0, min(1.0, float(confidence))),
        normalized_question=normalized_question.strip(),
        reason_category=reason_category.strip() or "unspecified",
        used_semantic_stage=True,
    )
