import unittest

from veya.conversation.turn_assembler import TurnAssembler


class TurnAssemblerTests(unittest.TestCase):
    def test_a_boundary_that_already_arrived_finalizes_immediately(self):
        assembler = TurnAssembler()
        assembler.add_fragment("Explain the caching layer", 0.0, 4.0)
        finalized = assembler.request_finalize_at(4.0)
        self.assertEqual(finalized, "Explain the caching layer")

    def test_a_boundary_requested_before_its_fragment_arrives_finalizes_on_arrival(self):
        assembler = TurnAssembler()
        assembler.add_fragment("Explain the caching layer", 0.0, 4.0)
        self.assertIsNone(assembler.request_finalize_at(8.0))  # not covered yet
        finalized = assembler.add_fragment("and its eviction policy", 4.0, 8.0)
        self.assertEqual(finalized, "Explain the caching layer and its eviction policy")

    def test_multiple_fragments_before_any_boundary_stay_open(self):
        assembler = TurnAssembler()
        self.assertIsNone(assembler.add_fragment("Q1, explain the algorithm", 0.0, 4.0))
        self.assertIsNone(assembler.add_fragment("and its inputs", 4.0, 8.0))
        self.assertIsNone(assembler.add_fragment("would you use", 8.0, 9.0))
        finalized = assembler.request_finalize_at(9.0)
        self.assertEqual(finalized, "Q1, explain the algorithm and its inputs would you use")

    def test_overlap_between_fragments_is_deduplicated(self):
        assembler = TurnAssembler()
        assembler.add_fragment("we moved the auth service first", 0.0, 4.0)
        assembler.add_fragment("auth service first since everything depended on it", 4.0, 8.0)
        finalized = assembler.request_finalize_at(8.0)
        self.assertEqual(finalized, "we moved the auth service first since everything depended on it")

    def test_flush_finalizes_whatever_is_buffered(self):
        assembler = TurnAssembler()
        assembler.add_fragment("Tell me about yourself", 0.0, 3.0)
        self.assertEqual(assembler.flush(), "Tell me about yourself")

    def test_flush_with_nothing_buffered_returns_none(self):
        assembler = TurnAssembler()
        self.assertIsNone(assembler.flush())

    def test_finalizing_clears_state_for_the_next_turn(self):
        assembler = TurnAssembler()
        assembler.add_fragment("first turn", 0.0, 2.0)
        assembler.request_finalize_at(2.0)
        self.assertFalse(assembler.has_pending_content)
        assembler.add_fragment("second turn", 2.0, 4.0)
        self.assertEqual(assembler.flush(), "second turn")

    def test_an_empty_fragment_does_not_get_appended(self):
        assembler = TurnAssembler()
        assembler.add_fragment("   ", 0.0, 1.0)
        self.assertFalse(assembler.has_pending_content)

    def test_very_long_turns_are_bounded(self):
        assembler = TurnAssembler()
        for i in range(200):
            assembler.add_fragment(f"segment number {i} with some additional padding text", float(i), float(i + 1))
        finalized = assembler.flush()
        self.assertIsNotNone(finalized)
        self.assertLessEqual(len(finalized), 4200)  # bounded, not literally every one of 200 segments verbatim
