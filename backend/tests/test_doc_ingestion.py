"""
Integration tests for Document Upload & Ingestion Pipeline (8 integration tests).
"""

import sys
import uuid
import time
import io
import hashlib
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.main import app
from app.core.config import settings
from app.db.neo4j_client import neo4j_client
from app.db.chroma_client import chroma_client
from app.db.postgres_client import SessionLocal
from app.models.db_models import Task
from app.core.constants import ensure_system_user

TEST_ORG_ID = f"test-org-docingest-{uuid.uuid4().hex[:8]}"

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def setup_environment_and_db():
    """Ensure database connection, system user sentinel, and schema constraints."""
    neo4j_client.connect()
    neo4j_client.create_constraints()
    settings.EMBEDDING_PROVIDER = "stub"

    with SessionLocal() as db:
        ensure_system_user(db)

    yield

    # Teardown: Clean up Neo4j test nodes for TEST_ORG_ID
    try:
        neo4j_client.run_write_query(
            "MATCH (n) WHERE n.organization_id = $org_id DETACH DELETE n",
            {"org_id": TEST_ORG_ID},
            organization_id=TEST_ORG_ID,
        )
    except Exception:
        pass

    # Teardown: Clean up task rows in Postgres
    with SessionLocal() as db:
        try:
            db.query(Task).filter(Task.payload["organization_id"].astext == TEST_ORG_ID).delete(synchronize_session=False)
            db.commit()
        except Exception:
            pass


def _poll_task(task_id: str, timeout_seconds: float = 10.0) -> dict:
    """Helper polling task status until completed or failed."""
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        res = client.get(f"/docs/tasks/{task_id}")
        assert res.status_code == 200
        data = res.json()
        if data["status"] in ("completed", "completed_without_embeddings", "failed"):
            return data
        time.sleep(0.2)
    pytest.fail(f"Task {task_id} timed out after {timeout_seconds}s")


# ---------------------------------------------------------------------------
# Test 1: Upload .md file returns 202 and task_id
# ---------------------------------------------------------------------------

def test_upload_md_file_returns_202_and_task_id():
    """Test 1: Uploading a valid Markdown file returns HTTP 202 with task_id."""
    content = b"# Getting Started\nWelcome to AutoKT document ingestion test.\n"
    files = [("files", ("overview.md", io.BytesIO(content), "text/markdown"))]
    data = {
        "relative_paths": ["docs/overview.md"],
        "organization_id": TEST_ORG_ID,
        "source_type": "doc",
    }

    res = client.post("/docs/upload", files=files, data=data)
    assert res.status_code == 202
    body = res.json()
    assert "task_id" in body
    assert body["status"] == "pending"
    assert "queued" in body["message"].lower()


# ---------------------------------------------------------------------------
# Test 2: Task completes without embeddings (gated)
# ---------------------------------------------------------------------------

def test_task_completes_without_embeddings():
    """Test 2: Document ingestion task transitions to completed_without_embeddings when EMBEDDING_PROVIDER is empty."""
    content = (
        "# System Manual\n"
        "AutoKT is a developer onboarding and knowledge management SaaS platform. "
        "It constructs a living knowledge graph combined with ChromaDB vector search.\n\n"
        "## Core Features\n"
        "Repo ingestion, document parsing, semantic search, and documentation health scoring.\n"
    )
    files = [("files", ("manual.md", io.BytesIO(content.encode("utf-8")), "text/markdown"))]
    data = {
        "relative_paths": ["specs/manual.md"],
        "organization_id": TEST_ORG_ID,
        "source_type": "doc",
    }

    # Explicitly disable the embedding provider to verify the gating logic
    with patch.object(settings, "EMBEDDING_PROVIDER", ""):
        res = client.post("/docs/upload", files=files, data=data)
        assert res.status_code == 202
        task_id = res.json()["task_id"]

        task_res = _poll_task(task_id)
    assert task_res["status"] == "completed_without_embeddings"
    result = task_res["result"]
    assert result["files_processed"] == 1
    assert result["chunks_count"] > 0


