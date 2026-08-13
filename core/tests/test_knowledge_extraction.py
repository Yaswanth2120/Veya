import tempfile
import unittest
import zipfile
from pathlib import Path

from veya.knowledge.errors import (
    DocumentEmptyError,
    DocumentEncryptedError,
    DocumentMalformedError,
    DocumentOversizedError,
    DocumentPathInvalidError,
    DocumentUnsupportedError,
)
from veya.knowledge.extraction import extract_text, validate_document_path
from veya.knowledge.models import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCX_UNCOMPRESSED_BYTES,
    MAX_EXTRACTED_TEXT_CHARACTERS,
)

_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 300 144] /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
5 0 obj
<< /Length 73 >>
stream
BT
/F1 18 Tf
10 100 Td
(The migration took six weeks.) Tj
ET
endstream
endobj
xref
0 6
0000000000 65535 f
trailer
<< /Size 6 /Root 1 0 R >>
startxref
0
%%EOF
"""

_DOCX_NAMESPACE_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:t>The migration took six weeks.</w:t></w:r></w:p>
<w:p><w:r><w:t>Auth service migrated first.</w:t></w:r></w:p>
</w:body>
</w:document>"""


def _write_docx(path: Path, document_xml: bytes = _DOCX_NAMESPACE_XML) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        archive.writestr("word/document.xml", document_xml)


class ValidateDocumentPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.documents_dir = Path(self._tmp.name) / "SessionDocuments"
        self.documents_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_file_inside_the_documents_directory_resolves(self):
        target = self.documents_dir / "session1" / "notes.txt"
        target.parent.mkdir()
        target.write_text("hello")

        resolved = validate_document_path(str(target), self.documents_dir)

        self.assertEqual(resolved, target.resolve())

    def test_a_nonexistent_path_is_rejected(self):
        with self.assertRaises(DocumentPathInvalidError):
            validate_document_path(str(self.documents_dir / "missing.txt"), self.documents_dir)

    def test_a_path_outside_the_documents_directory_is_rejected(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("hello")

        with self.assertRaises(DocumentPathInvalidError):
            validate_document_path(str(outside), self.documents_dir)

    def test_a_directory_is_rejected_not_just_missing_files(self):
        subdirectory = self.documents_dir / "session1"
        subdirectory.mkdir()

        with self.assertRaises(DocumentPathInvalidError):
            validate_document_path(str(subdirectory), self.documents_dir)

    def test_a_symlink_escaping_the_documents_directory_is_rejected(self):
        outside = Path(self._tmp.name) / "secret.txt"
        outside.write_text("top secret")
        link = self.documents_dir / "innocuous.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks not supported in this environment")

        with self.assertRaises(DocumentPathInvalidError):
            validate_document_path(str(link), self.documents_dir)


class ExtractTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_text_extraction(self):
        path = self.tmp_path / "notes.txt"
        path.write_text("The migration took six weeks.")
        self.assertEqual(extract_text(path, "txt"), "The migration took six weeks.")

    def test_markdown_extraction(self):
        path = self.tmp_path / "notes.md"
        path.write_text("# Heading\n\nThe migration took six weeks.")
        text = extract_text(path, "md")
        self.assertIn("The migration took six weeks.", text)

    def test_pdf_extraction(self):
        path = self.tmp_path / "sample.pdf"
        path.write_bytes(_MINIMAL_PDF)
        text = extract_text(path, "pdf")
        self.assertIn("The migration took six weeks.", text)

    def test_docx_extraction(self):
        path = self.tmp_path / "sample.docx"
        _write_docx(path)
        text = extract_text(path, "docx")
        self.assertIn("The migration took six weeks.", text)
        self.assertIn("Auth service migrated first.", text)

    def test_extension_is_case_insensitive_and_dot_tolerant(self):
        path = self.tmp_path / "notes.txt"
        path.write_text("hello world")
        self.assertEqual(extract_text(path, "TXT"), "hello world")
        self.assertEqual(extract_text(path, ".txt"), "hello world")

    def test_unsupported_extension_is_rejected(self):
        path = self.tmp_path / "notes.exe"
        path.write_bytes(b"not a document")
        with self.assertRaises(DocumentUnsupportedError):
            extract_text(path, "exe")

    def test_oversized_file_is_rejected(self):
        path = self.tmp_path / "big.txt"
        with open(path, "wb") as f:
            f.seek(MAX_DOCUMENT_BYTES + 1)
            f.write(b"\0")
        with self.assertRaises(DocumentOversizedError):
            extract_text(path, "txt")

    def test_empty_text_file_is_rejected(self):
        path = self.tmp_path / "empty.txt"
        path.write_text("   \n  \n")
        with self.assertRaises(DocumentEmptyError):
            extract_text(path, "txt")

    def test_malformed_pdf_is_rejected(self):
        path = self.tmp_path / "broken.pdf"
        path.write_bytes(b"not a real pdf at all")
        with self.assertRaises(DocumentMalformedError):
            extract_text(path, "pdf")

    def test_malformed_pdf_diagnostics_never_reach_stderr(self):
        # pypdf logs things like "invalid pdf header: b'...'" (literal
        # bytes from the file) and "incorrect startxref pointer(...)" via
        # its own loggers on malformed input. `extraction.py` must
        # suppress those at the source so they can never reach worker
        # stderr.
        #
        # Deliberately redirects real `sys.stderr` (not just a root-logger
        # handler): when no handler is found anywhere in a logger's
        # ancestor chain, Python's `logging.lastResort` fallback writes
        # WARNING+ records *directly* to `sys.stderr`, bypassing whatever
        # handlers are or aren't configured on the root logger entirely.
        # An earlier version of this fix only set `propagate = False` on
        # the "pypdf" logger, which stops messages from reaching a
        # configured root handler but does *not* stop `lastResort` (since
        # neither "pypdf" nor its submodule loggers have handlers of their
        # own, `lastResort` still fires) — a test that only swaps out the
        # root logger's handlers would have missed that leak entirely, so
        # this redirects the real stream instead.
        import io
        import sys

        path = self.tmp_path / "broken.pdf"
        path.write_bytes(b"not a real pdf at all")

        captured = io.StringIO()
        original_stderr = sys.stderr
        sys.stderr = captured
        try:
            with self.assertRaises(DocumentMalformedError):
                extract_text(path, "pdf")
        finally:
            sys.stderr = original_stderr

        output = captured.getvalue()
        self.assertNotIn("pdf header", output)
        self.assertNotIn("startxref", output)
        self.assertNotIn("EOF marker", output)
        self.assertEqual(output, "")

    def test_malformed_docx_is_rejected(self):
        path = self.tmp_path / "broken.docx"
        path.write_bytes(b"not a real zip at all")
        with self.assertRaises(DocumentMalformedError):
            extract_text(path, "docx")

    def test_docx_missing_document_xml_is_rejected(self):
        path = self.tmp_path / "broken.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        with self.assertRaises(DocumentMalformedError):
            extract_text(path, "docx")

    def test_encrypted_pdf_is_rejected(self):
        from pypdf import PdfWriter

        path = self.tmp_path / "encrypted.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("secret-password")
        with open(path, "wb") as f:
            writer.write(f)

        with self.assertRaises(DocumentEncryptedError):
            extract_text(path, "pdf")

    def test_docx_with_only_whitespace_paragraphs_is_empty(self):
        path = self.tmp_path / "blank.docx"
        _write_docx(
            path,
            document_xml=b"""<?xml version="1.0"?>
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body><w:p></w:p></w:body>
            </w:document>""",
        )
        with self.assertRaises(DocumentEmptyError):
            extract_text(path, "docx")

    def test_docx_with_entity_expansion_is_rejected(self):
        path = self.tmp_path / "unsafe.docx"
        _write_docx(
            path,
            document_xml=b'''<?xml version="1.0"?>
            <!DOCTYPE document [<!ENTITY expansion "sensitive-content">]>
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body><w:p><w:r><w:t>&expansion;</w:t></w:r></w:p></w:body>
            </w:document>''',
        )
        with self.assertRaises(DocumentMalformedError):
            extract_text(path, "docx")

    def test_docx_with_unsafe_compression_ratio_is_rejected(self):
        path = self.tmp_path / "compressed.docx"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
            archive.writestr("word/document.xml", b"A" * (1024 * 1024))
        with self.assertRaises(DocumentOversizedError):
            extract_text(path, "docx")

    def test_docx_with_excessive_uncompressed_size_is_rejected(self):
        path = self.tmp_path / "large.docx"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
            archive.writestr("word/document.xml", b"A" * (MAX_DOCX_UNCOMPRESSED_BYTES + 1))
        with self.assertRaises(DocumentOversizedError):
            extract_text(path, "docx")

    def test_extracted_text_limit_is_enforced(self):
        path = self.tmp_path / "long.txt"
        path.write_text("x" * (MAX_EXTRACTED_TEXT_CHARACTERS + 1))
        with self.assertRaises(DocumentOversizedError):
            extract_text(path, "txt")
