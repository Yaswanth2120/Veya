"""Session analysis — synthesizes a `SessionReport` from transcript/
question/answer data Swift already owns and sends narrowly for this one
RPC. Nothing here persists that raw data; only the derived report (and,
via `memory_candidates`, proposed — never auto-approved — memory text)
crosses back to Swift/the memory store. Never logs report content."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..llm.provider import LLMProvider

logger = logging.getLogger("veya.report")

# Bounds how much raw session content is ever included in one LLM prompt —
# a long session must not grow the request unboundedly.
_MAX_TRANSCRIPT_CHARACTERS = 20_000
_MAX_QA_ITEMS = 100


@dataclass
class SessionReport:
    session_id: str
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    generated_answers: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    unanswered_questions: list[str] = field(default_factory=list)
    preparation_gaps: list[str] = field(default_factory=list)
    memory_candidates: list[str] = field(default_factory=list)


async def analyze_session(
    provider: Optional[LLMProvider],
    session_id: str,
    transcript: list[dict],
    questions: list[dict],
    answers: list[dict],
) -> SessionReport:
    question_texts = [str(q.get("text", "")) for q in questions if isinstance(q, dict) and q.get("text")]
    answered_question_ids = {a.get("question_id") for a in answers if isinstance(a, dict)}
    unanswered = [
        str(q.get("text", "")) for q in questions
        if isinstance(q, dict) and q.get("id") not in answered_question_ids and q.get("text")
    ]

    sources: list[dict] = []
    seen_chunk_ids: set[str] = set()
    for answer in answers[:_MAX_QA_ITEMS]:
        if not isinstance(answer, dict):
            continue
        for source in answer.get("sources", []) or []:
            if not isinstance(source, dict):
                continue
            chunk_id = source.get("chunk_id")
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            sources.append(source)

    generated_answers = [
        {
            "question": str(a.get("question", "")),
            "talking_points": [str(p) for p in a.get("talking_points", []) if isinstance(p, str)],
        }
        for a in answers[:_MAX_QA_ITEMS] if isinstance(a, dict)
    ]

    report = SessionReport(
        session_id=session_id,
        questions=question_texts,
        generated_answers=generated_answers,
        sources=sources,
        unanswered_questions=unanswered,
    )

    if provider is None:
        report.summary = "No local LLM was available; this report only reflects raw session data."
        return report

    transcript_text = "\n".join(str(seg.get("text", "")) for seg in transcript if isinstance(seg, dict))[:_MAX_TRANSCRIPT_CHARACTERS]
    qa_block = "\n".join(f"Q: {a['question']}\nA: {' '.join(a['talking_points'])}" for a in generated_answers)
    prompt = f'''You are summarizing a completed local session for the person who attended it.
Return ONLY JSON with keys: summary (string), topics (list of strings), decisions (list of strings),
action_items (list of strings), preparation_gaps (list of strings), memory_candidates (list of short
standalone facts worth remembering for future sessions, e.g. "Prefers concise answers", "Works on the
payments team").
TRANSCRIPT (may be partial):\n{transcript_text}\nEND TRANSCRIPT
QUESTIONS AND ANSWERS:\n{qa_block}\nEND QUESTIONS AND ANSWERS'''

    try:
        parts = []
        async for delta in provider.generate_stream(prompt, timeout=45):
            parts.append(delta)
        parsed = json.loads("".join(parts))
        report.summary = str(parsed.get("summary", ""))
        report.topics = [str(x) for x in parsed.get("topics", []) if isinstance(x, (str, int, float))]
        report.decisions = [str(x) for x in parsed.get("decisions", []) if isinstance(x, (str, int, float))]
        report.action_items = [str(x) for x in parsed.get("action_items", []) if isinstance(x, (str, int, float))]
        report.preparation_gaps = [str(x) for x in parsed.get("preparation_gaps", []) if isinstance(x, (str, int, float))]
        report.memory_candidates = [str(x) for x in parsed.get("memory_candidates", []) if isinstance(x, (str, int, float))]
    except Exception as exc:  # noqa: BLE001 - a malformed/unavailable LLM response must not fail analysis
        logger.info("Unhandled %s synthesizing session report; returning data-only report.", type(exc).__name__)
        report.summary = "The local model did not return a usable summary; raw session data is still included below."

    return report
