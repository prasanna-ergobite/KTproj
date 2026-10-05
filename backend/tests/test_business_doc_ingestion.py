"""
Integration tests for Business Document Upload & Ingestion Pipeline (4 integration tests).
"""

import sys
import uuid
import time
import io
import hashlib
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

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

TEST_ORG_ID = f"test-org-bdoc-{uuid.uuid4().hex[:8]}"
TEST_REPO_ID = "repo:chatdoc"
TEST_MODULE_ID = "repo:chatdoc:module:auth"

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def setup_environment_and_db():
    """Ensure database connection, system user sentinel, schema constraints, and seed Repo/Module."""
    neo4j_client.connect()
    neo4j_client.create_constraints()
    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        pass
    settings.EMBEDDING_PROVIDER = "stub"

    with SessionLocal() as db:
        ensure_system_user(db)

    # Seed Repository and Module nodes in Neo4j for test linking
    cypher_seed_repo = (
        "MERGE (r:Repository {tenant_key: $repo_tk}) "
        "SET r.id = $repo_id, r.organization_id = $org_id, r.name = 'ChatDoc' "
        "RETURN r"
    )
    neo4j_client.run_write_query(
        cypher_seed_repo,
        {"repo_tk": f"{TEST_ORG_ID}:{TEST_REPO_ID}", "repo_id": TEST_REPO_ID, "org_id": TEST_ORG_ID},
        organization_id=TEST_ORG_ID,
    )

    cypher_seed_mod = (
        "MERGE (m:Module {tenant_key: $mod_tk}) "
        "SET m.id = $mod_id, m.organization_id = $org_id, m.name = 'auth', m.path = 'src/auth' "
        "RETURN m"
    )
    neo4j_client.run_write_query(
        cypher_seed_mod,
        {"mod_tk": f"{TEST_ORG_ID}:{TEST_MODULE_ID}", "mod_id": TEST_MODULE_ID, "org_id": TEST_ORG_ID},
        organization_id=TEST_ORG_ID,
    )

    yield

    # Teardown Neo4j nodes
    try:
        neo4j_client.run_write_query(
            "MATCH (n) WHERE n.organization_id = $org_id DETACH DELETE n",
            {"org_id": TEST_ORG_ID},
            organization_id=TEST_ORG_ID,
        )
    except Exception:
        pass

    # Teardown Chroma chunks
    try:
        chroma_client.delete_by_metadata("docs_chunks", organization_id=TEST_ORG_ID)
    except Exception:
        pass

    # Teardown Postgres tasks
    with SessionLocal() as db:
        try:
            db.query(Task).filter(Task.payload["organization_id"].astext == TEST_ORG_ID).delete(synchronize_session=False)
            db.commit()
        except Exception:
            pass


def _poll_task(task_id: str, timeout_seconds: float = 10.0) -> dict:
    """Helper polling business doc task status until completion."""
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        res = client.get(f"/business-docs/tasks/{task_id}")
        assert res.status_code == 200
        data = res.json()
        if data["status"] in ("completed", "completed_without_embeddings", "failed"):
            return data
        time.sleep(0.2)
    pytest.fail(f"Task {task_id} timed out after {timeout_seconds}s")


# ---------------------------------------------------------------------------
# Test 1: Upload business doc creates Document node and Repository edge
# ---------------------------------------------------------------------------

def test_business_doc_upload_creates_document_node_and_repo_edge():
    """Test 1: Uploading a PRD creates Document node in Neo4j and DOCUMENTS edge to Repository."""
    content = b"# Product Requirements Document\nThis document outlines authentication flow requirements.\n"
    files = [("files", ("prd-auth.md", io.BytesIO(content), "text/markdown"))]
    data = {
        "organization_id": TEST_ORG_ID,
        "repository_id": TEST_REPO_ID,
        "doc_type": "prd",
    }

    res = client.post("/business-docs/upload", data=data, files=files)
    assert res.status_code == 202
    task_id = res.json()["task_id"]

    task_data = _poll_task(task_id)
    assert task_data["status"] == "completed"
    assert task_data["result"]["files_processed"] == 1

    # Neo4j verification
    cypher_check = (
        "MATCH (d:Document {organization_id: $org_id, doc_type: 'prd'}) "
        "MATCH (d)-[:DOCUMENTS]->(r:Repository {id: $repo_id}) "
        "RETURN d.filename AS fn, d.relative_path AS rp, d.source_type AS st, d.repository_id AS rid"
    )
    records = neo4j_client.run_read_query(
        cypher_check,
        {"org_id": TEST_ORG_ID, "repo_id": TEST_REPO_ID},
        organization_id=TEST_ORG_ID,
    )
    assert len(records) == 1
    assert records[0]["fn"] == "prd-auth.md"
    assert records[0]["rp"] == ""
    assert records[0]["st"] == "doc"
    assert records[0]["rid"] == TEST_REPO_ID


# ---------------------------------------------------------------------------
# Test 2: Business doc is distinguishable by doc_type in ChromaDB & Search
# ---------------------------------------------------------------------------

