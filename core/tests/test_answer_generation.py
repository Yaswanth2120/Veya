import asyncio
import unittest

from veya.conversation.answer_generation import generate_answer, parse_answer_text
from veya.llm.errors import LLMProviderError, LLMTimeoutError


class ParseAnswerTextTests(unittest.TestCase):
    def test_well_formed_response_is_parsed_into_all_three_parts(self):
        raw = (
            "ANSWER: The migration took six weeks due to staged rollout.\n"
            "POINTS:\n"
            "- Auth service migrated first\n"
            "- Staged rollout for backward compatibility\n"
            "- Validation added time but reduced risk\n"
            "CAVEAT: Exact timeline may vary by team.\n"
        )
        parsed = parse_answer_text(raw)
        self.assertEqual(parsed.short_answer, "The migration took six weeks due to staged rollout.")
        self.assertEqual(
            parsed.talking_points,
            [
                "Auth service migrated first",
                "Staged rollout for backward compatibility",
                "Validation added time but reduced risk",
            ],
        )
        self.assertEqual(parsed.caveat, "Exact timeline may vary by team.")

    def test_caveat_of_none_is_treated_as_no_caveat(self):
        raw = "ANSWER: Yes.\nPOINTS:\n- One point\nCAVEAT: none\n"
        parsed = parse_answer_text(raw)
        self.assertEqual(parsed.caveat, "")

    def test_talking_points_are_capped_at_five(self):
        raw = "ANSWER: Many reasons.\nPOINTS:\n" + "\n".join(f"- point {i}" for i in range(10))
        parsed = parse_answer_text(raw)
        self.assertEqual(len(parsed.talking_points), 5)

    def test_never_fabricates_sources_field(self):
        # parse_answer_text has no notion of "sources" at all — the
        # orchestrator always emits sources=[] regardless of what the
        # model said, so there is nothing to parse here that could leak
        # into a sources list.
        raw = "ANSWER: See the official docs at example.com.\nPOINTS:\n- a point\n"
        parsed = parse_answer_text(raw)
        self.assertFalse(hasattr(parsed, "sources"))

    def test_malformed_response_falls_back_to_the_full_natural_text_not_a_chopped_first_sentence(self):
        # A model that ignores the ANSWER:/POINTS: format entirely has
        # presumably just written natural prose — the whole thing becomes
        # the answer verbatim rather than being chopped into a "first
        # sentence" and duplicated into talking points (which produced a
        # misleadingly bullet-fragment-looking result even from a real
        # natural answer).
        raw = "This is just a plain sentence. Here is another one. And a third."
        parsed = parse_answer_text(raw)
        self.assertEqual(parsed.short_answer, raw)
        self.assertEqual(parsed.talking_points, [])
        self.assertEqual(parsed.caveat, "")

    def test_a_natural_multi_sentence_answer_is_not_truncated_to_the_first_line(self):
        raw = (
            "ANSWER: I reduced latency by profiling the YOLOv5 inference path first. "
            "The biggest gains came from converting the model to TensorRT and tuning\n"
            "batch processing, which removed the main GPU inference bottleneck.\n"
            "POINTS:\n"
            "CAVEAT: none\n"
        )
        parsed = parse_answer_text(raw)
        self.assertEqual(
            parsed.short_answer,
            "I reduced latency by profiling the YOLOv5 inference path first. "
            "The biggest gains came from converting the model to TensorRT and tuning "
            "batch processing, which removed the main GPU inference bottleneck.",
        )
        self.assertEqual(parsed.talking_points, [])

    def test_empty_response_produces_an_empty_parsed_answer(self):
        parsed = parse_answer_text("")
        self.assertEqual(parsed.short_answer, "")
        self.assertEqual(parsed.talking_points, [])


class FakeProvider:
    def __init__(self, deltas, error=None):
        self._deltas = deltas
        self._error = error
        self.availability_checked = False

    async def check_availability(self):
        self.availability_checked = True

    async def generate_stream(self, prompt, *, timeout):
        for delta in self._deltas:
            await asyncio.sleep(0)
            yield delta
        if self._error is not None:
            raise self._error


class GenerateAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_speakable_deltas_are_clean_and_accumulated_for_parsing(self):
        provider = FakeProvider(["ANSWER: Hi", ".\nPOINTS:\n", "- a point\n"])
        received = []

        async def on_speakable_delta(delta):
            received.append(delta)

        parsed = await generate_answer(provider, "prompt", on_speakable_delta=on_speakable_delta)

        # Only clean, speakable prose — never the "ANSWER:" label or the
        # POINTS: section content.
        self.assertEqual("".join(received), "Hi.\n")
        self.assertEqual(parsed.short_answer, "Hi.")
        self.assertEqual(parsed.talking_points, ["a point"])

    async def test_on_raw_delta_receives_the_providers_unfiltered_chunks(self):
        provider = FakeProvider(["<think>hmm</think>ANSWER: Hi.\n"])
        raw_received = []
        speakable_received = []

        async def on_raw_delta(delta):
            raw_received.append(delta)

        async def on_speakable_delta(delta):
            speakable_received.append(delta)

        await generate_answer(provider, "prompt", on_speakable_delta=on_speakable_delta, on_raw_delta=on_raw_delta)

        self.assertEqual("".join(raw_received), "<think>hmm</think>ANSWER: Hi.\n")
        self.assertNotIn("think", "".join(speakable_received))
        self.assertEqual("".join(speakable_received), "Hi.\n")

    async def test_on_raw_delta_is_optional(self):
        provider = FakeProvider(["ANSWER: Hi.\n"])

        async def on_speakable_delta(delta):
            pass

        # Must not raise just because on_raw_delta was omitted.
        await generate_answer(provider, "prompt", on_speakable_delta=on_speakable_delta)

    async def test_provider_error_propagates_to_the_caller(self):
        provider = FakeProvider(["partial"], error=LLMProviderError("boom"))

        async def on_speakable_delta(delta):
            pass

        with self.assertRaises(LLMProviderError):
            await generate_answer(provider, "prompt", on_speakable_delta=on_speakable_delta)

    async def test_provider_timeout_propagates_to_the_caller(self):
        provider = FakeProvider([], error=LLMTimeoutError("timed out"))

        async def on_speakable_delta(delta):
            pass

        with self.assertRaises(LLMTimeoutError):
            await generate_answer(provider, "prompt", on_speakable_delta=on_speakable_delta)

    async def test_cancellation_stops_delivering_deltas(self):
        received = []

        class SlowProvider:
            async def generate_stream(self, prompt, *, timeout):
                for i in range(1000):
                    await asyncio.sleep(0.01)
                    yield f"chunk-{i}"

        async def on_speakable_delta(delta):
            received.append(delta)

        task = asyncio.create_task(generate_answer(SlowProvider(), "prompt", on_speakable_delta=on_speakable_delta))
        await asyncio.sleep(0.05)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        # A handful of chunks may have been delivered before cancellation
        # landed, but nowhere near all 1000 — proves cancellation actually
        # stopped the stream rather than running to completion.
        self.assertLess(len(received), 1000)