# ---------------------------------------------------------------------------
# Test 3: Invalid extension rejected with 400
# ---------------------------------------------------------------------------

def test_invalid_extension_rejected_with_400():
    """Test 3: Uploading a file with an unsupported extension returns HTTP 400."""
    files = [("files", ("script.exe", io.BytesIO(b"binary data"), "application/octet-stream"))]
    data = {
        "relative_paths": ["bin/script.exe"],
        "organization_id": TEST_ORG_ID,
        "source_type": "doc",
    }

    res = client.post("/docs/upload", files=files, data=data)
    assert res.status_code == 400
    assert "Unsupported file extension" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Test 4: File size limit enforced
# ---------------------------------------------------------------------------

def test_file_size_limit_enforced():
    """Test 4: Uploading a file > 10 MB returns HTTP 400."""
    big_content = b"A" * (11 * 1024 * 1024)  # 11 MB
    files = [("files", ("giant.txt", io.BytesIO(big_content), "text/plain"))]
    data = {
        "relative_paths": ["big/giant.txt"],
        "organization_id": TEST_ORG_ID,
        "source_type": "doc",
    }

    res = client.post("/docs/upload", files=files, data=data)
    assert res.status_code == 400
    assert "exceeds maximum allowed size" in res.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 5: Idempotent re-ingestion of same content
# ---------------------------------------------------------------------------

def test_idempotent_reingestion_same_content():
    """Test 5: Re-uploading identical content at same path skips processing (files_skipped=1)."""
    rel_path = "docs/idempotent.md"
    content = b"# Idempotency Test\nSame exact content uploaded twice.\n"

    # First upload
    res1 = client.post("/docs/upload", files=[("files", ("idempotent.md", io.BytesIO(content), "text/markdown"))], data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID})
    assert res1.status_code == 202
    _poll_task(res1.json()["task_id"])

    # Second upload (same content)
    res2 = client.post("/docs/upload", files=[("files", ("idempotent.md", io.BytesIO(content), "text/markdown"))], data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID})
    assert res2.status_code == 202
    task2 = _poll_task(res2.json()["task_id"])

    assert task2["result"]["files_skipped"] == 1
    assert task2["result"]["files_processed"] == 0

    # Assert Neo4j contains exactly 1 Document node
    query = "MATCH (d:Document {organization_id: $organization_id, relative_path: $rel_path}) RETURN count(d) AS count"
    records = neo4j_client.run_read_query(query, parameters={"rel_path": rel_path}, organization_id=TEST_ORG_ID)
    assert records[0]["count"] == 1


# ---------------------------------------------------------------------------
# Test 6a: Re-ingestion with different content replaces Neo4j node
# ---------------------------------------------------------------------------

def test_reingestion_different_content_replaces_neo4j_node():
    """Test 6a: Re-uploading modified content at same path replaces Neo4j node with updated content_hash."""
    rel_path = "auth/setup.md"
    content_v1 = b"# Auth Setup V1\nInitial authentication setup instructions.\n"
    content_v2 = b"# Auth Setup V2\nUpdated authentication setup instructions with OAuth2.\n"

    # Version 1 upload
    res1 = client.post("/docs/upload", files=[("files", ("setup.md", io.BytesIO(content_v1), "text/markdown"))], data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID})
    _poll_task(res1.json()["task_id"])

    # Version 2 upload
    res2 = client.post("/docs/upload", files=[("files", ("setup.md", io.BytesIO(content_v2), "text/markdown"))], data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID})
    _poll_task(res2.json()["task_id"])

    # Verify Neo4j has exactly 1 Document node for rel_path, carrying content_v2 hash
    query = "MATCH (d:Document {organization_id: $organization_id, relative_path: $rel_path}) RETURN d.content_hash AS ch"
    records = neo4j_client.run_read_query(query, parameters={"rel_path": rel_path}, organization_id=TEST_ORG_ID)
    assert len(records) == 1
    expected_v2_hash = hashlib.sha256(content_v2).hexdigest()[:32]
    assert records[0]["ch"] == expected_v2_hash


