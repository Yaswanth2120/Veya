import json
import math
import os
import unittest
import urllib.error
from unittest.mock import patch

from veya.knowledge.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    FakeEmbeddingProvider,
    OllamaEmbeddingConfig,
    OllamaEmbeddingProvider,
)
from veya.knowledge.errors import EmbeddingUnavailableError


class FakeEmbeddingProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_one_vector_per_input_text(self):
        provider = FakeEmbeddingProvider()
        vectors = await provider.embed(["hello world", "goodbye world"])
        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), provider.dimensions)

    async def test_is_deterministic(self):
        provider = FakeEmbeddingProvider()
        first = await provider.embed(["the migration took six weeks"])
        second = await provider.embed(["the migration took six weeks"])
        self.assertEqual(first, second)

    async def test_vectors_are_unit_normalized(self):
        provider = FakeEmbeddingProvider()
        [vector] = await provider.embed(["some words here"])
        norm = math.sqrt(sum(v * v for v in vector))
        self.assertAlmostEqual(norm, 1.0, places=5)

    async def test_texts_sharing_more_words_are_closer_than_unrelated_text(self):
        provider = FakeEmbeddingProvider()
        base, similar, unrelated = await provider.embed(
            [
                "the migration took six weeks to complete",
                "the migration took six weeks to finish",
                "completely different pizza recipe content",
            ]
        )

        def dot(a, b):
            return sum(x * y for x, y in zip(a, b))

        self.assertGreater(dot(base, similar), dot(base, unrelated))

    async def test_empty_input_returns_empty_list(self):
        provider = FakeEmbeddingProvider()
        self.assertEqual(await provider.embed([]), [])

    async def test_check_availability_never_raises(self):
        provider = FakeEmbeddingProvider()
        await provider.check_availability()  # does not raise


class OllamaEmbeddingConfigTests(unittest.TestCase):
    def test_resolves_defaults_when_env_vars_are_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            config = OllamaEmbeddingConfig.resolve_from_env()
        self.assertTrue(config.base_url)
        self.assertEqual(config.model, DEFAULT_EMBEDDING_MODEL)

    def test_resolves_from_env_vars_when_set(self):
        with patch.dict(
            os.environ,
            {"VEYA_OLLAMA_URL": "http://localhost:11434", "VEYA_OLLAMA_EMBEDDING_MODEL": "custom-embed"},
            clear=True,
        ):
            config = OllamaEmbeddingConfig.resolve_from_env()
        self.assertEqual(config.model, "custom-embed")

    def test_remote_url_is_rejected_by_default(self):
        with patch.dict(os.environ, {"VEYA_OLLAMA_URL": "http://example.com:11434"}, clear=True):
            with self.assertRaises(EmbeddingUnavailableError):
                OllamaEmbeddingConfig.resolve_from_env()

    def test_remote_url_is_accepted_with_explicit_opt_in(self):
        with patch.dict(
            os.environ,
            {"VEYA_OLLAMA_URL": "http://example.com:11434", "VEYA_OLLAMA_ALLOW_REMOTE": "1"},
            clear=True,
        ):
            config = OllamaEmbeddingConfig.resolve_from_env()
        self.assertEqual(config.base_url, "http://example.com:11434")


class FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


class OllamaEmbeddingProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_availability_succeeds_when_model_present(self):
        provider = OllamaEmbeddingProvider(OllamaEmbeddingConfig(base_url="http://localhost:11434", model="nomic-embed-text"))
        response = FakeHTTPResponse(json.dumps({"models": [{"name": "nomic-embed-text:latest"}]}).encode("utf-8"))
        with patch("veya.knowledge.embeddings.urllib.request.urlopen", return_value=response):
            await provider.check_availability()  # does not raise

    async def test_check_availability_raises_when_model_missing(self):
        provider = OllamaEmbeddingProvider(OllamaEmbeddingConfig(base_url="http://localhost:11434", model="missing-model"))
        response = FakeHTTPResponse(json.dumps({"models": [{"name": "other:latest"}]}).encode("utf-8"))
        with patch("veya.knowledge.embeddings.urllib.request.urlopen", return_value=response):
            with self.assertRaises(EmbeddingUnavailableError):
                await provider.check_availability()

    async def test_check_availability_raises_when_unreachable(self):
        provider = OllamaEmbeddingProvider(OllamaEmbeddingConfig(base_url="http://localhost:1", model="m"))
        with patch(
            "veya.knowledge.embeddings.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")
        ):
            with self.assertRaises(EmbeddingUnavailableError):
                await provider.check_availability()

    async def test_embed_returns_vectors_from_the_response(self):
        provider = OllamaEmbeddingProvider(OllamaEmbeddingConfig(base_url="http://localhost:11434", model="m"))
        response = FakeHTTPResponse(json.dumps({"embeddings": [[0.1, 0.2], [0.3, 0.4]]}).encode("utf-8"))
        with patch("veya.knowledge.embeddings.urllib.request.urlopen", return_value=response):
            vectors = await provider.embed(["text one", "text two"])
        self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])

    async def test_embed_of_empty_list_makes_no_request(self):
        provider = OllamaEmbeddingProvider(OllamaEmbeddingConfig(base_url="http://localhost:11434", model="m"))
        with patch("veya.knowledge.embeddings.urllib.request.urlopen") as mock_urlopen:
            result = await provider.embed([])
        self.assertEqual(result, [])
        mock_urlopen.assert_not_called()

    async def test_embed_raises_unavailable_on_connection_failure(self):
        provider = OllamaEmbeddingProvider(OllamaEmbeddingConfig(base_url="http://localhost:11434", model="m"))
        with patch(
            "veya.knowledge.embeddings.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")
        ):
            with self.assertRaises(EmbeddingUnavailableError):
                await provider.embed(["text"])

    async def test_embed_raises_unavailable_on_unexpected_response_shape(self):
        provider = OllamaEmbeddingProvider(OllamaEmbeddingConfig(base_url="http://localhost:11434", model="m"))
        response = FakeHTTPResponse(json.dumps({"error": "no embeddings support"}).encode("utf-8"))
        with patch("veya.knowledge.embeddings.urllib.request.urlopen", return_value=response):
            with self.assertRaises(EmbeddingUnavailableError):
                await provider.embed(["text"])
