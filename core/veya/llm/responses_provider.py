"""Answer inference through an explicitly selected, loopback-only SSH tunnel.

Only output_text deltas leave this provider. Reasoning and error response
bodies are never rendered or logged. No proxy environment or redirects are
used, so a tunnel URL cannot redirect private context to another host.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlsplit

from .errors import LLMContextError, LLMError, LLMProviderError, LLMTimeoutError, LLMUnavailableError


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ResponsesConfig:
    base_url: str = "http://127.0.0.1:18000/v1"
    model: str = "openai/gpt-oss-120b"
    context_window: int = 8192
    max_output_tokens: int = 1024
    auto_select_model: bool = False
    use_server_tokenizer: bool = False

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"}
                or url.username or url.password or url.query or url.fragment or url.path.rstrip("/") != "/v1"):
            raise LLMUnavailableError("GPU inference requires an HTTP loopback /v1 SSH tunnel endpoint.")
        if not self.model.strip() or not 1024 <= self.context_window <= 16384:
            raise LLMUnavailableError("Invalid GPU model or context window (1024–16384 tokens).")
        if not 128 <= self.max_output_tokens < self.context_window - 512:
            raise LLMUnavailableError("GPU output budget leaves insufficient prompt context.")

    @classmethod
    def resolve_from_env(cls):
        try:
            return cls(
                base_url=os.environ.get("VEYA_GPU_URL", cls.base_url).rstrip("/"),
                model=os.environ.get("VEYA_GPU_MODEL", cls.model),
                context_window=int(os.environ.get("VEYA_GPU_CONTEXT_WINDOW", "8192")),
                max_output_tokens=int(os.environ.get("VEYA_GPU_MAX_OUTPUT_TOKENS", "1024")),
                auto_select_model=not bool(os.environ.get("VEYA_GPU_MODEL")),
                use_server_tokenizer=True,
            )
        except ValueError as exc:
            raise LLMUnavailableError("Invalid numeric GPU context configuration.") from exc


class ResponsesProvider:
    safe_error_messages = True

    def __init__(self, config: ResponsesConfig | None = None):
        self.config = config or ResponsesConfig.resolve_from_env()
        self._resolved_model = self.config.model
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def _status(self):
        result = dict(provider="vast", remote_processing=True, reachable=False,
                      base_url=self.config.base_url, configured_model=self.config.model,
                      model_installed=False, available_models=[], error="",
                      context_window=self.config.context_window)
        try:
            with self._opener.open(self.config.base_url + "/models", timeout=3) as response:
                body = response.read(1_048_577)
                if len(body) > 1_048_576:
                    raise ValueError("oversized model listing")
                payload = json.loads(body)
            names = sorted({m["id"] for m in payload["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)})
            selected = self.config.model
            if self.config.auto_select_model and selected not in names and len(names) == 1:
                selected = names[0]
            self._resolved_model = selected
            result.update(reachable=True, available_models=names, configured_model=selected, model_installed=selected in names)
            if not result["model_installed"]:
                result["error"] = "model_missing"
        except Exception:
            result["error"] = "tunnel_unreachable_or_invalid_response"
        return result

    async def describe_status(self):
        return await asyncio.to_thread(self._status)

    async def check_availability(self):
        status = await self.describe_status()
        if not status["reachable"]:
            raise LLMUnavailableError("GPU tunnel unavailable. Check the existing SSH tunnel on port 18000.")
        if not status["model_installed"]:
            raise LLMUnavailableError("The configured GPU model is not served by this endpoint.")

    def _count_prompt_tokens(self, prompt: str) -> int:
        try:
            request = urllib.request.Request(self.config.base_url.rstrip("/").rsplit("/v1", 1)[0] + "/tokenize",
                data=json.dumps(dict(model=self._resolved_model, prompt=prompt, add_special_tokens=True)).encode(),
                headers={"Content-Type": "application/json"})
            with self._opener.open(request, timeout=3) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576: raise ValueError("oversized tokenizer response")
                payload = json.loads(raw)
            count = payload["count"]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0: raise ValueError("invalid token count")
            maximum = payload.get("max_model_len", self.config.context_window)
            if isinstance(maximum, int) and maximum > 0 and count + self.config.max_output_tokens + 512 > maximum:
                raise LLMContextError("Question and context exceed the GPU server's context window. Shorten the session notes or documents.")
            return count
        except LLMContextError:
            raise
        except Exception as exc:
            raise LLMContextError("Cannot verify the length of this large prompt. Check the GPU tokenizer endpoint or shorten the context.") from exc

    def _request_body(self, prompt: str, prompt_tokens: int | None = None):
        # UTF-8 bytes are a deliberately conservative token upper bound for
        # this byte-level model; reserve extra room for its chat template.
        # Reject oversized prompts rather than silently losing evidence.
        budget = self.config.context_window - self.config.max_output_tokens - 512
        if (len(prompt.encode("utf-8")) if prompt_tokens is None else prompt_tokens) > budget:
            raise LLMContextError("GPU prompt exceeds the conservative context budget. Shorten context or select a verified larger window.")
        return dict(model=self._resolved_model, input=prompt, stream=True, store=False,
                    max_output_tokens=self.config.max_output_tokens, reasoning={"effort": "low"})

    async def generate_stream(self, prompt: str, *, timeout: float) -> AsyncIterator[str]:
        if self.config.auto_select_model:
            await self.check_availability()
        prompt_tokens = None
        if self.config.use_server_tokenizer and len(prompt.encode("utf-8")) > self.config.context_window - self.config.max_output_tokens - 512:
            prompt_tokens = await asyncio.to_thread(self._count_prompt_tokens, prompt)
        body = self._request_body(prompt, prompt_tokens)
        items = queue.Queue(maxsize=64)
        stopped = threading.Event()
        done = object()
        deadline = time.monotonic() + timeout

        def put(item):
            while not stopped.is_set():
                try:
                    items.put(item, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def produce():
            try:
                request = urllib.request.Request(self.config.base_url + "/responses",
                    data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
                with self._opener.open(request, timeout=timeout) as response:
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        raise LLMProviderError("GPU endpoint did not return an SSE stream.")
                    completed = False
                    data_lines = []

                    def process_event():
                        nonlocal completed
                        if not data_lines:
                            return
                        raw = "\n".join(data_lines)
                        data_lines.clear()
                        if raw == "[DONE]":
                            return
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind == "response.output_text.delta":
                            delta = event.get("delta")
                            if not isinstance(delta, str):
                                raise ValueError("invalid text delta")
                            if delta:
                                put(delta)
                        elif kind == "response.completed":
                            if event.get("response", {}).get("status") != "completed":
                                raise LLMProviderError("GPU response did not complete successfully.")
                            completed = True
                        elif kind in {"error", "response.failed", "response.incomplete"}:
                            raise LLMProviderError("GPU response failed or exhausted its output budget.")

                    while not stopped.is_set():
                        line = response.readline(1_048_577)
                        if not line:
                            process_event()
                            break
                        if len(line) > 1_048_576:
                            raise ValueError("oversized stream line")
                        text = line.decode("utf-8").rstrip("\r\n")
                        if not text:
                            process_event()
                            if completed:
                                break
                        elif text.startswith("data:"):
                            data_lines.append(text[5:].lstrip(" "))
                        if time.monotonic() >= deadline:
                            raise LLMTimeoutError("GPU generation exceeded the request deadline.")
                    if not completed and not stopped.is_set():
                        raise LLMProviderError("GPU stream disconnected before response.completed.")
            except urllib.error.HTTPError as exc:
                exc.close()
                detail = "Check the served model in AI Backend settings." if exc.code == 404 else "Check the GPU server and model/context settings."
                put(LLMProviderError(f"GPU endpoint rejected the request (HTTP {exc.code}). {detail}"))
            except urllib.error.URLError:
                put(LLMUnavailableError("GPU SSH tunnel is unavailable."))
            except (TimeoutError, OSError):
                put(LLMTimeoutError("GPU inference timed out."))
            except LLMError as exc:
                put(exc)
            except Exception:
                put(LLMProviderError("GPU returned a malformed streaming response."))
            finally:
                put(done)

        threading.Thread(target=produce, daemon=True).start()
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise LLMTimeoutError("GPU generation exceeded the request deadline.")
                try:
                    item = await asyncio.to_thread(items.get, True, 0.1)
                except queue.Empty:
                    continue
                if item is done:
                    return
                if isinstance(item, LLMError):
                    raise item
                yield item
        finally:
            stopped.set()
