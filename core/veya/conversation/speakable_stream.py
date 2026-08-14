"""Section 18: converts a raw, incrementally-arriving LLM stream into
clean, speakable answer prose — never `<think>...</think>` (or
equivalent reasoning-block) content, and never the `ANSWER:`/`POINTS:`/
`CAVEAT:` protocol labels or the `POINTS:`/`CAVEAT:` sections themselves
(supplementary detail, not the primary speakable answer).

Two independent concerns, composed in `SpeakableAnswerStream`:

- `_ThinkTagFilter`: strips reasoning blocks from the raw stream,
  correctly handling a tag split across chunks and a block that never
  closes (in which case everything after the unclosed opening tag is
  suppressed for the rest of the stream — the safe default, never leaks).
- A per-line header state machine: decides whether the model followed
  the requested `ANSWER:`/`POINTS:`/`CAVEAT:` format at all (a bounded
  "sniff" against the first line — a real prefix check, not an arbitrary
  character cap) and, once inside the `ANSWER:` section, buffers the
  start of every subsequent line just long enough to rule out a
  `POINTS:`/`CAVEAT:` header before passing that line through — so
  header suppression is correct regardless of how the provider chunks
  its token stream (including one character at a time). If the model
  ignores the format entirely, everything (after think stripping) is
  speakable — mirrors `answer_generation.parse_answer_text`'s own
  raw-text fallback for the final parse.

`clean_text()` (think-stripped, headers intact) is what
`answer_generation.generate_answer` finally hands to `parse_answer_text` —
so the final `answer.completed.answer_text` and the live speakable
stream are always built from the exact same clean source, never two
independently-parsed views that could disagree.
"""

from __future__ import annotations

import re
from typing import List, Optional

