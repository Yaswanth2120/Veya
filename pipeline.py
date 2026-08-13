# LEGACY EXPERIMENTATION CODE — NOT INTEGRATED WITH THE APP.
#
# This was an early, standalone proof-of-concept (whisper.cpp + a local
# Ollama model) from before Section 6 introduced the real Swift↔Python
# bridge. The Veya app does not invoke this file, does not use Ollama,
# and does not use Whisper.
#
# The actual Python worker Swift talks to lives in `core/veya/` and is
# started by `Sources/Veya/Bridge/PythonWorkerManager.swift` — see
# `docs/IPC_PROTOCOL.md`. To smoke-test that worker directly, run:
#
#   cd core && python3 -m veya
#
# and pipe it JSON Lines requests, or see docs/IPC_PROTOCOL.md's
# troubleshooting section for a scripted example.
#
# Kept here only as a labeled historical artifact; requires `requests`,
# a local Ollama install, and a built whisper.cpp binary to actually run.
import subprocess
import requests

WHISPER_BIN = "whisper.cpp/build/bin/whisper-cli"
WHISPER_MODEL = "whisper.cpp/models/ggml-base.en.bin"
AUDIO_FILE = "whisper.cpp/samples/jfk.wav"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen3:1.7b"


def transcribe(audio_path: str) -> str:
    result = subprocess.run(
        [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", audio_path, "-nt", "-np"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def ask_ollama(prompt: str) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
    )
    response.raise_for_status()
    return response.json()["response"].strip()


def main():
    transcript = transcribe(AUDIO_FILE)
    print("[TRANSCRIPT]", transcript)

    answer = ask_ollama(transcript)
    print("[ANSWER]", answer)


if __name__ == "__main__":
    main()
