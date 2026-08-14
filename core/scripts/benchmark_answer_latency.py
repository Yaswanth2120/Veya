"""Local, repeatable benchmark for first-visible-answer latency.

Runs real questions through the actual `ConversationOrchestrator` ->
`render_prompt` -> `OllamaProvider.generate_stream` pipeline (the same
code path a live interview session uses), against whatever Ollama model
is configured (`VEYA_OLLAMA_MODEL`, defaulting to the provider's default)
and a fixed resume/question fixture below. Requires a real, reachable
local Ollama instance with that model already pulled — this is not a
mock/fake-timed benchmark.

Usage:
    python3 core/scripts/benchmark_answer_latency.py [--runs N] [--model NAME]

Reports median and p95 for three distinct measurements (Section 18 —
raw model activity is never conflated with a real usable answer):
  - first raw provider token (diagnostics only — may be hidden reasoning,
    e.g. a `<think>` block; never what a user would consider an answer)
  - first usable answer: the first clean, speakable character
    `SpeakableAnswerStream` actually emits — this is the real
    "first visible answer" number
  - total completion (stabilized question -> answer.completed)

This does not simulate microphone/VAD/turn-assembly latency, and it does
not include Swift's own render time (a separate, client-side
measurement — see `ConversationState.AnswerTimingSample.firstRenderedAt`)
— it starts the clock at the same point the real orchestrator does
(`stabilized_at`, right after a turn is classified as an answer
request), which is what `_start_answer_generation` already measures
from in production when `VEYA_ANSWER_TIMING_DIAGNOSTICS=1`.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veya.conversation.models import SessionContext
from veya.conversation.orchestrator import ConversationOrchestrator
from veya.knowledge.models import DocumentChunk, RetrievedChunk
from veya.llm.errors import LLMUnavailableError
from veya.llm.ollama_provider import OllamaConfig, OllamaProvider

# A fixed fixture, not read from any real user's data — kept in-source so
# the benchmark is exactly repeatable across runs/machines.
FIXTURE_SESSION_CONTEXT = SessionContext(
    title="Backend Engineer Interview",
    company="Acme Corp",
    role_or_topic="Senior Backend Engineer",
    description="Interviewing for a senior backend role focused on distributed systems.",
    notes="",
    preferred_answer_style="",
    preferred_programming_language="",
    custom_instructions="",
    session_type="interviewPractice",
)

FIXTURE_RESUME_TEXT = (
    "Jordan Lee — Senior Backend Engineer. 7 years building distributed systems in Python and Go. "
    "Led the migration of a monolithic order-processing service to an event-driven microservices "
    "architecture at Acme Logistics, cutting p99 latency from 800ms to 120ms and reducing on-call "
    "incidents by 60%. Built a real-time inference pipeline serving 40M requests/day on Kubernetes, "
    "optimizing batching and caching to cut inference cost by 35%. Mentored 4 junior engineers. "
    "Comfortable with PostgreSQL, Kafka, Redis, and Terraform."
)

FIXTURE_QUESTIONS = [
    "Tell me about yourself.",
    "Walk me through a time you optimized a slow system.",
    "What did you mean by optimizing the inference pipeline?",
]


_FIXTURE_CHUNK = DocumentChunk(
    chunk_id="benchmark-resume-0",
    document_id="benchmark-resume",
    session_id="benchmark",
    file_name="resume.txt",
    chunk_index=0,
    text=FIXTURE_RESUME_TEXT,
    excerpt=FIXTURE_RESUME_TEXT[:240],
    char_start=0,
    char_end=len(FIXTURE_RESUME_TEXT),
)


class _FixedContextRetriever:
    """A minimal stand-in for `KnowledgeRetriever` that always "retrieves"
    the same fixed resume chunk — avoids depending on a real embedding
    model just to exercise the benchmark's prompt shape, while still
    using real `RetrievedChunk`/`DocumentChunk` objects so
    `chunk_sources()` behaves exactly as it does in production."""

    def build_context_block(self, retrieved) -> str:
        return f"Resume:\n{FIXTURE_RESUME_TEXT}"

    async def retrieve(self, session_id: str, query_text: str):
        return [RetrievedChunk(chunk=_FIXTURE_CHUNK, score=1.0)]


async def _run_one(provider: OllamaProvider, question: str) -> dict:
    done = asyncio.Event()
    timing: dict = {}

    async def capture_timing(name: str, data: dict) -> None:
        if name == "answer.timing":
            timing.update(data)
            done.set()

    orchestrator = ConversationOrchestrator(
        session_id="benchmark",
        session_context=FIXTURE_SESSION_CONTEXT,
        emit_event=capture_timing,
        llm_provider=provider,
        retriever=_FixedContextRetriever(),
        emit_timing_diagnostics=True,
    )

    await orchestrator.handle_final_transcript(question, 0.0, 2.0)
    await orchestrator.handle_turn_boundary(2.0)
    await asyncio.wait_for(done.wait(), timeout=90.0)
    await orchestrator.close()

    stabilized_at = timing.get("stabilized_at")

    def _latency(key: str) -> float:
        value = timing.get(key)
        return (value - stabilized_at) if value and stabilized_at else None

    return {
        "question": question,
        "raw_token_latency": _latency("first_raw_token_at"),
        "usable_answer_latency": _latency("first_speakable_char_at"),
        "total_latency": _latency("completed_at"),
    }


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="Repetitions per question (default: 5)")
    parser.add_argument("--model", type=str, default=None, help="Ollama model name (default: $VEYA_OLLAMA_MODEL or provider default)")
    args = parser.parse_args()

    config = OllamaConfig.resolve_from_env()
    if args.model:
        config = OllamaConfig(base_url=config.base_url, model=args.model)
    provider = OllamaProvider(config)

    try:
        await provider.check_availability()
    except LLMUnavailableError as exc:
        print(f"Ollama unavailable: {exc}. This benchmark requires a real, reachable local Ollama with the model pulled.")
        return 1

    print(f"Model: {config.model}   Ollama: {config.base_url}   Runs per question: {args.runs}\n")

    raw_token_latencies: list[float] = []
    usable_answer_latencies: list[float] = []
    total_latencies: list[float] = []

    for question in FIXTURE_QUESTIONS:
        for run in range(args.runs):
            result = await _run_one(provider, question)
            rt = result["raw_token_latency"]
            ut = result["usable_answer_latency"]
            tt = result["total_latency"]
            if rt is not None:
                raw_token_latencies.append(rt)
            if ut is not None:
                usable_answer_latencies.append(ut)
            if tt is not None:
                total_latencies.append(tt)
            if ut is not None and tt is not None:
                print(f"  [{question[:40]:<40}] run {run + 1}/{args.runs}: raw_token={rt:.2f}s usable_answer={ut:.2f}s total={tt:.2f}s")
            else:
                print(f"  [{question[:40]:<40}] run {run + 1}/{args.runs}: FAILED (no usable answer text produced)")

    if not usable_answer_latencies:
        print("\nNo successful runs — nothing to report.")
        return 1

    print("\n--- Results ---")
    print("Raw first provider token (diagnostics only — may be hidden reasoning, never an answer):")
    if raw_token_latencies:
        print(f"  median: {statistics.median(raw_token_latencies):.2f}s   p95: {_percentile(raw_token_latencies, 95):.2f}s")
    else:
        print("  (no raw tokens captured)")
    print("First USABLE answer (first clean, speakable character — the real 'first visible answer'):")
    print(f"  median: {statistics.median(usable_answer_latencies):.2f}s   p95: {_percentile(usable_answer_latencies, 95):.2f}s")
    print("Total completion (stabilized question -> answer.completed):")
    print(f"  median: {statistics.median(total_latencies):.2f}s   p95: {_percentile(total_latencies, 95):.2f}s")
    print(f"\nSample size: {len(usable_answer_latencies)} successful runs across {len(FIXTURE_QUESTIONS)} fixed questions.")
    print("(Does not include Swift's own render time — a separate, client-side measurement.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
