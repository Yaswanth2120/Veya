#!/usr/bin/env python3
"""Opt-in real-worker + local embeddings + rented GPU acceptance check.

Uses synthetic data only. Optional --audio WAV replays mono 16kHz PCM
through actual local Whisper before asking the GPU. Requires the existing
SSH tunnel, Ollama nomic-embed-text, and (for audio) Whisper env settings.
"""
import argparse
import asyncio
import base64
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import time
import uuid
import wave
import platform
from datetime import datetime, timezone


async def main(audio, worker_directory=None, long_context=False, meeting_track=False, json_report=None):
    with tempfile.TemporaryDirectory(prefix="veya-gpu-smoke-") as tmp:
        root = Path(tmp)
        docs = root / "documents"
        docs.mkdir()
        resume = docs / "synthetic-resume.txt"
        resume.write_text("Jordan built Atlas, an inference service. Jordan reduced inference cost by 35% by batching requests and caching repeated results. Jordan used Python and Redis.")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", VEYA_LLM_BACKEND="vast", VEYA_DOCUMENTS_DIRECTORY=str(docs),
            VEYA_KNOWLEDGE_INDEX_DIRECTORY=str(root / "index"), VEYA_MEMORY_DATABASE_PATH=str(root / "memory.sqlite"),
            VEYA_REPORT_STORE_DIRECTORY=str(root / "reports"), VEYA_ANSWER_TIMING_DIAGNOSTICS="1")
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "veya",
            cwd=worker_directory or Path(__file__).resolve().parents[1], env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        pending = {}
        events = []

        async def read():
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message.get("type") == "event":
                    events.append((message["event"], message["data"], time.monotonic()))
                elif message.get("id") in pending:
                    future = pending.pop(message["id"])
                    if not future.done():
                        future.set_result(message)

        reader = asyncio.create_task(read())

        async def call(method, **params):
            ident = str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            pending[ident] = future
            process.stdin.write((json.dumps(dict(version=1, type="request", id=ident, method=method, params=params)) + "\n").encode())
            await process.stdin.drain()
            try:
                message = await asyncio.wait_for(future, 20)
            finally:
                pending.pop(ident, None)
            if message["type"] == "error":
                raise RuntimeError(str(message["error"]))
            return message["result"]

        async def wait_event(names, after=0, timeout=45):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for event in events[after:]:
                    if event[0] in names:
                        return event
                await asyncio.sleep(.02)
            raise TimeoutError("Expected event did not arrive: " + str(names))

        try:
            await wait_event({"worker.ready"})
            status = await call("system.llm_status")
            assert status.get("provider") == "vast" and status["model_installed"], status
            print("GPU status: ready; model:", status["configured_model"], flush=True)
            session_id = str(uuid.uuid4())
            await call("session.start", session_id=session_id, title="Synthetic interview practice",
                session_description=("Synthetic practice context. " * 400) if long_context else "")
            await call("knowledge.ingest", session_id=session_id, document_id="synthetic-resume", file_name=resume.name,
                file_extension="txt", file_path=str(resume))
            ingestion = await wait_event({"knowledge.ingestion_completed", "knowledge.ingestion_failed"})
            assert ingestion[0] == "knowledge.ingestion_completed", ingestion[1].get("reason", "ingestion failed")
            print("Local document embedding and indexing: passed", flush=True)
            mark = len(events)
            started = time.monotonic()
            if audio:
                if meeting_track:
                    await call("conversation.start_text", session_id=session_id)
                result = await call("transcription.start_meeting_audio" if meeting_track else "transcription.start",
                    session_id=session_id, sample_rate_hz=16000, channels=1, encoding="pcm_s16le")
                assert result.get("answer_intelligence_available", meeting_track), result
                print("ASR provider:", result["asr_provider"], flush=True)
                with wave.open(str(audio), "rb") as wav:
                    assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
                    pcm = wav.readframes(wav.getnframes()) + bytes(16000 * 2 * 3)
                chunk_size = 8000
                for sequence, offset in enumerate(range(0, len(pcm), chunk_size)):
                    chunk = pcm[offset:offset + chunk_size]
                    await call("transcription.meeting_audio_chunk" if meeting_track else "transcription.audio_chunk", session_id=session_id, sequence=sequence,
                        started_at_seconds=offset / 32000, duration_seconds=len(chunk) / 32000,
                        audio_base64=base64.b64encode(chunk).decode())
                    await asyncio.sleep(len(chunk) / 32000)
                await call("transcription.stop_meeting_audio" if meeting_track else "transcription.stop", session_id=session_id)
            else:
                await call("answer.ask", session_id=session_id, text="How did I reduce inference cost in Atlas?")
            _, answer, completed = await wait_event({"answer.completed"}, after=mark)
            assert not answer.get("is_failed"), answer.get("answer_text")
            assert any(s["document_id"] == "synthetic-resume" for s in answer["sources"]), "No retrieved source"
            print("Synthetic answer:", answer["answer_text"], flush=True)
            if "verified detail" in answer["answer_text"]:
                print("Synthetic streamed text before guard:", "".join(d.get("delta", "") for n, d, _ in events[mark:] if n == "answer.speakable_delta"), flush=True)
                print("Synthetic detected questions:", [d.get("text") for n, d, _ in events[mark:] if n == "question.detected"], flush=True)
            assert re.search(r"\b35\s*(?:%|percent\b)", answer["answer_text"], re.I), "Expected grounded metric missing"
            assert "batch" in answer["answer_text"].lower() and "cach" in answer["answer_text"].lower(), "Expected supporting techniques missing"
            deltas = [(d, t) for n, d, t in events[mark:] if n == "answer.speakable_delta"]
            assert deltas, "No usable streamed answer"
            print("First usable text seconds:", round(deltas[0][1] - started, 3), flush=True)
            print("Total answer seconds:", round(completed - started, 3), flush=True)
            print("PASS: real worker input → local retrieval → GPU → clean answer events", flush=True)
            if json_report:
                report = {
                    "schema_version": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "fixture": "synthetic-atlas-cost-reduction-v1", "synthetic_data_only": True,
                    "model": status["configured_model"], "context_window": status.get("context_window"),
                    "input_mode": ("meeting_track_replay" if meeting_track else "microphone_track_replay") if audio else "typed",
                    "long_context": long_context, "worker": "packaged" if worker_directory else "checkout",
                    "asr_provider": result["asr_provider"] if audio else None,
                    "client": {"os": platform.system(), "os_version": platform.mac_ver()[0],
                               "architecture": platform.machine(), "python": platform.python_version()},
                    "measurements_seconds": {"first_usable_text": round(deltas[0][1] - started, 3),
                                             "answer_completed": round(completed - started, 3)},
                    "checks": {"local_document_indexed": True, "retrieved_source": True,
                               "expected_metric_and_techniques": True, "streamed_text": True, "completed_without_failure": True},
                    "interpretation": "Single synthetic acceptance sample; not an accuracy evaluation, percentile benchmark, or live meeting capture test.",
                }
                json_report.parent.mkdir(parents=True, exist_ok=True)
                json_report.write_text(json.dumps(report, indent=2) + "\n")
        finally:
            if process.returncode is None:
                try:
                    await call("worker.shutdown")
                    await asyncio.wait_for(process.wait(), 15)
                except Exception:
                    process.terminate()
                    await process.wait()
            await reader


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--worker-directory", type=Path, help="Test the worker included in an app bundle")
    parser.add_argument("--long-context", action="store_true", help="Exercise exact token budgeting with larger synthetic notes")
    parser.add_argument("--meeting-track", action="store_true", help="Replay audio on the meeting track without starting a microphone track")
    parser.add_argument("--json-report", type=Path, help="Save a successful synthetic acceptance report")
    args = parser.parse_args()
    if args.meeting_track and not args.audio:
        parser.error("--meeting-track requires --audio")
    asyncio.run(main(args.audio, args.worker_directory, args.long_context, args.meeting_track, args.json_report))
