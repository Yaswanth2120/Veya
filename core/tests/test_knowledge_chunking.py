import unittest

from veya.knowledge.chunking import chunk_text
from veya.knowledge.models import ChunkingConfig


class ChunkTextTests(unittest.TestCase):
    def test_empty_text_produces_no_chunks(self):
        self.assertEqual(chunk_text("", "doc1", "sess1", "notes.txt"), [])

    def test_text_shorter_than_target_size_produces_one_chunk(self):
        chunks = chunk_text("short text", "doc1", "sess1", "notes.txt")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "short text")
        self.assertEqual(chunks[0].char_start, 0)
        self.assertEqual(chunks[0].char_end, len("short text"))

    def test_chunking_preserves_document_order_and_covers_the_full_text(self):
        config = ChunkingConfig(target_chunk_characters=10, overlap_characters=2)
        text = "abcdefghijklmnopqrstuvwxyz"
        chunks = chunk_text(text, "doc1", "sess1", "notes.txt", config)

        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        # Reconstructing from char_start/char_end (ignoring overlap
        # duplication) covers the whole original text in order.
        self.assertEqual(chunks[0].char_start, 0)
        self.assertEqual(chunks[-1].char_end, len(text))
        for earlier, later in zip(chunks, chunks[1:]):
            self.assertLess(earlier.char_start, later.char_start)

    def test_overlap_between_adjacent_chunks(self):
        config = ChunkingConfig(target_chunk_characters=10, overlap_characters=3)
        text = "abcdefghijklmnopqrstuvwxyz"
        chunks = chunk_text(text, "doc1", "sess1", "notes.txt", config)

        first, second = chunks[0], chunks[1]
        overlap_text = text[second.char_start : first.char_end]
        self.assertEqual(overlap_text, first.text[-3:])
        self.assertEqual(second.text[: len(overlap_text)], overlap_text)

    def test_chunk_ids_are_stable_across_reingestion_of_the_same_document(self):
        text = "a" * 50
        config = ChunkingConfig(target_chunk_characters=10, overlap_characters=2)
        first_pass = chunk_text(text, "doc1", "sess1", "notes.txt", config)
        second_pass = chunk_text(text, "doc1", "sess1", "notes.txt", config)

        self.assertEqual([c.chunk_id for c in first_pass], [c.chunk_id for c in second_pass])

    def test_chunk_ids_differ_across_different_documents(self):
        text = "a" * 50
        config = ChunkingConfig(target_chunk_characters=10, overlap_characters=2)
        doc1_chunks = chunk_text(text, "doc1", "sess1", "notes.txt", config)
        doc2_chunks = chunk_text(text, "doc2", "sess1", "notes.txt", config)

        self.assertTrue(set(c.chunk_id for c in doc1_chunks).isdisjoint(c.chunk_id for c in doc2_chunks))

    def test_chunks_carry_document_id_file_name_and_session_id(self):
        chunks = chunk_text("hello world", "doc1", "sess1", "notes.txt")
        self.assertEqual(chunks[0].document_id, "doc1")
        self.assertEqual(chunks[0].session_id, "sess1")
        self.assertEqual(chunks[0].file_name, "notes.txt")

    def test_excerpt_is_bounded_by_max_excerpt_length(self):
        config = ChunkingConfig(target_chunk_characters=100, overlap_characters=10, max_excerpt_length=20)
        text = "x" * 100
        chunks = chunk_text(text, "doc1", "sess1", "notes.txt", config)
        self.assertEqual(len(chunks[0].excerpt), 20)

    def test_invalid_config_is_rejected(self):
        with self.assertRaises(ValueError):
            ChunkingConfig(target_chunk_characters=0)
        with self.assertRaises(ValueError):
            ChunkingConfig(target_chunk_characters=10, overlap_characters=10)
        with self.assertRaises(ValueError):
            ChunkingConfig(target_chunk_characters=10, overlap_characters=-1)
