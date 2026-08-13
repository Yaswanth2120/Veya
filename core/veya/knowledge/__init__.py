"""Local document ingestion, chunking, embedding, and retrieval (Section
9). Python owns parsing/chunking/embeddings/retrieval and a small derived
knowledge index (chunk metadata + embeddings, SQLite, under
`VEYA_KNOWLEDGE_INDEX_DIRECTORY`) — never session/transcript/question/
answer data, which stays exclusively in Swift/GRDB. See
`docs/KNOWLEDGE_RETRIEVAL.md`.
"""
