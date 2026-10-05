"""
Cross-Encoder Reranking Module for AutoKT GET /search.

Provides a lazy-loaded singleton CrossEncoder instance that reranks
(query, chunk_text) pairs to produce a precise relevance score as the
final stage of the BM25 + vector + RRF retrieval pipeline.

Model: cross-encoder/ms-marco-MiniLM-L-6-v2
  - ~90 MB RAM, ~20-35ms for 30 pairs on CPU.
  - Output: raw logits (~-10 to +10), NOT normalized to 0-1 range.
  - Context window: 512 tokens.

Chunk Truncation Strategy (head+tail):
  Measured on live Chroma data (2026-08-14):
    code_chunks: 75.3% of 959 chunks exceed 400 chars, median 781 chars.
    docs_chunks:  5.7% of  35 chunks exceed 400 chars, median 133 chars.
  Strategy: first HEAD_CHARS (default 250) + last TAIL_CHARS (default 150)
  = 400 chars total (~100 BPE tokens), well inside the 512-token limit.
"""

import logging
from typing import List, Optional

logger = logging.getLogger("autokt.reranker")

# Module-level lazy-loaded singleton. None until first call with provider="cross-encoder".
_reranker: Optional[object] = None


class RerankerError(Exception):
    """Raised when cross-encoder model fails to load or score pairs."""


def _truncate_for_reranker(text: str, head_chars: int, tail_chars: int) -> str:
    """
    Apply head+tail truncation for cross-encoder input.

    For short texts (len <= head_chars + tail_chars), returns text unchanged.
    For long texts, returns first head_chars + ' ... ' + last tail_chars.
    """
    total = head_chars + tail_chars
    if len(text) <= total:
        return text
    head = text[:head_chars]
    tail = text[-tail_chars:]
    return f"{head} ... {tail}"


def rerank(
    query: str,
    texts: List[str],
    provider: str = "cross-encoder",
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    head_chars: int = 250,
    tail_chars: int = 150,
) -> List[float]:
    """
    Score (query, text) pairs using the configured reranking provider.

    Args:
        query:      The user search query string.
        texts:      List of chunk text strings to score against the query.
        provider:   One of "cross-encoder", "disabled", or "stub".
        model_name: HuggingFace model ID (used only when provider="cross-encoder").
        head_chars: Chars to take from the start of each text for reranking input.
        tail_chars: Chars to take from the end of each text for reranking input.

    Returns:
        List of float scores (same length as texts), or [] if provider="disabled".
        For "stub": returns deterministic descending floats [N, N-1, ..., 1.0].
        For "cross-encoder": returns raw logit scores from the CrossEncoder model.

    Raises:
        RerankerError: If the cross-encoder model fails to load or score.
    """
def get_reranker(model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
    """
    Retrieves or initializes the lazy-loaded CrossEncoder model instance.
    Call during startup lifespan or prior to timing inference loops.
    """
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder  # type: ignore
        logger.info("Loading cross-encoder reranking model: %s", model_name)
        _reranker = CrossEncoder(model_name)
        logger.info("Cross-encoder model loaded successfully.")
    return _reranker


def rerank(
    query: str,
    texts: List[str],
    provider: str = "cross-encoder",
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    head_chars: int = 250,
    tail_chars: int = 150,
) -> List[float]:
    """
    Score (query, text) pairs using the configured reranking provider.

    Args:
        query:      The user search query string.
        texts:      List of chunk text strings to score against the query.
        provider:   One of "cross-encoder", "disabled", or "stub".
        model_name: HuggingFace model ID (used only when provider="cross-encoder").
        head_chars: Chars to take from the start of each text for reranking input.
        tail_chars: Chars to take from the end of each text for reranking input.

    Returns:
        List of float scores (same length as texts), or [] if provider="disabled".
        For "stub": returns deterministic descending floats [N, N-1, ..., 1.0].
        For "cross-encoder": returns raw logit scores from the CrossEncoder model.

    Raises:
        RerankerError: If the cross-encoder model fails to load or score.
    """
    if not texts:
        return []

    if provider == "disabled":
        return []

    if provider == "stub":
        # Deterministic descending scores: first item gets the highest score.
        return [float(len(texts) - i) for i in range(len(texts))]

    if provider == "cross-encoder":
        try:
            model = get_reranker(model_name)
            truncated_texts = [_truncate_for_reranker(t, head_chars, tail_chars) for t in texts]
            pairs = [(query, t) for t in truncated_texts]
            scores = model.predict(pairs)  # numpy array of floats
            return [float(s) for s in scores]

        except Exception as exc:
            raise RerankerError(f"Cross-encoder scoring failed: {exc}") from exc

    raise RerankerError(f"Unknown RERANKER_PROVIDER: '{provider}'. Must be one of: cross-encoder, disabled, stub.")

