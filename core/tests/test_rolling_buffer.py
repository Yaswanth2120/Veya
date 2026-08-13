import unittest

from veya.transcription.rolling_buffer import RollingWindowBuffer, RollingWindowConfig


def make_pcm(num_samples: int, fill: int = 1) -> bytes:
    # 16-bit PCM, little-endian, all samples the same value for easy
    # byte-count assertions.
    return (fill.to_bytes(2, "little", signed=False)) * num_samples


class RollingWindowConfigTests(unittest.TestCase):
    def test_non_positive_window_seconds_is_rejected(self):
        with self.assertRaises(ValueError):
            RollingWindowConfig(window_seconds=0)

    def test_overlap_must_be_smaller_than_window(self):
        with self.assertRaises(ValueError):
            RollingWindowConfig(window_seconds=1.0, overlap_seconds=1.0)

    def test_negative_overlap_is_rejected(self):
        with self.assertRaises(ValueError):
            RollingWindowConfig(overlap_seconds=-0.1)


class RollingWindowBufferTests(unittest.TestCase):
    def test_no_window_returned_until_enough_audio_has_accumulated(self):
        config = RollingWindowConfig(sample_rate_hz=100, window_seconds=1.0, overlap_seconds=0.2)
        buffer = RollingWindowBuffer(config)

        # window is 100 samples * 2 bytes = 200 bytes.
        self.assertIsNone(buffer.add_chunk(make_pcm(50)))
        self.assertIsNone(buffer.add_chunk(make_pcm(40)))

    def test_window_is_returned_once_enough_audio_accumulates(self):
        config = RollingWindowConfig(sample_rate_hz=100, window_seconds=1.0, overlap_seconds=0.2)
        buffer = RollingWindowBuffer(config)

        buffer.add_chunk(make_pcm(60))
        window = buffer.add_chunk(make_pcm(60))

        self.assertIsNotNone(window)
        self.assertEqual(len(window), 200)  # 100 samples * 2 bytes

    def test_overlap_tail_is_retained_for_the_next_window(self):
        config = RollingWindowConfig(sample_rate_hz=100, window_seconds=1.0, overlap_seconds=0.2)
        buffer = RollingWindowBuffer(config)

        first_window = buffer.add_chunk(make_pcm(100))  # exactly one window: 200 bytes
        self.assertIsNotNone(first_window)

        # overlap is 0.2s * 100Hz * 2 bytes = 40 bytes retained, so 160
        # more bytes (80 samples) are needed to complete the next window.
        self.assertIsNone(buffer.add_chunk(make_pcm(20)))  # 40 bytes: 40 + 40 = 80 < 200
        second_window = buffer.add_chunk(make_pcm(60))  # 120 bytes: 80 + 120 = 200
        self.assertIsNotNone(second_window)
        self.assertEqual(len(second_window), 200)

    def test_bounded_for_realistic_chunk_sizes_smaller_than_one_window(self):
        # Real callers (see MAX_AUDIO_CHUNK_BYTES in ipc/dispatcher.py)
        # always send chunks well under one window's duration, so feeding
        # many small chunks should never let the buffer grow past one
        # window's worth of bytes plus the latest chunk.
        config = RollingWindowConfig(sample_rate_hz=100, window_seconds=1.0, overlap_seconds=0.2)
        buffer = RollingWindowBuffer(config)

        for _ in range(50):
            window = buffer.add_chunk(make_pcm(10))  # 20 bytes per chunk
            self.assertLessEqual(len(buffer._buffer), 200 + 20)
            if window is not None:
                self.assertEqual(len(window), 200)

    def test_flush_returns_none_when_buffer_is_empty(self):
        buffer = RollingWindowBuffer()
        self.assertIsNone(buffer.flush())

    def test_flush_returns_and_clears_partial_remaining_audio(self):
        config = RollingWindowConfig(sample_rate_hz=100, window_seconds=1.0, overlap_seconds=0.2)
        buffer = RollingWindowBuffer(config)
        buffer.add_chunk(make_pcm(30))

        remaining = buffer.flush()

        self.assertEqual(len(remaining), 60)
        self.assertIsNone(buffer.flush())
