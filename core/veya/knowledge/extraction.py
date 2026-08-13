"""Path validation and text extraction for the supported V1 document
formats (.txt/.md/.pdf/.docx). Every function here is synchronous and
local-only — no network calls, no macro/script execution, no OCR.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

from .errors import (
    DocumentEmptyError,
    DocumentEncryptedError,
    DocumentMalformedError,
    DocumentOversizedError,
    DocumentPathInvalidError,
    DocumentUnsupportedError,
)
from .models import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCX_ARCHIVE_ENTRIES,
    MAX_DOCX_COMPRESSION_RATIO,
    MAX_DOCX_UNCOMPRESSED_BYTES,
    MAX_EXTRACTED_TEXT_CHARACTERS,
    SUPPORTED_EXTENSIONS,
)

_DOCX_WORD_TEXT_TAG = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
_DOCX_WORD_PARAGRAPH_TAG = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

# pypdf logs malformed-PDF diagnostics (e.g. "invalid pdf header: %(header_byte)r",
# "incorrect startxref pointer(...)") via `logging.getLogger(__name__).warning(...)`
# from its own submodules (`pypdf._reader`, etc) — meaning bytes read
# straight out of a malformed document could otherwise reach worker
# stderr, contradicting the "stderr is metadata-only" guarantee the rest
# of this codebase enforces (see docs/IPC_PROTOCOL.md).
#
# Setting `propagate = False` alone is *not* sufficient here: Python's
# `Logger.callHandlers` falls back to `logging.lastResort` (a hardcoded
# handler that writes WARNING+ straight to `sys.stderr`) whenever it
# finds *no handler at all* while walking the logger chain — and since
# neither "pypdf" nor its submodule loggers have any handlers of their
# own, stopping propagation early (before reaching this worker's
# root-logger handler) still leaves `found == 0`, so `lastResort` fires
# anyway. Raising the "pypdf" logger's level above CRITICAL instead
# suppresses these calls at the source (`Logger.isEnabledFor` returns
# `False` before a record is even created), which is what actually stops
# them from reaching stderr by any path. `propagate = False` is kept too,
# as defense in depth in case some future pypdf release attaches its own
# handler directly.
logging.getLogger("pypdf").setLevel(logging.CRITICAL + 1)
logging.getLogger("pypdf").propagate = False


def validate_document_path(file_path: str, documents_directory: Path) -> Path:
    """Canonicalizes `file_path` and requires it to resolve to an existing
    file strictly beneath `documents_directory` — the one and only
    filesystem boundary Python is allowed to read from. Never follows a
    request to read an arbitrary path, symlink-escaped or otherwise."""
    try:
        resolved = Path(file_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DocumentPathInvalidError("The document path does not exist.") from exc

    if not resolved.is_file():
        raise DocumentPathInvalidError("The document path is not a regular file.")

    documents_root = documents_directory.resolve()
    try:
        resolved.relative_to(documents_root)
    except ValueError as exc:
        raise DocumentPathInvalidError(
            "The document path is not inside the managed documents directory."
        ) from exc

    return resolved


def extract_text(path: Path, file_extension: str) -> str:
    """Extracts and returns plain text for `path`. Raises a typed
    `KnowledgeError` subclass for every failure mode — never a bare
    parser/filesystem exception. Callers must call
    `validate_document_path` first; this does not re-validate the path."""
    extension = file_extension.lower().lstrip(".")
    if extension not in SUPPORTED_EXTENSIONS:
        raise DocumentUnsupportedError(f"'.{extension}' is not a supported document type.")

    size_bytes = path.stat().st_size
    if size_bytes > MAX_DOCUMENT_BYTES:
        raise DocumentOversizedError(
            f"Document exceeds the maximum size of {MAX_DOCUMENT_BYTES} bytes."
        )

    if extension in ("txt", "md"):
        text = _extract_plain_text(path)
    elif extension == "pdf":
        text = _extract_pdf(path)
    else:
        text = _extract_docx(path)

    if len(text) > MAX_EXTRACTED_TEXT_CHARACTERS:
        raise DocumentOversizedError("Extracted document text exceeds the supported limit.")

    stripped = text.strip()
    if not stripped:
        raise DocumentEmptyError("No extractable text was found in this document.")
    return stripped


def _extract_plain_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DocumentMalformedError("Could not read this file.") from exc


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise DocumentMalformedError(
            "PDF extraction requires the 'pypdf' package, which is not installed."
        ) from exc

    try:
        reader = PdfReader(str(path))
    except PdfReadError as exc:
        raise DocumentMalformedError("This PDF could not be parsed.") from exc
    except Exception as exc:  # noqa: BLE001 - pypdf can raise several underlying exception types
        raise DocumentMalformedError("This PDF could not be parsed.") from exc

    if reader.is_encrypted:
        raise DocumentEncryptedError("This PDF is password-protected/encrypted.")

    try:
        pages_text = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001
        raise DocumentMalformedError("This PDF's text could not be extracted.") from exc

    return "\n".join(pages_text)


def _extract_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            _validate_docx_archive(archive)
            with archive.open("word/document.xml") as document_xml:
                # ElementTree does not fetch external entities, and this
                # explicit preflight rejects DTD/entity declarations before
                # any XML parser work. `document.xml` is already bounded by
                # `_validate_docx_archive`, so reading it here is safe.
                xml_data = document_xml.read()
                if b"<!DOCTYPE" in xml_data.upper() or b"<!ENTITY" in xml_data.upper():
                    raise DocumentMalformedError("This .docx file contains unsupported XML declarations.")
                root = ElementTree.fromstring(xml_data)
    except zipfile.BadZipFile as exc:
        raise DocumentMalformedError("This .docx file is not a valid ZIP archive.") from exc
    except KeyError as exc:
        raise DocumentMalformedError("This .docx file has no word/document.xml part.") from exc
    except ElementTree.ParseError as exc:
        raise DocumentMalformedError("This .docx file's document.xml is malformed.") from exc
    except RuntimeError as exc:
        # zipfile raises RuntimeError for a password-protected archive.
        raise DocumentEncryptedError("This .docx file is password-protected/encrypted.") from exc

    paragraphs = []
    for paragraph in root.iter(_DOCX_WORD_PARAGRAPH_TAG):
        run_texts = [node.text for node in paragraph.iter(_DOCX_WORD_TEXT_TAG) if node.text]
        if run_texts:
            paragraphs.append("".join(run_texts))
    return "\n".join(paragraphs)


def _validate_docx_archive(archive: zipfile.ZipFile) -> None:
    """Reject archives that are unsafe to expand before opening XML.

    A DOCX is a ZIP archive. These metadata checks do not replace OS-level
    resource limits, but they stop the common ZIP-bomb and encrypted-member
    cases without ever materializing an unbounded member in memory.
    """
    entries = archive.infolist()
    if len(entries) > MAX_DOCX_ARCHIVE_ENTRIES:
        raise DocumentOversizedError("This .docx archive has too many files.")

    total_uncompressed = 0
    for entry in entries:
        if entry.flag_bits & 0x1:
            raise DocumentEncryptedError("This .docx file is password-protected/encrypted.")
        total_uncompressed += entry.file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise DocumentOversizedError("This .docx archive expands beyond the supported limit.")
        if entry.compress_size and entry.file_size / entry.compress_size > MAX_DOCX_COMPRESSION_RATIO:
            raise DocumentOversizedError("This .docx archive has an unsafe compression ratio.")
