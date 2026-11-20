"""Select answer inference independently of local document embeddings."""
import os

from .errors import LLMUnavailableError
from .ollama_provider import OllamaProvider
from .responses_provider import ResponsesProvider


def default_llm_provider_factory():
    backend = os.environ.get("VEYA_LLM_BACKEND", "ollama")
    if backend == "ollama":
        return OllamaProvider()
    if backend == "vast":
        return ResponsesProvider()
    raise LLMUnavailableError("Unknown VEYA_LLM_BACKEND; choose ollama or vast.")