def test_business_doc_is_distinguishable_by_doc_type_in_chroma():
    """Test 2: ChromaDB docs_chunks query with doc_type filter isolates business doc chunks (bidirectional)."""
    # Ingest tech doc
    tech_content = b"# Technical Setup Guide\nRun npm install and npm start.\n"
    tech_res = client.post(
        "/docs/upload",
        data={"relative_paths": ["docs/setup.md"], "organization_id": TEST_ORG_ID, "source_type": "doc"},
        files=[("files", ("setup.md", io.BytesIO(tech_content), "text/markdown"))],
    )
    assert tech_res.status_code == 202
    tech_task = _poll_task(tech_res.json()["task_id"])
    assert tech_task["status"] == "completed"

    # Positive Case: Query Chroma for doc_type == "prd" -> returns ONLY business doc chunk IDs
    chroma_prd = chroma_client.get_documents(
        "docs_chunks",
        organization_id=TEST_ORG_ID,
        extra_filter={"doc_type": {"$eq": "prd"}},
    )
    assert chroma_prd and chroma_prd.get("ids")
    for cid in chroma_prd["ids"]:
        assert "bdoc:" in cid
        assert "doc:docs/setup.md" not in cid

    # Negative Case 1: Query Chroma for doc_type == "nonexistent_type" -> returns empty list
    chroma_empty = chroma_client.get_documents(
        "docs_chunks",
        organization_id=TEST_ORG_ID,
        extra_filter={"doc_type": {"$eq": "nonexistent_type"}},
    )
    assert chroma_empty and len(chroma_empty.get("ids", [])) == 0

    # Positive Search API test with doc_type="prd" -> returns business doc
    search_prd = client.get(
        f"/search?q=authentication&organization_id={TEST_ORG_ID}&source_type=doc&doc_type=prd"
    )
    assert search_prd.status_code == 200
    s_prd_data = search_prd.json()
    assert s_prd_data["total_results"] > 0
    for item in s_prd_data["results"]:
        assert item["metadata"].get("doc_type") == "prd"
        assert item["metadata"].get("relative_path") == ""

    # Negative Search API test with doc_type="prd" querying technical doc term "npm" -> tech doc setup.md excluded
    search_tech_excluded = client.get(
        f"/search?q=npm&organization_id={TEST_ORG_ID}&source_type=doc&doc_type=prd"
    )
    assert search_tech_excluded.status_code == 200
    s_excluded_data = search_tech_excluded.json()
    for item in s_excluded_data["results"]:
        assert item["metadata"].get("relative_path") != "docs/setup.md"


# ---------------------------------------------------------------------------
# Test 3: Uploading same business doc twice triggers dedup skip
# ---------------------------------------------------------------------------

def test_business_doc_dedup_same_content_is_skipped():
    """Test 3: Re-uploading identical business doc content triggers dedup skip."""
    content = b"# Unique Business Plan\nRevenue model details for Q3.\n"
    files = [("files", ("biz-plan.md", io.BytesIO(content), "text/markdown"))]
    data = {
        "organization_id": TEST_ORG_ID,
        "repository_id": TEST_REPO_ID,
        "doc_type": "business",
    }

    # Upload 1
    res1 = client.post("/business-docs/upload", data=data, files=files)
    assert res1.status_code == 202
    t1 = _poll_task(res1.json()["task_id"])
    assert t1["result"]["files_processed"] == 1

    # Upload 2 (same content)
    files2 = [("files", ("biz-plan.md", io.BytesIO(content), "text/markdown"))]
    res2 = client.post("/business-docs/upload", data=data, files=files2)
    assert res2.status_code == 202
    t2 = _poll_task(res2.json()["task_id"])
    assert t2["result"]["files_skipped"] == 1

    # Verify Neo4j has exactly 1 Document node for biz-plan.md
    cypher_count = (
        "MATCH (d:Document {organization_id: $org_id, filename: 'biz-plan.md'}) "
        "RETURN count(d) AS cnt"
    )
    recs = neo4j_client.run_read_query(
        cypher_count,
        {"org_id": TEST_ORG_ID},
        organization_id=TEST_ORG_ID,
    )
    assert recs[0]["cnt"] == 1


# ---------------------------------------------------------------------------
# Test 4: Business doc with module_id creates both Repository and Module edges
# ---------------------------------------------------------------------------

def test_business_doc_with_module_id_creates_both_edges():
    """Test 4: Business doc uploaded with module_id creates edges to both Repository and Module."""
    content = b"# Auth Spec\nDetailed spec for the auth module.\n"
    files = [("files", ("auth-spec.md", io.BytesIO(content), "text/markdown"))]
    data = {
        "organization_id": TEST_ORG_ID,
        "repository_id": TEST_REPO_ID,
        "module_id": TEST_MODULE_ID,
        "doc_type": "requirements",
    }

    res = client.post("/business-docs/upload", data=data, files=files)
    assert res.status_code == 202
    t_data = _poll_task(res.json()["task_id"])
    assert t_data["status"] == "completed"

    # Verify Repo edge
    cypher_repo = (
        "MATCH (d:Document {filename: 'auth-spec.md', organization_id: $org_id})-[:DOCUMENTS]->(r:Repository {id: $repo_id}) "
        "RETURN count(d) AS cnt"
    )
    repo_recs = neo4j_client.run_read_query(cypher_repo, {"org_id": TEST_ORG_ID, "repo_id": TEST_REPO_ID}, organization_id=TEST_ORG_ID)
    assert repo_recs[0]["cnt"] == 1

    # Verify Module edge
    cypher_mod = (
        "MATCH (d:Document {filename: 'auth-spec.md', organization_id: $org_id})-[:DOCUMENTS]->(m:Module {id: $mod_id}) "
        "RETURN count(d) AS cnt"
    )
    mod_recs = neo4j_client.run_read_query(cypher_mod, {"org_id": TEST_ORG_ID, "mod_id": TEST_MODULE_ID}, organization_id=TEST_ORG_ID)
    assert mod_recs[0]["cnt"] == 1
