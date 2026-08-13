"""Real (non-mocked) transcription support: a bounded rolling-window buffer
feeding a local Whisper engine, behind a small `TranscriptionEngine`
abstraction so it can be faked in tests. See `docs/REALTIME_TRANSCRIPTION.md`
for the architecture and known limitations.
"""
