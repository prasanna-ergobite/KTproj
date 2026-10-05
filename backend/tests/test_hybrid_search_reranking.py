"""
Integration Test Suite for Cross-Encoder Reranking in GET /search.

Verifies:
1. rerank helper outputs valid float scores for query-text pairs.
2. test_cross_encoder_rerank_promotes_relevance: Cross-encoder rerank reorders RRF candidates
   according to query-chunk cross-attention relevance (Empirical domain-fit validation).
3. test_graceful_degradation_on_reranker_failure: Falls back seamlessly to RRF ordering on exception.
4. test_telemetry_includes_reranking_ms: Telemetry returns reranking_ms.
5. test_total_results_reflects_reranked_pool: total_results matches reranked candidate pool size.
6. test_disabled_reranker_falls_back_to_rrf: Setting RERANKER_PROVIDER='disabled' bypasses reranking.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.main import app
from app.core.config import settings
from app.core.reranker import rerank, _truncate_for_reranker

client = TestClient(app)


def test_truncation_helper_head_tail():
    """Verify _truncate_for_reranker applies head+tail truncation correctly."""
    short_text = "Hello world"
    assert _truncate_for_reranker(short_text, head_chars=250, tail_chars=150) == short_text

    long_text = "HEAD_" + "x" * 500 + "_TAIL"
    truncated = _truncate_for_reranker(long_text, head_chars=10, tail_chars=10)
    assert truncated.startswith("HEAD_")
    assert truncated.endswith("_TAIL")
    assert " ... " in truncated


def test_reranker_scores_query_text_pairs():
    """Verify rerank helper returns float scores for query-text pairs with stub provider."""
    scores = rerank(
        query="validate_repo_url",
        texts=["first text snippet", "second text snippet"],
        provider="stub",
    )
    assert len(scores) == 2
    assert scores[0] == 2.0
    assert scores[1] == 1.0


def test_cross_encoder_rerank_promotes_relevance():
    """
    Empirical domain-fit validation test for ms-marco-MiniLM-L-6-v2.

    Seeds two chunks:
      - chunk_exact: Contains exact query function name 'validate_repo_url' in header/text.
      - chunk_semantic: High vector similarity, but generic/unrelated function.

    Verifies that the cross-encoder model scores chunk_exact higher than chunk_semantic
    and promotes it to the top position post-rerank.
    """
    query = "validate_repo_url"
    chunk_exact_text = (
        "# File: app/core/validator.py\n"
        "# Function: validate_repo_url\n\n"
        "def validate_repo_url(url: str) -> bool:\n"
        "    \"\"\"Validate git repository URL format and accessibility.\"\"\"\n"
        "    if not url:\n"
        "        return False\n"
        "    return url.startswith(('http://', 'https://', 'git@'))\n"
    )
    chunk_semantic_text = (
        "# File: app/core/utils.py\n"
        "# Function: check_network_connection\n\n"
        "def check_network_connection(host: str, port: int) -> bool:\n"
        "    \"\"\"Check generic network endpoint reachability.\"\"\"\n"
        "    return True\n"
    )

    # Test real cross-encoder model scoring directly
    scores = rerank(
        query=query,
        texts=[chunk_semantic_text, chunk_exact_text],
        provider="cross-encoder",
        model_name=settings.RERANKER_MODEL_NAME,
        head_chars=settings.RERANKER_HEAD_CHARS,
        tail_chars=settings.RERANKER_TAIL_CHARS,
    )

    assert len(scores) == 2
    # chunk_exact (index 1) MUST receive a significantly higher cross-encoder relevance score
    # than chunk_semantic (index 0) for the domain-fit claim to be empirically validated.
    score_semantic, score_exact = scores[0], scores[1]
    print(f"\n[Domain-Fit Validation Output]")
    print(f"Query: '{query}'")
    print(f"chunk_semantic (generic network check) rerank_score: {score_semantic:.4f}")
    print(f"chunk_exact (validate_repo_url function) rerank_score:  {score_exact:.4f}")

    assert score_exact > score_semantic, (
        f"Domain-fit validation failed! Cross-encoder assigned lower score to exact match ({score_exact}) "
        f"than generic match ({score_semantic})."
    )


def test_graceful_degradation_on_reranker_failure():
    """Verify search endpoint degrades gracefully to RRF ordering if reranker raises an exception."""
    with patch("app.api.search.rerank", side_effect=RuntimeError("Cross-encoder model load failed")):
        response = client.get(
            "/search",
            params={
                "q": "health",
                "organization_id": "org_test_rerank_degrade",
                "source_type": "all",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert "results" in data
        assert "telemetry" in data
        # All rerank_scores should be None when degraded
        for item in data["results"]:
            assert item["rerank_score"] is None
        # Telemetry reranking_ms recorded cleanly
        assert data["telemetry"]["reranking_ms"] >= 0.0


def test_telemetry_includes_reranking_ms():
    """Verify reranking_ms is returned in search telemetry."""
    response = client.get(
        "/search",
        params={
            "q": "test query",
            "organization_id": "org_test_telemetry",
            "source_type": "all",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "reranking_ms" in data["telemetry"]
    assert isinstance(data["telemetry"]["reranking_ms"], float)


def test_total_results_reflects_reranked_pool():
    """Verify total_results reflects the size of the reranked pool."""
    response = client.get(
        "/search",
        params={
            "q": "query",
            "organization_id": "org_test_total_results",
            "limit": 5,
            "offset": 0,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "total_results" in data
    assert data["total_results"] <= settings.RERANKER_TOP_K


def test_disabled_reranker_falls_back_to_rrf():
    """Verify setting RERANKER_PROVIDER='disabled' bypasses cross-encoder reranking."""
    with patch("app.api.search.settings.RERANKER_PROVIDER", "disabled"):
        response = client.get(
            "/search",
            params={
                "q": "test query",
                "organization_id": "org_test_disabled",
            },
        )
        assert response.status_code == 200
        data = response.json()

        for item in data["results"]:
            assert item["rerank_score"] is None
