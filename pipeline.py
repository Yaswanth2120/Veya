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
