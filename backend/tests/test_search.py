"""
Integration tests for app/api/search.py (GET /search hybrid search endpoint).
"""

import sys
import io
import time
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.main import app
from app.core.config import settings
from app.db.neo4j_client import neo4j_client
from app.db.chroma_client import chroma_client

client = TestClient(app)
TEST_ORG_ID = "test-org-search-hybrid"


def _poll_task(task_id: str, timeout_seconds: float = 30.0) -> dict:
    """Helper to poll background task completion."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        res = client.get(f"/repos/tasks/{task_id}")
        assert res.status_code == 200
        data = res.json()
        if data["status"] in ("completed", "failed"):
            return data
        time.sleep(0.2)
    pytest.fail(f"Task {task_id} did not complete within {timeout_seconds}s")


# ---------------------------------------------------------------------------
# Test 1: Validation errors for missing required query params
# ---------------------------------------------------------------------------

def test_search_missing_org_id_returns_422():
    """Test 1: Omitting organization_id returns HTTP 422 Unprocessable Entity."""
    res = client.get("/search?q=authentication")
    assert res.status_code == 422


def test_search_empty_query_returns_422():
    """Test 1b: Empty query q returns HTTP 422 Unprocessable Entity."""
    res = client.get(f"/search?q=&organization_id={TEST_ORG_ID}")
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Test 2: End-to-End Hybrid Search in Stub Mode
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_search_end_to_end_stub_mode(monkeypatch):
    """Test 2: End-to-end ingestion + GET /search query execution with graph context."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
        neo4j_client.connect()
        neo4j_client.create_constraints()
    except Exception:
        pytest.skip("ChromaDB or Neo4j service not available on localhost")

    # Ingest a sample doc (unique path to prevent dedup skip across test suite runs)
    import uuid
    unique_id = uuid.uuid4().hex[:6]
    doc_path = f"auth/setup_{unique_id}.md"
    doc_content = (
        b"# Authentication Module\n"
        b"Setup guide for JWT authentication and token verification.\n"
    )
    res_doc = client.post(
        "/docs/upload",
        files=[("files", (f"setup_{unique_id}.md", io.BytesIO(doc_content), "text/markdown"))],
        data={"relative_paths": [doc_path], "organization_id": TEST_ORG_ID},
    )
    assert res_doc.status_code == 202
    task_doc = _poll_task(res_doc.json()["task_id"])
    assert task_doc["result"]["files_processed"] == 1

    # Execute GET /search
    res_search = client.get(
        f"/search?q=authentication&organization_id={TEST_ORG_ID}&source_type=all"
    )
    assert res_search.status_code == 200
    data = res_search.json()

    assert data["query"] == "authentication"
    assert data["organization_id"] == TEST_ORG_ID
    assert "telemetry" in data
    assert data["telemetry"]["total_ms"] >= 0.0
    assert data["total_results"] > 0
    assert len(data["results"]) > 0

    first = data["results"][0]
    assert "similarity_score" in first
    assert "graph_context" in first
    assert first["result_type"] in ("code", "doc")


# ---------------------------------------------------------------------------
# Test 3: Resilience on Neo4j Graph Failure
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_search_graph_resilience_on_neo4j_failure(monkeypatch):
    """Test 3: If Neo4j graph lookup fails, search returns vector results with empty graph context."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pytest.skip("ChromaDB service not available")

    # Mock Neo4j client run_read_query to raise an exception
    def mock_raise(*args, **kwargs):
        raise RuntimeError("Neo4j database connection lost")

    monkeypatch.setattr(neo4j_client, "run_read_query", mock_raise)

    res_search = client.get(
        f"/search?q=test&organization_id={TEST_ORG_ID}&source_type=all"
    )
    assert res_search.status_code == 200
    data = res_search.json()
    assert data["organization_id"] == TEST_ORG_ID
    # Vector search results still returned intact
    for item in data["results"]:
        assert item["graph_context"]["repository"] is None
        assert item["graph_context"]["module"] is None


# ---------------------------------------------------------------------------
# Test 4: Source Type Filtering (code vs doc)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_search_source_type_filtering(monkeypatch):
    """Test 4: Verify source_type='code' returns only code hits, source_type='doc' returns only doc hits."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pytest.skip("ChromaDB service not available")

    res_code = client.get(
        f"/search?q=test&organization_id={TEST_ORG_ID}&source_type=code"
    )
    assert res_code.status_code == 200
    for item in res_code.json()["results"]:
        assert item["result_type"] == "code"

    res_doc = client.get(
        f"/search?q=test&organization_id={TEST_ORG_ID}&source_type=doc"
    )
    assert res_doc.status_code == 200
    for item in res_doc.json()["results"]:
        assert item["result_type"] == "doc"


