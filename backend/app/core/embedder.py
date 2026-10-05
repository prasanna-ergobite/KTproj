"""
Local Embedding Engine for AutoKT.

Uses nomic-ai/nomic-embed-text-v1.5 via sentence-transformers.
Model context window: 8,192 tokens (~32,000 chars).

AutoKT chunk sizes comfortably fit:
  - Code chunks (AST): up to ~3,000 chars (~750 tokens)
  - Code chunks (fixed-window): up to ~2,000 chars (~500 tokens)
  - Doc chunks (heading-aware): up to ~2,400 chars (~600 tokens)
  - Doc chunks (fixed-window): up to ~2,000 chars (~500 tokens)
  All well within the 8,192-token limit.

Prefix convention (required for retrieval quality):
  - Ingestion (documents stored in ChromaDB): "search_document: {text}"
  - Query (semantic search queries): "search_query: {text}"
"""

import logging
from typing import List

logger = logging.getLogger("autokt.embedder")

_model = None
_model_name = "nomic-ai/nomic-embed-text-v1.5"


def _get_model():
    """Lazy-load and cache the sentence-transformers model on first use."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading embedding model '%s'...", _model_name)
            _model = SentenceTransformer(_model_name, trust_remote_code=True)
            logger.info("Embedding model '%s' loaded successfully.", _model_name)
        except Exception as exc:
            logger.error("Failed to load embedding model '%s': %s", _model_name, exc)
            raise
    return _model


def embed_documents(texts: List[str]) -> List[List[float]]:
    """
    Embed a list of texts for ingestion into ChromaDB.
    Applies the required 'search_document: ' prefix for nomic-embed-text-v1.5.
    """
    if not texts:
        return []
    from app.core.config import settings
    if settings.EMBEDDING_PROVIDER == "stub":
        from tests.helpers.stub_embedder import stub_embed
        return stub_embed(texts)
    prefixed = [f"search_document: {t}" for t in texts]
    model = _get_model()
    embeddings = model.encode(prefixed, batch_size=32, show_progress_bar=False, normalize_embeddings=True)
    return embeddings.tolist()


def embed_query(text: str) -> List[float]:
    """
    Embed a single query string for semantic search against ChromaDB.
    Applies the required 'search_query: ' prefix for nomic-embed-text-v1.5.
    """
    if not text or not text.strip():
        return []
    from app.core.config import settings
    if settings.EMBEDDING_PROVIDER == "stub":
        from tests.helpers.stub_embedder import stub_embed
        return stub_embed([text])[0]
    prefixed = f"search_query: {text}"
    model = _get_model()
    embedding = model.encode(prefixed, normalize_embeddings=True)
    return embedding.tolist()


def count_tokens(texts: List[str]) -> List[int]:
    """
    Return the real BPE token count for each text using the loaded Nomic tokenizer.
    Applies the same 'search_document: ' prefix as embed_documents() so the count
    matches exactly what the model sees at embed time.
    Pure tokenizer call — no matrix multiply performed.
    """
    if not texts:
        return []
    from app.core.config import settings
    if settings.EMBEDDING_PROVIDER == "stub":
        return [len(t.split()) for t in texts]
    prefixed = [f"search_document: {t}" for t in texts]
    model = _get_model()
    encoded = model.tokenizer(prefixed, padding=False, truncation=False)
    return [len(ids) for ids in encoded["input_ids"]]

