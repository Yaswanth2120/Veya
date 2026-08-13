"""Typed knowledge-layer errors. Every document-ingestion/retrieval
failure mode becomes one of these — never a bare filesystem/parsing
exception surfacing to the dispatcher or a log line. `reason` is always
safe to send to Swift and to log: a description of *what went wrong*,
never document content.
"""

from __future__ import annotations


class KnowledgeError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DocumentPathInvalidError(KnowledgeError):
    """The supplied `file_path` doesn't resolve to an existing file
    beneath the app-managed documents directory — never read arbitrary
    filesystem paths."""


class DocumentUnsupportedError(KnowledgeError):
    """Extension isn't one of the supported V1 formats (.txt/.md/.pdf/.docx)."""


class DocumentEncryptedError(KnowledgeError):
    """A password-protected/encrypted PDF or DOCX — not decrypted, ever."""


class DocumentMalformedError(KnowledgeError):
    """The file claims to be a supported format but couldn't actually be
    parsed (corrupt PDF/DOCX structure, etc.)."""


class DocumentEmptyError(KnowledgeError):
    """Extraction succeeded but produced no usable text (e.g. a scanned,
    non-OCR'd PDF with no embedded text layer)."""


class DocumentOversizedError(KnowledgeError):
    """The file exceeds the configured maximum size."""


class EmbeddingUnavailableError(KnowledgeError):
    """The local embedding provider isn't usable right now (not
    configured, unreachable, or the configured model isn't present) —
    mirrors `llm.errors.LLMUnavailableError`'s role for chat generation."""
