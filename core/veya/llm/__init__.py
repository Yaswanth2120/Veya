"""LLM providers: local Ollama and explicitly selected tunneled Responses.

The factory selects answer inference independently of local embeddings;
conversation orchestration and Swift IPC depend on the shared protocol.
"""
