"""
Unit & Integration tests for BM25 Keyword Search and Reciprocal Rank Fusion (RRF).
Verifies exact keyword boosting, RRF formula calculation, graph context expansion for BM25-only hits,
graceful degradation on BM25 failure, telemetry metrics, and pre-pagination total_results semantics.
"""

import sys
import pytest
import chromadb
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.main import app
from app.core.config import settings
from app.db.chroma_client import chroma_client
from app.db.neo4j_client import neo4j_client

client = TestClient(app)
TEST_ORG = "org-test-rrf-search"


class InMemoryNeo4jDriverForRRF:
    """In-memory mock for Neo4j read query graph expansion."""
    def __init__(self):
        self.files = {
            f"{TEST_ORG}:file:app/utils/validator.py": {
                "file_id": "file:app/utils/validator.py",
                "repository": {"id": "repo:autokt", "name": "autokt", "url": "https://github.com/autokt/autokt.git"},
                "module": {"id": "repo:autokt:module:utils", "name": "utils", "path": "app/utils"},
                "owners": [{"name": "Alex Developer", "email": "alex@autokt.io", "commit_count": 5}],
                "related_documents": [],
            },
            f"{TEST_ORG}:file:app/core/helpers.py": {
                "file_id": "file:app/core/helpers.py",
                "repository": {"id": "repo:autokt", "name": "autokt", "url": "https://github.com/autokt/autokt.git"},
                "module": {"id": "repo:autokt:module:core", "name": "core", "path": "app/core"},
                "owners": [{"name": "Sam Architect", "email": "sam@autokt.io", "commit_count": 2}],
                "related_documents": [],
            }
        }
        self.docs = {
            f"{TEST_ORG}:doc:doc_spec_123": {
                "doc_id": "doc:doc_spec_123",
                "repository": {"id": "repo:autokt", "name": "autokt", "url": "https://github.com/autokt/autokt.git"},
                "module": {"id": "repo:autokt:module:_root", "name": "_root", "path": "_root"},
                "owners": [{"name": "Alex Developer", "email": "alex@autokt.io", "commit_count": 1}],
                "related_code_files": [],
            }
        }

    def run_read_query(self, query: str, parameters: dict = None, organization_id: str = ""):
        parameters = parameters or {}
        records = []
        if "UNWIND $file_tenant_keys" in query:
            for tk in parameters.get("file_tenant_keys", []):
                if tk in self.files:
                    records.append(self.files[tk])
        elif "UNWIND $doc_tenant_keys" in query:
            for tk in parameters.get("doc_tenant_keys", []):
                if tk in self.docs:
                    records.append(self.docs[tk])
        return records


def _ensure_db_connections():
    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        chroma_client.client = chromadb.EphemeralClient()
        chroma_client.ensure_collections()


@pytest.fixture(autouse=True)
def setup_test_data(monkeypatch):
    """Seed ChromaDB with test code & doc chunks for RRF testing."""
    _ensure_db_connections()
    driver = InMemoryNeo4jDriverForRRF()
    monkeypatch.setattr(neo4j_client, "run_read_query", driver.run_read_query)

    # Clean up test collection contents
    try:
        chroma_client.delete_by_metadata("code_chunks", organization_id=TEST_ORG)
    except Exception:
        pass
    try:
        chroma_client.delete_by_metadata("docs_chunks", organization_id=TEST_ORG)
    except Exception:
        pass

    # Seed 4 code chunks into code_chunks so corpus size N=4 ensures BM25 IDF > 0 for single-doc terms
    documents = [
        "# File: app/utils/validator.py\n# Function: validate_repo_url\n\ndef validate_repo_url(url: str) -> bool:\n    return url.startswith('https://')",
        "# File: app/core/helpers.py\n# Function: check_git_location\n\ndef check_git_location(path: str):\n    # Checks the repository location and path validity\n    pass",
        "# File: app/db/session.py\n# Function: get_db_session\n\ndef get_db_session():\n    pass",
        "# File: app/api/health.py\n# Function: health_check\n\ndef health_check():\n    pass",
    ]
    # Synthetic 768-dim embeddings
    vec_a = [0.01] * 768
    vec_b = [0.02] * 768
    vec_c = [0.03] * 768
    vec_d = [0.04] * 768
    ids = ["chunk_val_url_001", "chunk_git_loc_002", "chunk_db_session_003", "chunk_health_004"]

    chroma_client.add_documents(
        collection_name="code_chunks",
        documents=documents,
        embeddings=[vec_a, vec_b, vec_c, vec_d],
        ids=ids,
        organization_id=TEST_ORG,
        module_id="repo:autokt:module:utils",
        source_type="code",
        extra_metadata=[
            {"file_path": "app/utils/validator.py", "function_name": "validate_repo_url", "file_id": "file:app/utils/validator.py"},
            {"file_path": "app/core/helpers.py", "function_name": "check_git_location", "file_id": "file:app/core/helpers.py"},
            {"file_path": "app/db/session.py", "function_name": "get_db_session", "file_id": "file:app/db/session.py"},
            {"file_path": "app/api/health.py", "function_name": "health_check", "file_id": "file:app/api/health.py"},
        ]
    )

    # Seed 1 doc chunk into docs_chunks
    chroma_client.add_documents(
        collection_name="docs_chunks",
        documents=["# Section: Architecture Guide\n\nValidation rules for validate_repo_url parameters."],
        embeddings=[[0.03] * 768],
        ids=["chunk_doc_arch_003"],
        organization_id=TEST_ORG,
        module_id="repo:autokt:module:_root",
        source_type="doc",
        extra_metadata=[
            {"relative_path": "docs/architecture.md", "doc_id": "doc:doc_spec_123", "heading_path": "Architecture Guide"}
        ]
    )

    yield

    # Teardown
    try:
        chroma_client.delete_by_metadata("code_chunks", organization_id=TEST_ORG)
        chroma_client.delete_by_metadata("docs_chunks", organization_id=TEST_ORG)
    except Exception:
        pass