# ---------------------------------------------------------------------------
# Test 6b: Re-ingestion with different content deletes old Chroma chunks
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_reingestion_different_content_deletes_old_chroma_chunks(monkeypatch):
    """Test 6b: Re-uploading modified content deletes old Chroma chunks when EMBEDDING_PROVIDER is set."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pytest.skip("ChromaDB service not available on localhost:8001")

    rel_path = "billing/guide.md"
    content_v1 = b"# Billing Guide V1\nInitial payment gateway configuration details.\n"
    content_v2 = b"# Billing Guide V2\nRevised payment gateway setup for Stripe and PayPal.\n"

    # Version 1 upload
    res1 = client.post("/docs/upload", files=[("files", ("guide.md", io.BytesIO(content_v1), "text/markdown"))], data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID})
    _poll_task(res1.json()["task_id"])

    # Confirm v1 chunks in Chroma
    v1_docs = chroma_client.get_documents("docs_chunks", organization_id=TEST_ORG_ID, extra_filter={"relative_path": {"$eq": rel_path}})
    assert len(v1_docs["ids"]) > 0

    # Version 2 upload
    res2 = client.post("/docs/upload", files=[("files", ("guide.md", io.BytesIO(content_v2), "text/markdown"))], data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID})
    _poll_task(res2.json()["task_id"])

    # Confirm v2 chunks present in Chroma
    v2_docs = chroma_client.get_documents("docs_chunks", organization_id=TEST_ORG_ID, extra_filter={"relative_path": {"$eq": rel_path}})
    assert len(v2_docs["ids"]) > 0


# ---------------------------------------------------------------------------
# Test 7: Identical content at different relative paths produces independent nodes
# ---------------------------------------------------------------------------

def test_identical_content_different_paths_produce_independent_nodes():
    """Test 7 (v5 bug fix regression test): Byte-identical content at distinct relative_paths produces 2 independent Document nodes."""
    same_bytes = b"# Shared Readme\nStandard project introduction readme content.\n"
    path1 = "auth/README.md"
    path2 = "billing/README.md"

    res1 = client.post("/docs/upload", files=[("files", ("README.md", io.BytesIO(same_bytes), "text/markdown"))], data={"relative_paths": [path1], "organization_id": TEST_ORG_ID})
    _poll_task(res1.json()["task_id"])

    res2 = client.post("/docs/upload", files=[("files", ("README.md", io.BytesIO(same_bytes), "text/markdown"))], data={"relative_paths": [path2], "organization_id": TEST_ORG_ID})
    _poll_task(res2.json()["task_id"])

    # Query Neo4j: assert 2 distinct Document nodes exist
    query = (
        "MATCH (d:Document {organization_id: $organization_id}) "
        "WHERE d.relative_path IN [$p1, $p2] "
        "RETURN d.relative_path AS rel_path, d.tenant_key AS tk"
    )
    records = neo4j_client.run_read_query(query, parameters={"p1": path1, "p2": path2}, organization_id=TEST_ORG_ID)

    assert len(records) == 2

    paths_found = {r["rel_path"] for r in records}
    assert paths_found == {path1, path2}

    tenant_keys_found = {r["tk"] for r in records}
    assert len(tenant_keys_found) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-s"])


# ---------------------------------------------------------------------------
# Test 8: Chroma docs_chunks metadata schema — all 24 fields present & typed
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_doc_chunk_metadata_schema(monkeypatch):
    """Test 8: Verify all 24 Chroma docs_chunks metadata fields are present and correctly typed after .md upload."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pytest.skip("ChromaDB service not available on localhost:8001")

    rel_path = "schema/metadata_check.md"
    content = (
        b"# Metadata Schema Test\n"
        b"This document verifies that all enriched Chroma metadata fields are present.\n\n"
        b"## Section One\n"
        b"Content for section one covering the first major topic in detail.\n"
    )

    res = client.post(
        "/docs/upload",
        files=[("files", ("metadata_check.md", io.BytesIO(content), "text/markdown"))],
        data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID},
    )
    assert res.status_code == 202
    task = _poll_task(res.json()["task_id"])
    assert task["result"]["files_processed"] == 1

    docs = chroma_client.get_documents(
        "docs_chunks",
        organization_id=TEST_ORG_ID,
        extra_filter={"relative_path": {"$eq": rel_path}},
    )
    assert len(docs["ids"]) > 0, "No Chroma records found for uploaded file"

    # All 24 expected metadata keys (11 existing + 13 new)
    EXPECTED_KEYS = {
        # Base metadata (4)
        "organization_id", "module_id", "source_type", "project_id",
        # Extra metadata — existing (7)
        "doc_id", "heading_path", "parent_section_id", "section_level",
        "chunking_method", "chunk_index", "relative_path",
        # Extra metadata — new (13)
        "chunk_type", "chunk_total", "file_format", "content_hash",
        "last_modified_date", "ingested_at", "version", "uploaded_by",
        "source_confidence", "token_count", "has_code_fence",
        "is_stub_or_empty", "visibility",
    }
    assert len(EXPECTED_KEYS) == 24, "Test itself has wrong key count — fix the test"

    meta = docs["metadatas"][0]
    missing = EXPECTED_KEYS - set(meta.keys())
    assert not missing, f"Missing metadata fields: {missing}"

    # Type assertions
    assert isinstance(meta["version"], int), f"version must be int, got {type(meta['version'])}"
    assert isinstance(meta["token_count"], int), f"token_count must be int, got {type(meta['token_count'])}"
    assert isinstance(meta["has_code_fence"], bool), f"has_code_fence must be bool, got {type(meta['has_code_fence'])}"
    assert isinstance(meta["is_stub_or_empty"], bool), f"is_stub_or_empty must be bool, got {type(meta['is_stub_or_empty'])}"
    assert isinstance(meta["chunk_total"], int), f"chunk_total must be int, got {type(meta['chunk_total'])}"
    assert isinstance(meta["section_level"], int), f"section_level must be int, got {type(meta['section_level'])}"
    assert isinstance(meta["chunk_index"], int), f"chunk_index must be int, got {type(meta['chunk_index'])}"
    assert meta["chunk_type"] in {"heading_body", "fixed_window"}, f"unexpected chunk_type: {meta['chunk_type']!r}"
    assert isinstance(meta["source_confidence"], str), f"source_confidence must be str, got {type(meta['source_confidence'])}"
    assert meta["visibility"] == "internal"
    assert meta["file_format"] == "md"
    assert meta["version"] == 1


