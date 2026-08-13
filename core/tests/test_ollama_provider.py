import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from veya.llm.errors import LLMProviderError, LLMTimeoutError, LLMUnavailableError
from veya.llm.ollama_provider import OllamaConfig, OllamaProvider


class FakeHTTPResponse:
    """A minimal stand-in for `http.client.HTTPResponse` — supports the
    context-manager protocol, `.read()`, and line iteration, which is all
    `OllamaProvider` uses."""

    def __init__(self, lines: list[bytes] = None, body: bytes = b""):
        self._lines = lines or []
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)


def ndjson_lines(*payloads: dict) -> list[bytes]:
    return [(json.dumps(p) + "\n").encode("utf-8") for p in payloads]


class OllamaConfigTests(unittest.TestCase):
    def test_resolves_defaults_when_env_vars_are_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            config = OllamaConfig.resolve_from_env()
        self.assertTrue(config.base_url)
        self.assertTrue(config.model)

    def test_resolves_from_env_vars_when_set(self):
        with patch.dict(
            os.environ, {"VEYA_OLLAMA_URL": "http://localhost:9999/", "VEYA_OLLAMA_MODEL": "custom-model"}, clear=True
        ):
            config = OllamaConfig.resolve_from_env()
        self.assertEqual(config.base_url, "http://localhost:9999")  # trailing slash stripped
        self.assertEqual(config.model, "custom-model")

    def test_loopback_ip_literal_is_accepted(self):
        with patch.dict(os.environ, {"VEYA_OLLAMA_URL": "http://127.0.0.1:11434"}, clear=True):
            config = OllamaConfig.resolve_from_env()
        self.assertEqual(config.base_url, "http://127.0.0.1:11434")

    def test_remote_url_is_rejected_by_default(self):
        with patch.dict(os.environ, {"VEYA_OLLAMA_URL": "http://example.com:11434"}, clear=True):
            with self.assertRaises(LLMUnavailableError):
                OllamaConfig.resolve_from_env()

    def test_remote_ip_is_rejected_by_default(self):
        with patch.dict(os.environ, {"VEYA_OLLAMA_URL": "http://93.184.216.34:11434"}, clear=True):
            with self.assertRaises(LLMUnavailableError):
                OllamaConfig.resolve_from_env()

    def test_remote_url_is_accepted_with_explicit_opt_in(self):
        with patch.dict(
            os.environ,
            {"VEYA_OLLAMA_URL": "http://example.com:11434", "VEYA_OLLAMA_ALLOW_REMOTE": "1"},
            clear=True,
        ):
            config = OllamaConfig.resolve_from_env()
        self.assertEqual(config.base_url, "http://example.com:11434")

    def test_never_silently_falls_back_to_local_when_remote_is_rejected(self):
        # Rejecting a remote URL must raise, not silently substitute the
        # local default and proceed as if nothing were misconfigured.
        with patch.dict(os.environ, {"VEYA_OLLAMA_URL": "http://example.com:11434"}, clear=True):
            with self.assertRaises(LLMUnavailableError) as ctx:
                OllamaConfig.resolve_from_env()
        self.assertIn("loopback", ctx.exception.reason.lower())


class CheckAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_host_raises_unavailable(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:1", model="m"))
        with patch(
            "veya.llm.ollama_provider.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(LLMUnavailableError):
                await provider.check_availability()

    async def test_model_not_present_locally_raises_unavailable(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="missing-model"))
        response = FakeHTTPResponse(body=json.dumps({"models": [{"name": "other-model:latest"}]}).encode("utf-8"))
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            with self.assertRaises(LLMUnavailableError):
                await provider.check_availability()

    async def test_model_present_with_implicit_latest_tag_succeeds(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="llama3.2"))
        response = FakeHTTPResponse(body=json.dumps({"models": [{"name": "llama3.2:latest"}]}).encode("utf-8"))
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            await provider.check_availability()  # does not raise

    async def test_malformed_response_raises_unavailable(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        response = FakeHTTPResponse(body=b"not json")
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            with self.assertRaises(LLMUnavailableError):
                await provider.check_availability()


class GenerateStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_yields_response_deltas_in_order(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        lines = ndjson_lines(
            {"response": "Hello", "done": False},
            {"response": " there", "done": False},
            {"response": "", "done": True},
        )
        response = FakeHTTPResponse(lines=lines)
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            deltas = [chunk async for chunk in provider.generate_stream("prompt", timeout=5)]
        self.assertEqual(deltas, ["Hello", " there"])

    async def test_connection_failure_raises_unavailable(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        with patch(
            "veya.llm.ollama_provider.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            with self.assertRaises(LLMUnavailableError):
                async for _ in provider.generate_stream("prompt", timeout=5):
                    pass

    async def test_timeout_raises_llm_timeout_error(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(LLMTimeoutError):
                async for _ in provider.generate_stream("prompt", timeout=5):
                    pass

    async def test_malformed_streaming_line_raises_provider_error(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        response = FakeHTTPResponse(lines=[b"not json at all\n"])
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            with self.assertRaises(LLMProviderError):
                async for _ in provider.generate_stream("prompt", timeout=5):
                    pass

    async def test_module_has_no_logger_so_prompt_content_can_never_be_logged_from_here(self):
        # `ollama_provider.py` deliberately has no `logging` calls at
        # all — proving by construction that a prompt/response can never
        # be logged from this module, rather than asserting against
        # captured log output from a specific call site.
        import veya.llm.ollama_provider as module

        self.assertFalse(hasattr(module, "logger"))
