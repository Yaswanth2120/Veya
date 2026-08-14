"""Section 19: one shared, non-speech-marker + low-quality eligibility
layer for raw transcript text — the single place every consumer
(streaming/batch transcription, turn assembly, candidate tracking,
question detection, prompt construction) checks before treating text as
real spoken content. Never logs or exposes the rejected text itself —
callers get back a typed `TranscriptRejectionReason` only, safe to count
in diagnostics.

Whisper (and whisper.cpp) emit a bracketed/parenthesized tag instead of
real words for a non-speech window — `[BLANK_AUDIO]`, `(silence)`,
`[SILENCE]`, `(soft music)`, `(mouse clicking)`, etc. These must never
reach the transcript, a question candidate, a detected question, an
answer prompt, a report, or durable memory as if they were real speech.
A real spoken sentence that merely *contains* a bracketed aside (e.g. a
literal parenthetical the speaker said) must never be destroyed — only
bracket/paren groups whose content matches a known non-speech marker
vocabulary are treated as noise; everything else is left alone.
"""

from __future__ import annotations

import re
from enum import Enum

# Deliberately not "any bracketed content" — only content that reads as
# one of Whisper's own non-speech tag conventions. Matched after
# stripping punctuation and collapsing whitespace, case-insensitive.
_MARKER_PHRASES = {
    "blank audio",
    "inaudible",
    "silence",
    "no audio",
    "no speech",
    "music",
    "soft music",
    "background music",
    "wind blowing",
    "wind",
    "static",
    "background noise",
    "noise",
    "mouse clicking",
    "mouse click",
    "clicking",
    "click",
    "keyboard clicking",
    "keyboard click",
    "typing",
    "laughter",
    "laughing",
    "applause",
    "clapping",
    "coughing",
    "cough",
    "sigh",
    "sighs",
    "breathing",
    "sniffling",
    "pause",
    "silence.",
}

_BRACKET_GROUP_RE = re.compile(r"[\[\(][^\[\]\(\)]{0,60}[\]\)]")
_NON_WORD_RE = re.compile(r"[^a-z ]")
_WHITESPACE_RE = re.compile(r"\s+")


class TranscriptRejectionReason(str, Enum):
    NONE = "none"
    EMPTY = "transcript_rejected_empty"
    NON_SPEECH_MARKER = "transcript_rejected_non_speech_marker"
    TOO_SHORT = "transcript_rejected_too_short"
    LOW_QUALITY = "turn_rejected_low_quality"


def _normalize_marker_candidate(inner: str) -> str:
    lowered = inner.strip().lower()
    # Whisper's own tag convention uses underscores ("BLANK_AUDIO") where
    # a real phrase would have a space ("blank audio") — normalize
    # separators to spaces before stripping remaining punctuation, so
    # both spellings match the same vocabulary entry.
    with_spaces = re.sub(r"[_\-]", " ", lowered)
    without_punctuation = _NON_WORD_RE.sub("", with_spaces)
    return _WHITESPACE_RE.sub(" ", without_punctuation).strip()


def _is_marker_phrase(inner: str) -> bool:
    normalized = _normalize_marker_candidate(inner)
    return not normalized or normalized in _MARKER_PHRASES


def _text_with_markers_removed(text: str) -> str:
    """Removes only bracket/paren groups matching a known non-speech
    marker phrase — a literal parenthetical that's part of real speech
    (e.g. "(and I mean that)") never matches and is left untouched."""

    def _replace(match: re.Match) -> str:
        inner = match.group(0)[1:-1]
        return "" if _is_marker_phrase(inner) else match.group(0)

    without_markers = _BRACKET_GROUP_RE.sub(_replace, text)
    return _WHITESPACE_RE.sub(" ", without_markers).strip()


def classify_transcript_text(text: str, *, min_length: int = 2) -> TranscriptRejectionReason:
    """The one authoritative eligibility check. Never mutates or returns
    the rejected text — callers only ever see the typed reason, safe to
    log/count."""
    stripped = text.strip()
    if not stripped:
        return TranscriptRejectionReason.EMPTY
    without_markers = _text_with_markers_removed(stripped)
    if not without_markers:
        return TranscriptRejectionReason.NON_SPEECH_MARKER
    if len(without_markers) < min_length:
        return TranscriptRejectionReason.TOO_SHORT
    return TranscriptRejectionReason.NONE


def is_eligible_transcript_text(text: str, *, min_length: int = 2) -> bool:
    return classify_transcript_text(text, min_length=min_length) == TranscriptRejectionReason.NONE


# A slightly higher bar for a *finalized interviewer turn* than for a
# single fragment — "ok" or "so" alone is eligible fragment-level text
# (harmless, might legitimately precede a real question) but is not a
# credible complete turn to detect a question from.
_MIN_CREDIBLE_TURN_LENGTH = 4
# Guards against a turn built almost entirely from repeated ASR garbage
# (e.g. Whisper hallucinating the same short phrase over and over on
# near-silent audio) — a real spoken turn practically never repeats one
# short token this many times in a row.
_MAX_IMMEDIATE_WORD_REPETITION = 4


def classify_turn_quality(text: str) -> TranscriptRejectionReason:
    """Turn-level quality gate, run once a turn has actually finalized —
    stricter than per-fragment eligibility, and independent of it (a
    turn assembled from several individually-eligible fragments can
    still be low-quality garbage overall)."""
    base = classify_transcript_text(text, min_length=_MIN_CREDIBLE_TURN_LENGTH)
    if base != TranscriptRejectionReason.NONE:
        return TranscriptRejectionReason.LOW_QUALITY if base == TranscriptRejectionReason.TOO_SHORT else base

    words = text.strip().lower().split()
    if len(words) >= _MAX_IMMEDIATE_WORD_REPETITION:
        run_length = 1
        for previous, current in zip(words, words[1:]):
            if current == previous:
                run_length += 1
                if run_length >= _MAX_IMMEDIATE_WORD_REPETITION:
                    return TranscriptRejectionReason.LOW_QUALITY
            else:
                run_length = 1

    return TranscriptRejectionReason.NONE


def is_credible_turn(text: str) -> bool:
    return classify_turn_quality(text) == TranscriptRejectionReason.NONE


# Section 19: a compound interviewer question ("What was the bottleneck,
# and how did you reduce the latency?") often has a short, natural pause
# right at the internal conjunction — well under a real VAD silence
# threshold, but longer than the speculative-finalize debounce, which
# reacts to "no new ASR fragment yet" rather than real acoustic silence.
# Text ending in a dangling conjunction/comma is still visibly
# incomplete — never treated as a strong, finished prompt on its own.
_INCOMPLETE_ENDING_RE = re.compile(
    r"(,|\b(and|or|but|so|because|which|that|while|since|although|when|if|to|of|the|a|an)\s*)$",
    re.IGNORECASE,
)


def looks_like_incomplete_sentence(text: str) -> bool:
    return bool(_INCOMPLETE_ENDING_RE.search(text.strip()))
