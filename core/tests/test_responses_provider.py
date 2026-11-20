import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from veya.llm.errors import LLMContextError, LLMProviderError, LLMUnavailableError
from veya.llm.factory import default_llm_provider_factory
from veya.llm.ollama_provider import OllamaProvider
from veya.llm.responses_provider import ResponsesConfig, ResponsesProvider, _NoRedirects


class Response(io.BytesIO):
    headers = {"Content-Type": "text/event-stream"}


def sse(*events):
    return Response(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events))


class ResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_only_loopback_tunnel_urls_accepted(self):
        for url in ["http://example.com/v1", "http://127.0.0.1.evil.test/v1",
                    "http://secret@localhost/v1", "http://localhost/v1?key=secret", "file:///v1"]:
            with self.subTest(url=url), self.assertRaises(LLMUnavailableError):
                ResponsesConfig(base_url=url)

    def test_redirects_never_forward_context(self):
        self.assertIsNone(_NoRedirects().redirect_request(None, None, 302, "", {}, "http://example.com"))

    def test_factory_explicit_selection_does_not_change_ollama_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(default_llm_provider_factory(), OllamaProvider)
        with patch.dict(os.environ, {"VEYA_LLM_BACKEND": "vast"}, clear=True):
            self.assertIsInstance(default_llm_provider_factory(), ResponsesProvider)
        with patch.dict(os.environ, {"VEYA_LLM_BACKEND": "typo"}, clear=True):
            with self.assertRaises(LLMUnavailableError):
                default_llm_provider_factory()

    async def test_reasoning_is_ignored_and_only_text_is_streamed(self):
        provider = ResponsesProvider(ResponsesConfig())
        response = sse(
            {"type": "response.reasoning_text.delta", "delta": "SECRET REASONING"},
            {"type": "response.output_text.delta", "delta": "ANSWER: hello"},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
        with patch.object(provider._opener, "open", return_value=response) as opened:
            text = "".join([d async for d in provider.generate_stream("question", timeout=2)])
        self.assertEqual(text, "ANSWER: hello")
        body = json.loads(opened.call_args.args[0].data)
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"], {"effort": "low"})

    async def test_oversized_prompt_is_rejected_before_network_access(self):
        provider = ResponsesProvider(ResponsesConfig())
        with patch.object(provider._opener, "open") as opened:
            with self.assertRaises(LLMContextError):
                _ = [d async for d in provider.generate_stream("x" * 8192, timeout=2)]
            opened.assert_not_called()

    async def test_incomplete_or_disconnected_stream_never_counts_as_success(self):
        for event in [None, {"type": "response.incomplete", "response": {"status": "incomplete"}}]:
            provider = ResponsesProvider(ResponsesConfig())
            response = sse(*([event] if event else []))
            with patch.object(provider._opener, "open", return_value=response):
                with self.assertRaises(LLMProviderError):
                    _ = [d async for d in provider.generate_stream("question", timeout=2)]

    async def test_server_error_body_is_never_exposed(self):
        provider = ResponsesProvider(ResponsesConfig())
        error = urllib.error.HTTPError("http://localhost", 400, "secret prompt", {}, io.BytesIO(b"PRIVATE DATA"))
        with patch.object(provider._opener, "open", side_effect=error):
            with self.assertRaises(LLMProviderError) as caught:
                _ = [d async for d in provider.generate_stream("question", timeout=2)]
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    async def test_model_listing_drives_reachability_and_model_match(self):
        provider = ResponsesProvider(ResponsesConfig())
        with patch.object(provider._opener, "open", return_value=Response(json.dumps({"data": [{"id": provider.config.model}]}).encode())):
            status = await provider.describe_status()
        self.assertTrue(status["remote_processing"])
        self.assertTrue(status["model_installed"])
        self.assertEqual(status["provider"], "vast")

    async def test_auto_selection_uses_actual_single_served_model(self):
        provider = ResponsesProvider(ResponsesConfig(auto_select_model=True))
        response = Response(json.dumps({"data": [{"id": "Qwen/current-model"}]}).encode())
        with patch.object(provider._opener, "open", return_value=response):
            status = await provider.describe_status()
        self.assertTrue(status["model_installed"])
        self.assertEqual(status["configured_model"], "Qwen/current-model")
        self.assertEqual(provider._request_body("hello")["model"], "Qwen/current-model")

    async def test_explicit_model_is_never_silently_replaced(self):
        provider = ResponsesProvider(ResponsesConfig(model="explicit-model"))
        response = Response(json.dumps({"data": [{"id": "different-model"}]}).encode())
        with patch.object(provider._opener, "open", return_value=response):
            status = await provider.describe_status()
        self.assertFalse(status["model_installed"])
        self.assertEqual(status["configured_model"], "explicit-model")

    async def test_large_text_uses_exact_token_count_instead_of_rejecting_bytes(self):
        provider = ResponsesProvider(ResponsesConfig(use_server_tokenizer=True))
        tokenizer = Response(json.dumps({"count": 2000, "max_model_len": 16384}).encode())
        response = sse({"type": "response.output_text.delta", "delta": "Valid answer"},
                       {"type": "response.completed", "response": {"status": "completed"}})
        with patch.object(provider._opener, "open", side_effect=[tokenizer, response]) as opened:
            text = "".join([d async for d in provider.generate_stream("word " * 2000, timeout=2)])
        self.assertEqual(text, "Valid answer")
        self.assertTrue(opened.call_args_list[0].args[0].full_url.endswith("/tokenize"))

    async def test_exact_token_count_still_rejects_overflow(self):
        provider = ResponsesProvider(ResponsesConfig(use_server_tokenizer=True))
        response = Response(json.dumps({"count": 8000, "max_model_len": 8192}).encode())
        with patch.object(provider._opener, "open", return_value=response) as opened:
            with self.assertRaises(LLMContextError):
                _ = [d async for d in provider.generate_stream("word " * 2000, timeout=2)]
        self.assertEqual(opened.call_count, 1)
