"""Local Ollama-backed `LLMProvider`. Talks to Ollama's local HTTP API
(`/api/tags`, `/api/generate`) using only the standard library (`urllib`)
— no `requests` dependency, matching the rest of this codebase. Never
silently calls a remote/cloud endpoint: `resolve_from_env()` enforces that
`VEYA_OLLAMA_URL` resolves to a loopback host unless `VEYA_OLLAMA_ALLOW_REMOTE=1`
is explicitly set — a config-time check, not a network-egress firewall,
but enough that a stray/misconfigured `VEYA_OLLAMA_URL` can't silently
start sending prompt/transcript content off-machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

from .errors import LLMError, LLMProviderError, LLMTimeoutError, LLMUnavailableError

DEFAULT_OLLAMA_URL = "http://localhost:11434"
# A sensible default only — not a guarantee this model is pulled locally.
# `check_availability()` verifies the configured model actually exists
# before answer intelligence is reported available.
DEFAULT_OLLAMA_MODEL = "llama3.2"

_LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

_STREAM_DONE = object()


def _is_loopback_url(url: str) -> bool:
    hostname = urlparse(url).hostname
    if hostname is None:
        return False
    hostname = hostname.lower()
    return hostname in _LOOPBACK_HOSTNAMES or hostname.startswith("127.")


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    model: str
    connect_timeout_seconds: float = 5.0

    @staticmethod
    def resolve_from_env() -> "OllamaConfig":
        base_url = (os.environ.get("VEYA_OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
        model = os.environ.get("VEYA_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
        allow_remote = os.environ.get("VEYA_OLLAMA_ALLOW_REMOTE") == "1"

        if not allow_remote and not _is_loopback_url(base_url):
            raise LLMUnavailableError(
                "VEYA_OLLAMA_URL must point at a local (loopback) Ollama instance. "
                "Set VEYA_OLLAMA_ALLOW_REMOTE=1 to explicitly allow a non-local endpoint."
            )

        return OllamaConfig(base_url=base_url, model=model)


class OllamaProvider:
    """Implements `llm.provider.LLMProvider`. `urllib` calls run on a
    background thread (via `run_in_executor`/a producer thread) so a slow
    or hung Ollama instance never blocks the worker's asyncio event loop."""

    def __init__(self, config: Optional[OllamaConfig] = None) -> None:
        self._config = config or OllamaConfig.resolve_from_env()

    async def describe_status(self) -> dict:
        """Never raises — a diagnostic for Swift's Local AI status panel,
        not an availability gate. Reports whether Ollama is reachable at
        all, which model is configured, whether that exact model is
        installed, and what *is* installed, so the app can show an
        actionable "run `ollama pull <model>`" instead of the current
        opaque "No local LLM was available." Loopback enforcement already
        happened in `OllamaConfig.resolve_from_env()` — this never talks
        to a non-loopback host either."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._describe_status_blocking)
        except Exception as exc:  # noqa: BLE001 - a status check must never raise/crash the RPC
            return {
                "reachable": False,
                "base_url": self._config.base_url,
                "configured_model": self._config.model,
                "model_installed": False,
                "available_models": [],
                "error": type(exc).__name__,
            }

    def _describe_status_blocking(self) -> dict:
        request = urllib.request.Request(f"{self._config.base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._config.connect_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {
                "reachable": False,
                "base_url": self._config.base_url,
                "configured_model": self._config.model,
                "model_installed": False,
                "available_models": [],
                "error": "unreachable",
            }

        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = sorted({model.get("name") for model in models if isinstance(model, dict) and model.get("name")})
        installed = self._config.model in names or f"{self._config.model}:latest" in names
        return {
            "reachable": True,
            "base_url": self._config.base_url,
            "configured_model": self._config.model,
            "model_installed": installed,
            "available_models": names,
            "error": "",
        }

    async def check_availability(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._check_availability_blocking)
        except LLMUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert anything unexpected into the typed error
            raise LLMUnavailableError(f"Ollama availability check failed ({type(exc).__name__}).") from exc

    def _check_availability_blocking(self) -> None:
        request = urllib.request.Request(f"{self._config.base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._config.connect_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise LLMUnavailableError("Could not reach the configured Ollama URL.") from exc
        except (TimeoutError, OSError) as exc:
            raise LLMUnavailableError("Timed out reaching the configured Ollama URL.") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LLMUnavailableError("Ollama returned a malformed response.") from exc

        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = {model.get("name") for model in models if isinstance(model, dict)}
        if self._config.model not in names and f"{self._config.model}:latest" not in names:
            raise LLMUnavailableError("The configured Ollama model is not available locally.")

    async def generate_stream(self, prompt: str, *, timeout: float) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        chunk_queue: "queue.Queue[object]" = queue.Queue()

        def produce() -> None:
            try:
                request = urllib.request.Request(
                    f"{self._config.base_url}/api/generate",
                    data=json.dumps({"model": self._config.model, "prompt": prompt, "stream": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    for raw_line in response:
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        payload = json.loads(line)
                        delta = payload.get("response", "")
                        if delta:
                            chunk_queue.put(delta)
                        if payload.get("done"):
                            break
            except urllib.error.URLError as exc:
                chunk_queue.put(LLMUnavailableError("Could not reach the configured Ollama URL."))
            except (TimeoutError, OSError) as exc:
                chunk_queue.put(LLMTimeoutError("Ollama did not respond within the configured timeout."))
            except json.JSONDecodeError:
                chunk_queue.put(LLMProviderError("Ollama returned a malformed streaming response."))
            except Exception as exc:  # noqa: BLE001 - never let a raw exception escape the thread
                chunk_queue.put(LLMProviderError(f"Unhandled {type(exc).__name__} while streaming from Ollama."))
            finally:
                chunk_queue.put(_STREAM_DONE)

        # Daemonized: if this async generator is abandoned/cancelled
        # before the stream naturally ends, the producer thread is not
        # explicitly joined — it exits on its own once its current
        # blocking urllib read unblocks (data, EOF, or its own `timeout`),
        # and being a daemon thread means it never blocks process exit.
        producer_thread = threading.Thread(target=produce, daemon=True)
        producer_thread.start()

        while True:
            item = await loop.run_in_executor(None, chunk_queue.get)
            if item is _STREAM_DONE:
                return
            if isinstance(item, LLMError):
                raise item
            yield item


def default_ollama_provider_factory() -> OllamaProvider:
    """The production `WorkerContext.llm_provider_factory` — resolves
    configuration fresh on every call (not cached at import time) so an
    env-var change between worker starts is picked up, and so tests can
    monkeypatch the environment freely."""
    return OllamaProvider()
