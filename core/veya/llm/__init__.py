"""Local LLM provider abstraction. Ollama (`ollama_provider.py`) is the
only provider implemented in Section 8, but `provider.py`'s `LLMProvider`
protocol is written so a later local/cloud provider can be added without
touching question detection, session orchestration, or Swift IPC.
"""
