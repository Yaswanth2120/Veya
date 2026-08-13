"""The `EmbeddingProvider` abstraction and its implementations: a real
local-Ollama-backed provider (`OllamaEmbeddingProvider`), and a
deterministic fake for tests (`FakeEmbeddingProvider`) that needs no
model, no I/O, and no event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Protocol

from ..llm.ollama_provider import DEFAULT_OLLAMA_URL, _is_loopback_url
from .errors import EmbeddingUnavailableError

# A sensible default only — not a guarantee this model is pulled locally.
# `check_availability()` verifies the configured model actually exists.
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"


class EmbeddingProvider(Protocol):
    async def check_availability(self) -> None:
        """Raises `EmbeddingUnavailableError` if this provider can't
        currently serve requests. Never logs prompt/document content."""
        ...

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Returns one embedding vector per input text, same order.
        Never logs the input texts."""
        ...


class FakeEmbeddingProvider:
    """Deterministic, hash-based bag-of-words pseudo-embeddings — no real
    model, no I/O, safe for CI. Texts sharing more words land closer
    together under cosine similarity, which is enough to exercise real
    retrieval/ranking behavior in tests without a real embedding model."""

    def __init__(self, dimensions: int = 32) -> None:
        self.dimensions = dimensions

    async def check_availability(self) -> None:
        return None

    async def embed(self, texts: List[str]) -> List[List[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> List[float]:
        vector = [0.0] * self.dimensions
        for word in text.lower().split():
            digest = hashlib.sha256(word.encode("utf-8")).hexdigest()
            vector[int(digest, 16) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


@dataclass(frozen=True)
class OllamaEmbeddingConfig:
    base_url: str
    model: str
    connect_timeout_seconds: float = 10.0

    @staticmethod
    def resolve_from_env() -> "OllamaEmbeddingConfig":
        base_url = (os.environ.get("VEYA_OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
        model = os.environ.get("VEYA_OLLAMA_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
        allow_remote = os.environ.get("VEYA_OLLAMA_ALLOW_REMOTE") == "1"

        if not allow_remote and not _is_loopback_url(base_url):
            raise EmbeddingUnavailableError(
                "VEYA_OLLAMA_URL must point at a local (loopback) Ollama instance. "
                "Set VEYA_OLLAMA_ALLOW_REMOTE=1 to explicitly allow a non-local endpoint."
            )

        return OllamaEmbeddingConfig(base_url=base_url, model=model)


class OllamaEmbeddingProvider:
    """Uses Ollama's local `/api/embed` endpoint — same trust boundary and
    stdlib-only (`urllib`) HTTP approach as `llm.ollama_provider`. Never
    calls a remote/cloud endpoint (see `OllamaEmbeddingConfig.resolve_from_env`)."""

    def __init__(self, config: Optional[OllamaEmbeddingConfig] = None) -> None:
        self._config = config or OllamaEmbeddingConfig.resolve_from_env()

    async def check_availability(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._check_availability_blocking)
        except EmbeddingUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingUnavailableError(
                f"Ollama embedding availability check failed ({type(exc).__name__})."
            ) from exc

    def _check_availability_blocking(self) -> None:
        request = urllib.request.Request(f"{self._config.base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._config.connect_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise EmbeddingUnavailableError("Could not reach the configured Ollama URL.") from exc
        except (TimeoutError, OSError) as exc:
            raise EmbeddingUnavailableError("Timed out reaching the configured Ollama URL.") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EmbeddingUnavailableError("Ollama returned a malformed response.") from exc

        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = {model.get("name") for model in models if isinstance(model, dict)}
        if self._config.model not in names and f"{self._config.model}:latest" not in names:
            raise EmbeddingUnavailableError("The configured embedding model is not available locally.")

    async def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._embed_blocking, texts)
        except EmbeddingUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingUnavailableError(f"Embedding request failed ({type(exc).__name__}).") from exc

    def _embed_blocking(self, texts: List[str]) -> List[List[float]]:
        request = urllib.request.Request(
            f"{self._config.base_url}/api/embed",
            data=json.dumps({"model": self._config.model, "input": texts}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.connect_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise EmbeddingUnavailableError("Could not reach the configured Ollama URL.") from exc
        except (TimeoutError, OSError) as exc:
            raise EmbeddingUnavailableError("Timed out reaching the configured Ollama URL.") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EmbeddingUnavailableError("Ollama returned a malformed embedding response.") from exc

        if not isinstance(payload, dict) or "embeddings" not in payload:
            raise EmbeddingUnavailableError("Ollama returned an unexpected embedding response shape.")
        return payload["embeddings"]


def default_embedding_provider_factory() -> EmbeddingProvider:
    """The production `WorkerContext.embedding_provider_factory` —
    resolves configuration fresh on every call (not cached at import
    time), same reasoning as `llm.ollama_provider.default_ollama_provider_factory`."""
    return OllamaEmbeddingProvider()