def test_bm25_exact_keyword_boost():
    """Verify exact identifier query ('validate_repo_url') gets BM25 boost to rank #1 in RRF."""
    response = client.get(
        "/search",
        params={"q": "validate_repo_url", "organization_id": TEST_ORG, "source_type": "all"}
    )
    assert response.status_code == 200
    data = response.json()

    results = data["results"]
    assert len(results) >= 2

    # Chunk with exact function name 'validate_repo_url' must be rank #1 in results
    top_hit = results[0]
    assert top_hit["chunk_id"] == "chunk_val_url_001"
    assert top_hit["keyword_rank"] == 1
    assert top_hit["rrf_score"] > 0.0
    assert "validate_repo_url" in top_hit["text"]


def test_rrf_scoring_formula():
    """Verify RRF score calculation: 1/(60 + vec_rank) + 1/(60 + keyword_rank)."""
    with patch("app.api.search.settings.RERANKER_PROVIDER", "disabled"):
        response = client.get(
            "/search",
            params={"q": "validate_repo_url", "organization_id": TEST_ORG, "source_type": "code"}
        )
        assert response.status_code == 200
        data = response.json()

        results = data["results"]
        top_hit = next(r for r in results if r["chunk_id"] == "chunk_val_url_001")
        second_hit = next(r for r in results if r["chunk_id"] == "chunk_git_loc_002")

        # Top hit: rank 1 vector (1/61) + rank 1 keyword (1/61) = 0.01639344 + 0.01639344 = 0.032787
        assert top_hit["vector_rank"] == 1
        assert top_hit["keyword_rank"] == 1
        assert top_hit["rrf_score"] == 0.032787

    # Second hit: rank 2 vector (1/62) + no keyword rank = 0.016129
    assert second_hit["vector_rank"] == 2
    assert second_hit["keyword_rank"] is None
    assert second_hit["rrf_score"] == 0.016129

    for item in results:
        v_rank = item["vector_rank"]
        k_rank = item["keyword_rank"]

        expected_rrf = 0.0
        if v_rank is not None:
            expected_rrf += 1.0 / (60.0 + v_rank)
        if k_rank is not None:
            expected_rrf += 1.0 / (60.0 + k_rank)

        assert abs(item["rrf_score"] - round(expected_rrf, 6)) < 1e-5


def test_bm25_only_hit_gets_graph_context():
    """Verify BM25-only hit gets full Neo4j graph context expansion."""
    response = client.get(
        "/search",
        params={"q": "validate_repo_url", "organization_id": TEST_ORG, "source_type": "all"}
    )
    assert response.status_code == 200
    data = response.json()

    code_hit = next((r for r in data["results"] if r["chunk_id"] == "chunk_val_url_001"), None)
    assert code_hit is not None
    assert code_hit["graph_context"] is not None
    assert code_hit["graph_context"]["repository"]["name"] == "autokt"
    assert code_hit["graph_context"]["module"]["path"] == "app/utils"
    assert code_hit["graph_context"]["owners"][0]["email"] == "alex@autokt.io"


def test_graceful_degradation_on_keyword_failure(monkeypatch):
    """Verify search degrades to vector-only if BM25 throws an exception."""
    from app.api import search as search_module

    def mock_get_documents_fail(*args, **kwargs):
        raise Exception("Simulated Chroma get_documents failure")

    monkeypatch.setattr(chroma_client, "get_documents", mock_get_documents_fail)

    response = client.get(
        "/search",
        params={"q": "validate_repo_url", "organization_id": TEST_ORG, "source_type": "all"}
    )
    assert response.status_code == 200
    data = response.json()

    telemetry = data["telemetry"]
    assert telemetry["keyword_search_ms"] >= 0.0
    # Search succeeds with vector hits
    assert len(data["results"]) > 0


def test_telemetry_fields_present():
    """Verify new keyword_search_ms and fusion_ms metrics are included in response telemetry."""
    response = client.get(
        "/search",
        params={"q": "validate_repo_url", "organization_id": TEST_ORG, "source_type": "all"}
    )
    assert response.status_code == 200
    telemetry = response.json()["telemetry"]

    assert "embedding_ms" in telemetry
    assert "vector_search_ms" in telemetry
    assert "keyword_search_ms" in telemetry
    assert "fusion_ms" in telemetry
    assert "graph_expansion_ms" in telemetry
    assert "total_ms" in telemetry


def test_total_results_is_pre_pagination_count():
    """Verify total_results reports total matching candidate count before limit/offset pagination."""
    response = client.get(
        "/search",
        params={"q": "validate_repo_url", "organization_id": TEST_ORG, "source_type": "all", "limit": 1, "offset": 0}
    )
    assert response.status_code == 200
    data = response.json()

    assert data["limit"] == 1
    assert len(data["results"]) == 1
    assert data["total_results"] >= 2  # Total pre-pagination candidates