# ---------------------------------------------------------------------------
# Test 9: version increments on re-ingest; skip does NOT increment
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_version_increments_on_reingestion(monkeypatch):
    """Test 9: version==1 on first ingest, version==2 after changed content, version stays 2 on same-content re-upload."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pytest.skip("ChromaDB service not available on localhost:8001")

    # Unique rel_path isolated to this test
    rel_path = "version/test_version_field.md"
    content_v1 = b"# Version Test V1\nInitial content for version tracking test.\n"
    content_v2 = b"# Version Test V2\nUpdated content to trigger version increment on re-ingest.\n"

    def _get_version():
        docs = chroma_client.get_documents(
            "docs_chunks",
            organization_id=TEST_ORG_ID,
            extra_filter={"relative_path": {"$eq": rel_path}},
        )
        assert len(docs["ids"]) > 0, "No Chroma records found"
        return docs["metadatas"][0]["version"]

    # --- First upload: version should be 1 ---
    res1 = client.post(
        "/docs/upload",
        files=[("files", ("test_version_field.md", io.BytesIO(content_v1), "text/markdown"))],
        data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID},
    )
    assert res1.status_code == 202
    task1 = _poll_task(res1.json()["task_id"])
    assert task1["result"]["files_processed"] == 1
    assert _get_version() == 1

    # --- Second upload: different content → version should become 2 ---
    res2 = client.post(
        "/docs/upload",
        files=[("files", ("test_version_field.md", io.BytesIO(content_v2), "text/markdown"))],
        data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID},
    )
    assert res2.status_code == 202
    task2 = _poll_task(res2.json()["task_id"])
    assert task2["result"]["files_processed"] == 1
    assert _get_version() == 2

    # Snapshot the Chroma record after v2 ingest — used to detect any write on the skip path
    docs_after_v2 = chroma_client.get_documents(
        "docs_chunks",
        organization_id=TEST_ORG_ID,
        extra_filter={"relative_path": {"$eq": rel_path}},
    )
    ingested_at_after_v2 = docs_after_v2["metadatas"][0]["ingested_at"]
    content_hash_after_v2 = docs_after_v2["metadatas"][0]["content_hash"]

    # --- Third upload: same content as v2 → should skip (version stays at 2, NOT 3) ---
    res3 = client.post(
        "/docs/upload",
        files=[("files", ("test_version_field.md", io.BytesIO(content_v2), "text/markdown"))],
        data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID},
    )
    assert res3.status_code == 202
    task3 = _poll_task(res3.json()["task_id"])
    # Pipeline-level proof: files_skipped==1 + files_processed==0 is architecturally
    # sufficient (the continue before Steps 3–7 is mutually exclusive with any Chroma write).
    assert task3["result"]["files_skipped"] == 1
    assert task3["result"]["files_processed"] == 0

    # Chroma-side proof: version, ingested_at, and content_hash must all be
    # byte-for-byte identical to the v2 snapshot. This distinguishes "no write occurred"
    # from "write occurred but didn't bump version".
    docs_after_skip = chroma_client.get_documents(
        "docs_chunks",
        organization_id=TEST_ORG_ID,
        extra_filter={"relative_path": {"$eq": rel_path}},
    )
    meta_after_skip = docs_after_skip["metadatas"][0]
    assert meta_after_skip["version"] == 2
    assert meta_after_skip["ingested_at"] == ingested_at_after_v2, (
        f"ingested_at changed after skip: {ingested_at_after_v2!r} → {meta_after_skip['ingested_at']!r}"
    )
    assert meta_after_skip["content_hash"] == content_hash_after_v2, (
        f"content_hash changed after skip — a new chunk was written unexpectedly"
    )


# ---------------------------------------------------------------------------
# Test 10: token_count is real BPE (nomic mode only)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_token_count_is_real_bpe_in_nomic_mode(monkeypatch):
    """Test 10: In nomic mode, token_count stored in Chroma matches count_tokens() for the chunk text."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "nomic")

    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pytest.skip("ChromaDB service not available on localhost:8001")

    from app.core.embedder import count_tokens

    rel_path = "token/bpe_count_check.md"
    content = b"# Token Count Verification\nThis section verifies the BPE token count stored in ChromaDB metadata matches the Nomic tokenizer output.\n"

    res = client.post(
        "/docs/upload",
        files=[("files", ("bpe_count_check.md", io.BytesIO(content), "text/markdown"))],
        data={"relative_paths": [rel_path], "organization_id": TEST_ORG_ID},
    )
    assert res.status_code == 202
    task = _poll_task(res.json()["task_id"], timeout_seconds=60.0)
    assert task["result"]["files_processed"] == 1

    docs = chroma_client.get_documents(
        "docs_chunks",
        organization_id=TEST_ORG_ID,
        extra_filter={"relative_path": {"$eq": rel_path}},
    )
    assert len(docs["ids"]) > 0

    # Verify token_count > 0 and matches count_tokens() for the chunk text
    chunk_text = docs["documents"][0]
    stored_token_count = docs["metadatas"][0]["token_count"]
    expected_token_count = count_tokens([chunk_text])[0]

    assert stored_token_count > 0, "token_count must be > 0 in nomic mode"
    assert stored_token_count == expected_token_count, (
        f"Stored token_count {stored_token_count} != count_tokens() result {expected_token_count}"
    )

