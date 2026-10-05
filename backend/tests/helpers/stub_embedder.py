"""
Stub Embedding Provider for AutoKT test suite.
Returns fixed-dimension zero vectors without calling any external LLM service.
"""

from typing import List


def stub_embed(texts: List[str], dim: int = 768) -> List[List[float]]:
    """
    Returns zero-vectors for testing embedding-gated code paths.
    Defaults to 768 dimensions to match nomic-embed-text-v1.5 schema.
    """
    return [[0.0] * dim for _ in texts]

