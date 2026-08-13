"""Veya Python AI worker.

Section 6 scope only: a long-running subprocess that speaks the JSON
Lines IPC protocol (see `veya.ipc`) and drives a deterministic mocked
live-session pipeline (see `veya.mock.live_feed`). No real audio,
transcription, retrieval, or LLM calls happen here yet.
"""

__version__ = "0.1.0"