_REASONING_TAGS = ("think", "reasoning", "scratchpad")
_OPEN_TAG_RE = re.compile(r"<(?:" + "|".join(_REASONING_TAGS) + r")>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"</(?:" + "|".join(_REASONING_TAGS) + r")>", re.IGNORECASE)
_MAX_PENDING_TAG_LENGTH = max(len(f"</{tag}>") for tag in _REASONING_TAGS) - 1

_ANSWER_HEADER_PREFIX = "answer:"
_SECTION_HEADER_PREFIXES = ("points:", "caveat:")
_POINTS_HEADER_RE = re.compile(r"^\s*points:\s*$", re.IGNORECASE)
_CAVEAT_HEADER_RE = re.compile(r"^\s*caveat:\s*", re.IGNORECASE)
# A bounded safety net alongside the real prefix-tracking below — no
# legitimate header is ever this long before its colon/newline.
_MAX_HEADER_SNIFF_CHARACTERS = 40


def _held_back_partial_tag(text: str) -> tuple:
    """Returns `(safe_to_process, held_back)` — `held_back` is a
    trailing fragment starting at the last `<` that could still grow
    into a recognized tag on the next chunk (so it must not be processed
    as ordinary text yet). Empty if nothing needs to wait."""
    last_open = text.rfind("<")
    if last_open == -1:
        return text, ""
    candidate = text[last_open:]
    if ">" in candidate or len(candidate) > _MAX_PENDING_TAG_LENGTH + 1:
        return text, ""
    return text[:last_open], candidate


class _ThinkTagFilter:
    """Strips `<think>`/`<reasoning>`/`<scratchpad>` blocks from an
    incrementally-arriving raw text stream. A single, non-nested block at
    a time (real reasoning models do not nest these) — an unclosed block
    suppresses everything from its opening tag to the end of the stream,
    which is the only safe behavior when a close tag never arrives."""

    def __init__(self) -> None:
        self._buffer = ""
        self._inside_think = False

    def feed(self, raw_delta: str) -> str:
        self._buffer += raw_delta
        output: List[str] = []
        while True:
            if not self._inside_think:
                match = _OPEN_TAG_RE.search(self._buffer)
                if match:
                    output.append(self._buffer[: match.start()])
                    self._buffer = self._buffer[match.end() :]
                    self._inside_think = True
                    continue
                safe, held_back = _held_back_partial_tag(self._buffer)
                output.append(safe)
                self._buffer = held_back
                break
            else:
                match = _CLOSE_TAG_RE.search(self._buffer)
                if match:
                    self._buffer = self._buffer[match.end() :]
                    self._inside_think = False
                    continue
                _, held_back = _held_back_partial_tag(self._buffer)
                self._buffer = held_back
                break
        return "".join(output)


class SpeakableAnswerStream:
    def __init__(self) -> None:
        self._think_filter = _ThinkTagFilter()
        self._clean_text_parts: List[str] = []

        # "sniffing" (first line: deciding whether the model uses
        # ANSWER:/POINTS:/CAVEAT: at all) -> "answer" (mid-line
        # passthrough inside the primary section) <-> "line_start"
        # (buffering the start of a new answer-section line just long
        # enough to rule out POINTS:/CAVEAT:) -> "suppressed" (inside a
        # POINTS:/CAVEAT: section) -> back to "line_start" at its next
        # newline. "fallback": no format detected — everything from here
        # on is speakable, unconditionally.
        self._section_state = "sniffing"
        self._line_buffer = ""
        # The whitespace right after "ANSWER:" must never appear in the
        # speakable stream — but a real token stream can split the colon
        # and the following space into separate chunks, so this can't
        # just be stripped once at header-detection time. Sticky until
        # the first non-whitespace speakable character is actually seen.
        self._pending_leading_whitespace_strip = False

    def feed(self, raw_delta: str) -> str:
        clean = self._think_filter.feed(raw_delta)
        if not clean:
            return ""
        self._clean_text_parts.append(clean)
        return self._extract_speakable(clean)

    def clean_text(self) -> str:
        return "".join(self._clean_text_parts)

    def _extract_speakable(self, clean_chunk: str) -> str:
        output: List[str] = []
        remaining = clean_chunk
        while remaining:
            if self._section_state == "fallback":
                output.append(remaining)
                remaining = ""
                break

            if self._section_state == "suppressed":
                newline_index = remaining.find("\n")
                if newline_index == -1:
                    remaining = ""
                    break
                remaining = remaining[newline_index + 1 :]
                self._section_state = "line_start"
                continue

            if self._section_state == "answer":
                newline_index = remaining.find("\n")
                if newline_index == -1:
                    piece = remaining
                    remaining = ""
                    output.append(self._strip_pending_leading_whitespace(piece))
                    break
                output.append(self._strip_pending_leading_whitespace(remaining[: newline_index + 1]))
                remaining = remaining[newline_index + 1 :]
                self._section_state = "line_start"
                continue

            if self._section_state == "line_start":
                self._line_buffer += remaining
                remaining = ""
                decision = self._decide_section_header(self._line_buffer)
                if decision is None:
                    break  # still ambiguous — wait for more of this line
                if decision == "header":
                    self._line_buffer = ""
                    self._section_state = "suppressed"
                    continue
                flushed = self._line_buffer
                self._line_buffer = ""
                self._section_state = "answer"
                remaining = flushed
                continue

            # "sniffing" — the very first line of the whole stream,
            # deciding whether an ANSWER: header is used at all.
            self._line_buffer += remaining
            remaining = ""
            decision = self._decide_sniff()
            if decision is None:
                break
            if decision == "answer":
                self._section_state = "answer"
                self._pending_leading_whitespace_strip = True
                remaining = _strip_answer_header(self._line_buffer)
                self._line_buffer = ""
                continue
            # "fallback" — the buffered first-line text becomes the start
            # of the speakable stream, and everything is speakable from
            # here on, unconditionally (a model this far off the
            # requested format is not expected to spontaneously start
            # emitting POINTS:/CAVEAT: headers later).
            self._section_state = "fallback"
            remaining = self._line_buffer
            self._line_buffer = ""
            continue
        return "".join(output)

    def _strip_pending_leading_whitespace(self, text: str) -> str:
        if not self._pending_leading_whitespace_strip:
            return text
        stripped = text.lstrip()
        if stripped:
            self._pending_leading_whitespace_strip = False
        return stripped

    def _decide_sniff(self) -> Optional[str]:
        """Returns "answer" (an `ANSWER:` header was found), "fallback"
        (the buffered text can no longer possibly become that header), or
        `None` (still ambiguous, need more text)."""
        if ":" in self._line_buffer and re.match(r"^\s*answer\s*:", self._line_buffer, re.IGNORECASE):
            return "answer"
        if "\n" in self._line_buffer or len(self._line_buffer) > _MAX_HEADER_SNIFF_CHARACTERS:
            return "fallback"
        stripped_lower = self._line_buffer.lstrip().lower()
        prefix_len = min(len(stripped_lower), len(_ANSWER_HEADER_PREFIX))
        if stripped_lower[:prefix_len] != _ANSWER_HEADER_PREFIX[:prefix_len]:
            return "fallback"
        return None

    def _decide_section_header(self, buffer: str) -> Optional[str]:
        """Returns "header" (a `POINTS:`/`CAVEAT:` line), "content" (this
        line is ordinary answer text — flush and resume passthrough), or
        `None` (still ambiguous, need more of this line)."""
        newline_index = buffer.find("\n")
        if newline_index != -1:
            line = buffer[: newline_index + 1]
            if _POINTS_HEADER_RE.match(line) or _CAVEAT_HEADER_RE.match(line):
                return "header"
            return "content"
        if len(buffer) > _MAX_HEADER_SNIFF_CHARACTERS:
            return "content"
        stripped_lower = buffer.lstrip().lower()
        if not stripped_lower:
            return None
        for header in _SECTION_HEADER_PREFIXES:
            prefix_len = min(len(stripped_lower), len(header))
            if stripped_lower[:prefix_len] == header[:prefix_len]:
                return None
        return "content"


def _strip_answer_header(line_and_newline: str) -> str:
    match = re.match(r"^\s*answer\s*:\s*", line_and_newline, re.IGNORECASE)
    if match:
        return line_and_newline[match.end() :]
    return line_and_newline
