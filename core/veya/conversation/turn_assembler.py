"""Combines `transcript.final` fragments (one per Whisper rolling window)
into complete spoken turns. A single interviewer question is frequently
split across multiple windows — this is the component that fixes that,
replacing the old "judge each fragment independently" behavior.

A turn finalizes only on a real endpoint, signaled by the caller:
- a VAD silence endpoint (`request_finalize_at`), once the fragment
  covering that boundary has actually arrived;
- an explicit flush (session stop / safety cap), which finalizes
  immediately with whatever has been buffered so far.

Ownership: one `TurnAssembler` per real-transcription session, owned by
`ConversationOrchestrator`. Never sees partial transcripts — only the
same deduplicated `transcript.final` text `TranscriptionSession` already
produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..transcription.overlap import dedupe_overlap

# Bounds how much fragment text one turn can accumulate — a runaway
# "in speech forever" edge case (e.g. VAD never detects silence and the
# max-turn-duration safety net is somehow bypassed by the caller) must
# never grow this without bound.
_MAX_TURN_CHARACTERS = 4000


@dataclass
class _Fragment:
    text: str
    started_at: float
    ended_at: float


@dataclass
class TurnAssembler:
    _fragments: List[_Fragment] = field(default_factory=list)
    _pending_finalize_at: Optional[float] = None

    def request_finalize_at(self, boundary_time: float) -> Optional[str]:
        """Called when VAD reports a turn boundary at `boundary_time`
        (the audio timestamp silence began). If a fragment already
        buffered already covers that boundary (its `ended_at` is at or
        after it), finalizes immediately and returns the assembled turn
        text. Otherwise remembers the boundary — the next fragment whose
        `ended_at` reaches it triggers finalization in `add_fragment`."""
        if self._fragments and self._fragments[-1].ended_at >= boundary_time:
            return self._finalize()
        self._pending_finalize_at = boundary_time
        return None

    def add_fragment(self, text: str, started_at: float, ended_at: float) -> Optional[str]:
        """Appends a new `transcript.final` fragment to the current turn,
        deduplicating any overlap with the previous fragment the same way
        `TranscriptionSession` already does across raw Whisper windows
        (a turn can span several windows, so the same class of boundary
        repetition can occur here too). Returns the assembled turn text
        if this fragment satisfies a pending finalize boundary, else
        `None` (the turn is still open)."""
        stripped = text.strip()
        if stripped:
            previous_text = self._fragments[-1].text if self._fragments else ""
            deduped = dedupe_overlap(previous_text, stripped) if previous_text else stripped
            if deduped:
                self._fragments.append(_Fragment(text=deduped, started_at=started_at, ended_at=ended_at))
                self._trim_if_too_long()

        if self._pending_finalize_at is not None and ended_at >= self._pending_finalize_at:
            return self._finalize()
        return None

    def flush(self) -> Optional[str]:
        """Force-finalizes whatever is currently buffered — used for
        explicit session stop and the max-turn-duration safety net.
        Returns `None` if nothing was buffered."""
        if not self._fragments:
            self._pending_finalize_at = None
            return None
        return self._finalize()

    @property
    def has_pending_content(self) -> bool:
        return bool(self._fragments)

    def peek_pending_text(self) -> str:
        """The turn text assembled so far, without finalizing/consuming
        it — lets a caller judge whether the still-open turn already
        reads as a complete, strong prompt (e.g. to decide whether to
        finalize early rather than waiting for a VAD silence endpoint
        that may never come, e.g. under continuous background noise)."""
        return " ".join(fragment.text for fragment in self._fragments if fragment.text).strip()

    def _finalize(self) -> Optional[str]:
        text = " ".join(fragment.text for fragment in self._fragments if fragment.text).strip()
        self._fragments = []
        self._pending_finalize_at = None
        return text or None

    def _trim_if_too_long(self) -> None:
        total = sum(len(fragment.text) for fragment in self._fragments)
        while total > _MAX_TURN_CHARACTERS and len(self._fragments) > 1:
            removed = self._fragments.pop(0)
            total -= len(removed.text)
