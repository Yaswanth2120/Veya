"""Question detection and local-LLM answer generation for the real
transcription pipeline. Consumes only `transcript.final` text (never
partials) — see `question_detector.py`/`answer_generation.py`/
`orchestrator.py`. Kept independent of `transcription/` and `llm/`: this
package depends on both, neither depends back on it.
"""
