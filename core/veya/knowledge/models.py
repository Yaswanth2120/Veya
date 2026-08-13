"""Data shapes shared across `knowledge/`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

# Conservative V1 cap — well above any real session document while still
# bounding worst-case parse time/memory for a local, synchronous extractor.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

# Source-file size alone is not enough for archive formats: a small DOCX
# can expand into a very large XML payload. Keep both parsing work and the
# text passed to chunking/embedding bounded.
MAX_EXTRACTED_TEXT_CHARACTERS = 2_000_000
MAX_DOCX_ARCHIVE_ENTRIES = 1_000
MAX_DOCX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100

SUPPORTED_EXTENSIONS = {"txt", "md", "pdf", "docx"}


class IngestionStatus(str, Enum):
    NOT_INDEXED = "not_indexed"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ChunkingConfig:
    target_chunk_characters: int = 800
    overlap_characters: int = 150
    max_excerpt_length: int = 240

    def __post_init__(self) -> None:
        if self.target_chunk_characters <= 0:
            raise ValueError("target_chunk_characters must be positive.")
        if not (0 <= self.overlap_characters < self.target_chunk_characters):
            raise ValueError("overlap_characters must be non-negative and smaller than target_chunk_characters.")


@dataclass(frozen=True)
class RetrievalConfig:
    top_k: int = 5
    similarity_threshold: float = 0.3
    max_context_characters: int = 4000
    max_excerpt_length: int = 240


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    document_id: str
    session_id: str
    file_name: str
    chunk_index: int
    text: str
    excerpt: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: DocumentChunk
    score: float
