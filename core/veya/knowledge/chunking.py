"""Deterministic character-based chunking with overlap. No tokenizer
dependency — character counts are a simple, dependency-free, fully
deterministic proxy for "roughly how much text fits in a bounded prompt
context," which is all this needs for V1.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional

from .models import ChunkingConfig, DocumentChunk


def _stable_chunk_id(document_id: str, chunk_index: int) -> str:
    """Deterministic given `(document_id, chunk_index)` — re-ingesting the
    same document produces the same chunk IDs, which is what makes
    `VectorStore.replace_chunks` a clean replace rather than an
    ever-growing duplicate set."""
    digest = hashlib.sha256(f"{document_id}:{chunk_index}".encode("utf-8")).hexdigest()
    return digest[:32]


def chunk_text(
    text: str,
    document_id: str,
    session_id: str,
    file_name: str,
    config: Optional[ChunkingConfig] = None,
) -> List[DocumentChunk]:
    """Splits `text` into overlapping, order-preserving chunks. Returns an
    empty list for empty text (callers should have already rejected that
    via `DocumentEmptyError` before chunking, but this stays total)."""
    config = config or ChunkingConfig()
    if not text:
        return []

    chunks: List[DocumentChunk] = []
    step = config.target_chunk_characters - config.overlap_characters
    length = len(text)
    start = 0
    index = 0

    while start < length:
        end = min(start + config.target_chunk_characters, length)
        chunk_body = text[start:end]
        chunks.append(
            DocumentChunk(
                chunk_id=_stable_chunk_id(document_id, index),
                document_id=document_id,
                session_id=session_id,
                file_name=file_name,
                chunk_index=index,
                text=chunk_body,
                excerpt=chunk_body[: config.max_excerpt_length],
                char_start=start,
                char_end=end,
            )
        )
        index += 1
        if end == length:
            break
        start += step

    return chunks