# ---------------------------------------------------------------------------
# Test 5: Pagination (limit & offset)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_search_pagination_limit_offset(monkeypatch):
    """Test 5: Verify limit and offset parameters paginate result items cleanly."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pytest.skip("ChromaDB service not available")

    res_p1 = client.get(
        f"/search?q=test&organization_id={TEST_ORG_ID}&limit=1&offset=0"
    )
    assert res_p1.status_code == 200
    p1_data = res_p1.json()
    assert p1_data["limit"] == 1
    assert p1_data["offset"] == 0

    res_p2 = client.get(
        f"/search?q=test&organization_id={TEST_ORG_ID}&limit=1&offset=1"
    )
    assert res_p2.status_code == 200
    p2_data = res_p2.json()
    assert p2_data["limit"] == 1
    assert p2_data["offset"] == 1


# ---------------------------------------------------------------------------
# Test 6: Answer Generation (RAG synthesis)
# ---------------------------------------------------------------------------

def test_search_answer_empty_context_returns_400():
    """Test: POST /search/answer with empty context_items returns HTTP 400."""
    res = client.post(
        "/search/answer",
        json={
            "query": "How do we authenticate?",
            "organization_id": TEST_ORG_ID,
            "context_items": [],
        },
    )
    assert res.status_code == 400
    assert "empty" in res.json()["detail"].lower()


def test_search_answer_successful_synthesis(monkeypatch):
    """Test: POST /search/answer returns synthesized answer and cited sources."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "stub")

    mock_context_items = [
        {
            "chunk_id": "code:chunk:1",
            "result_type": "code",
            "text": "def authenticate(token: str): return verify_jwt(token)",
            "rrf_score": 0.95,
            "rerank_score": 0.98,
            "metadata": {
                "file_path": "auth/service.py",
                "function_name": "authenticate",
                "chunk_type": "function",
            },
            "graph_context": {
                "module": {"id": "mod:auth", "name": "Authentication"},
                "owners": [{"name": "Alice", "email": "alice@example.com", "commit_count": 10}],
            },
        },
        {
            "chunk_id": "doc:chunk:2",
            "result_type": "doc",
            "text": "# Auth Architecture\nTokens expire after 1 hour.",
            "rrf_score": 0.85,
            "rerank_score": 0.88,
            "metadata": {
                "file_path": "docs/auth.md",
                "heading_path": "Auth Architecture",
                "chunk_type": "markdown",
            },
        },
    ]

    res = client.post(
        "/search/answer",
        json={
            "query": "How does authentication work?",
            "organization_id": TEST_ORG_ID,
            "context_items": mock_context_items,
            "top_k": 2,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["query"] == "How does authentication work?"
    assert data["organization_id"] == TEST_ORG_ID
    assert "answer" in data
    assert len(data["answer"]) > 0
    assert data["context_items_used"] == 2
    assert len(data["sources"]) == 2
    assert data["sources"][0]["chunk_id"] == "code:chunk:1"
    assert data["sources"][0]["module_name"] == "Authentication"
    assert "alice@example.com" in data["sources"][0]["owners"]


if __name__ == "__main__":
    pytest.main([__file__, "-s"])

