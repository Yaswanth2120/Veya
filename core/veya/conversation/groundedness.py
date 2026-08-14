"""Section 19: a structured groundedness guard for generated answers —
catches the two concrete failure modes the build prompt calls out:
a self-contradictory numeric claim (e.g. "35% to 35%" — not a change at
all), and a specific number/percentage the answer states that appears
nowhere in the actual grounding context it was given (resume/JD chunks,
approved memory, the candidate's own authoritative spoken context).

Deliberately narrow and structured (matches against the real context
text actually sent to the model), not a general hallucination detector —
overclaiming detection accuracy here would be its own kind of dishonesty.
A number legitimately mentioned in the interviewer's own question (e.g.
"...reduce latency by how much?") is not itself a claim the answer is
inventing, so question text counts as grounding too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PERCENT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%")
# "35% to 35%", "from 20 to 20", "increased from 5% to 5%" — the exact
# shape of a stated change where the before/after values are identical,
# which is never a meaningful reduction/increase claim.
_SAME_VALUE_CHANGE_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s?%?\s*(?:to|down to|up to)\s*(\d{1,3}(?:\.\d+)?)\s?%",
    re.IGNORECASE,
)

_SAFE_FALLBACK_ANSWER = (
    "I'd want to answer that with the specific numbers, but I don't have enough verified detail in front of me "
    "to give you an accurate figure right now — I don't want to guess at something that specific."
)


@dataclass(frozen=True)
class GroundednessResult:
    is_grounded: bool
    reason: str = ""


def _has_self_contradictory_numeric_change(text: str) -> bool:
    for match in _SAME_VALUE_CHANGE_RE.finditer(text):
        before, after = match.group(1), match.group(2)
        try:
            if float(before) == float(after):
                return True
        except ValueError:
            continue
    return False


def _percentages_in(text: str) -> set:
    return {match.group(0).replace(" ", "") for match in _PERCENT_RE.finditer(text)}


def check_answer_groundedness(answer_text: str, grounding_text: str) -> GroundednessResult:
    """`grounding_text` should be every real source the answer was
    actually allowed to draw from (document context + approved memory +
    the candidate's own authoritative spoken context + the question
    text itself) concatenated together. Only numeric/percentage claims
    are checked — this is not a general fact-checker."""
    if not answer_text.strip():
        return GroundednessResult(is_grounded=True)

    if _has_self_contradictory_numeric_change(answer_text):
        return GroundednessResult(is_grounded=False, reason="self_contradictory_numeric_change")

    answer_percentages = _percentages_in(answer_text)
    if answer_percentages:
        grounding_percentages = _percentages_in(grounding_text)
        unsupported = answer_percentages - grounding_percentages
        if unsupported:
            return GroundednessResult(is_grounded=False, reason="unsupported_numeric_claim")

    return GroundednessResult(is_grounded=True)


def safe_fallback_answer() -> str:
    return _SAFE_FALLBACK_ANSWER
