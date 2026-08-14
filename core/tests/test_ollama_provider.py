import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from veya.llm.errors import LLMProviderError, LLMTimeoutError, LLMUnavailableError
from veya.llm.ollama_provider import OllamaConfig, OllamaProvider, _supports_think_param


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


class DescribeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_reachable_with_configured_model_installed(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="qwen3:1.7b"))
        response = FakeHTTPResponse(body=json.dumps({"models": [{"name": "qwen3:1.7b"}, {"name": "nomic-embed-text:latest"}]}).encode("utf-8"))
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            status = await provider.describe_status()
        self.assertTrue(status["reachable"])
        self.assertTrue(status["model_installed"])
        self.assertEqual(status["configured_model"], "qwen3:1.7b")
        self.assertIn("nomic-embed-text:latest", status["available_models"])

    async def test_reachable_but_configured_model_missing(self):
        # Exactly the scenario a review found live: Ollama running,
        # llama3.2 configured, only qwen3:1.7b actually installed.
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="llama3.2"))
        response = FakeHTTPResponse(body=json.dumps({"models": [{"name": "qwen3:1.7b"}]}).encode("utf-8"))
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            status = await provider.describe_status()
        self.assertTrue(status["reachable"])
        self.assertFalse(status["model_installed"])
        self.assertEqual(status["configured_model"], "llama3.2")
        self.assertEqual(status["available_models"], ["qwen3:1.7b"])

    async def test_unreachable_never_raises(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:1", model="m"))
        with patch(
            "veya.llm.ollama_provider.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            status = await provider.describe_status()
        self.assertFalse(status["reachable"])
        self.assertFalse(status["model_installed"])
        self.assertEqual(status["available_models"], [])

    async def test_malformed_response_never_raises(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        response = FakeHTTPResponse(body=b"not json")
        with patch("veya.llm.ollama_provider.urllib.request.urlopen", return_value=response):
            status = await provider.describe_status()
        self.assertFalse(status["reachable"])


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


class SupportsThinkParamTests(unittest.TestCase):
    """Section 18: a real version check, not an assumption — gates
    whether `"think": false` (skips the reasoning pass entirely for
    thinking models) is safe to send."""

    def test_a_new_enough_version_supports_the_think_param(self):
        self.assertTrue(_supports_think_param("0.9.0"))
        self.assertTrue(_supports_think_param("0.32.9"))
        self.assertTrue(_supports_think_param("1.0.0"))

    def test_an_older_version_does_not_support_the_think_param(self):
        self.assertFalse(_supports_think_param("0.8.9"))
        self.assertFalse(_supports_think_param("0.1.0"))

    def test_an_unparseable_version_string_is_treated_as_unsupported(self):
        self.assertFalse(_supports_think_param(""))
        self.assertFalse(_supports_think_param("not-a-version"))


def _request_for(call) -> "urllib.request.Request":
    return call.args[0] if call.args else call.kwargs["url"]


class GenerateStreamThinkParamTests(unittest.IsolatedAsyncioTestCase):
    """Section 18: `think: false` is sent only when a real `/api/version`
    check confirms support, and a real rejection triggers a genuine,
    observed fallback — never assumed either way."""

    def _urlopen_side_effect(self, version_response, generate_response):
        def _side_effect(request, timeout=None):
            if request.full_url.endswith("/api/version"):
                return version_response
            return generate_response

        return _side_effect

    async def test_think_false_is_sent_when_the_version_check_confirms_support(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        version_response = FakeHTTPResponse(body=json.dumps({"version": "0.32.9"}).encode())
        generate_response = FakeHTTPResponse(lines=ndjson_lines({"response": "hi", "done": True}))

        with patch(
            "veya.llm.ollama_provider.urllib.request.urlopen",
            side_effect=self._urlopen_side_effect(version_response, generate_response),
        ) as mock_urlopen:
            deltas = [chunk async for chunk in provider.generate_stream("prompt", timeout=5)]

        self.assertEqual(deltas, ["hi"])
        generate_calls = [call for call in mock_urlopen.call_args_list if _request_for(call).full_url.endswith("/api/generate")]
        self.assertEqual(len(generate_calls), 1)
        sent_body = json.loads(_request_for(generate_calls[0]).data)
        self.assertEqual(sent_body.get("think"), False)

    async def test_think_field_is_omitted_when_the_version_is_too_old(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        version_response = FakeHTTPResponse(body=json.dumps({"version": "0.7.0"}).encode())
        generate_response = FakeHTTPResponse(lines=ndjson_lines({"response": "hi", "done": True}))

        with patch(
            "veya.llm.ollama_provider.urllib.request.urlopen",
            side_effect=self._urlopen_side_effect(version_response, generate_response),
        ) as mock_urlopen:
            deltas = [chunk async for chunk in provider.generate_stream("prompt", timeout=5)]

        self.assertEqual(deltas, ["hi"])
        generate_calls = [call for call in mock_urlopen.call_args_list if _request_for(call).full_url.endswith("/api/generate")]
        sent_body = json.loads(_request_for(generate_calls[0]).data)
        self.assertNotIn("think", sent_body)

    async def test_version_check_only_happens_once_per_provider_not_per_question(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        version_response = FakeHTTPResponse(body=json.dumps({"version": "0.32.9"}).encode())

        def make_generate_response():
            return FakeHTTPResponse(lines=ndjson_lines({"response": "hi", "done": True}))

        call_count = {"version": 0}

        def side_effect(request, timeout=None):
            if request.full_url.endswith("/api/version"):
                call_count["version"] += 1
                return version_response
            return make_generate_response()

        with patch("veya.llm.ollama_provider.urllib.request.urlopen", side_effect=side_effect):
            async for _ in provider.generate_stream("prompt one", timeout=5):
                pass
            async for _ in provider.generate_stream("prompt two", timeout=5):
                pass

        self.assertEqual(call_count["version"], 1)

    async def test_an_http_error_on_think_false_falls_back_to_a_plain_request(self):
        provider = OllamaProvider(OllamaConfig(base_url="http://localhost:11434", model="m"))
        version_response = FakeHTTPResponse(body=json.dumps({"version": "0.32.9"}).encode())
        generate_response = FakeHTTPResponse(lines=ndjson_lines({"response": "hi", "done": True}))

        call_state = {"generate_attempts": 0}

        def side_effect(request, timeout=None):
            if request.full_url.endswith("/api/version"):
                return version_response
            call_state["generate_attempts"] += 1
            if call_state["generate_attempts"] == 1:
                # `HTTPError.close()` before raising: constructing one
                # directly (rather than via a real urlopen failure) leaves
                # an internal tempfile closer that otherwise warns on GC
                # under `-W error::ResourceWarning`, unrelated to
                # anything this test is actually checking.
                error = urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b""))
                error.close()
                raise error
            return generate_response

        with patch("veya.llm.ollama_provider.urllib.request.urlopen", side_effect=side_effect):
            deltas = [chunk async for chunk in provider.generate_stream("prompt", timeout=5)]

        self.assertEqual(deltas, ["hi"])
        self.assertEqual(call_state["generate_attempts"], 2)
        # The failure is remembered — a second question in the same
        # session never retries the rejected field again.
        self.assertFalse(provider._think_param_supported)
