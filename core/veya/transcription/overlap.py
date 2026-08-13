"""Word-level overlap deduplication between two consecutive rolling-window
transcripts. Windows deliberately overlap (see `rolling_buffer.py`) so a
word spoken right at a window boundary isn't lost to either side, which
means naive concatenation would repeat the overlapping words verbatim in
both transcripts. `dedupe_overlap` strips that repeated prefix from the new
transcript before it's emitted, using a plain word-level suffix/prefix
match — good enough for Whisper's fairly stable phrasing across nearby
windows, not a general text-alignment algorithm.
"""

from __future__ import annotations


def dedupe_overlap(previous_text: str, new_text: str, max_overlap_words: int = 12) -> str:
    """Returns `new_text` with any prefix that duplicates the end of
    `previous_text` removed. Tries the longest plausible overlap first so a
    short accidental match (e.g. both texts starting with "the") doesn't
    strip more than actually repeated."""
    if not previous_text or not new_text:
        return new_text

    previous_words = previous_text.split()
    new_words = new_text.split()
    max_check = min(max_overlap_words, len(previous_words), len(new_words))

    for overlap_len in range(max_check, 0, -1):
        if previous_words[-overlap_len:] == new_words[:overlap_len]:
            return " ".join(new_words[overlap_len:])

    return new_text
