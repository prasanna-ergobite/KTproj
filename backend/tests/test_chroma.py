import sys
import uuid
from pathlib import Path
import pytest

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.db.chroma_client import (
    chroma_client,
    ChromaValidationError,
    ChromaCollectionError,
    COLLECTION_NAMES,
)

# Isolated test org IDs
TEST_ORG_A = f"test-org-chroma-{uuid.uuid4().hex[:8]}"
TEST_ORG_B = f"test-org-chroma-{uuid.uuid4().hex[:8]}"
TEST_COLLECTION = "docs_chunks"

# Valid embeddings matching Nomic 768-dim schema
EMB_DIM = 768
EMB_1 = [0.1] * EMB_DIM
EMB_2 = [0.5] * EMB_DIM



@pytest.fixture(scope="module", autouse=True)
def setup_chroma():
    """Connect and ensure all collections exist before any test runs.
    Tests run on the host machine, so use localhost:8001 (host-mapped port).
    Inside Docker, the backend uses chroma:8000 via container networking.
    """
    chroma_client.host = "localhost"
    chroma_client.port = 8001
    chroma_client.connect()
    chroma_client.ensure_collections()
    yield
    # Module-level teardown: clean up any leftover test data
    for org_id in (TEST_ORG_A, TEST_ORG_B):
        for col in COLLECTION_NAMES:
            try:
                chroma_client.delete_by_metadata(col, organization_id=org_id)
            except Exception:
                pass
    chroma_client.close()


# ---------------------------------------------------------------------------
# Test 1: Connection and collections
# ---------------------------------------------------------------------------

def test_connection_and_collections():
    """Heartbeat succeeds and all 3 collections exist after ensure_collections()."""
    # Heartbeat should not raise
    chroma_client.client.heartbeat()

    # In chromadb v1, list_collections() returns Collection objects with a .name attribute
    existing = {col.name for col in chroma_client.client.list_collections()}
    for name in COLLECTION_NAMES:
        assert name in existing, f"Expected collection '{name}' to exist"


# ---------------------------------------------------------------------------
# Test 2: Round-trip add / query
# ---------------------------------------------------------------------------

def test_add_and_query_round_trip():
    """Insert 2 docs, query by embedding, assert correct IDs and metadata returned."""
    try:
        chroma_client.add_documents(
            collection_name=TEST_COLLECTION,
            documents=["doc alpha content", "doc beta content"],
            embeddings=[EMB_1, EMB_2],
            ids=["doc-alpha", "doc-beta"],
            organization_id=TEST_ORG_A,
            module_id="mod-001",
            source_type="doc",
            project_id="proj-001",
        )

        result = chroma_client.query(
            collection_name=TEST_COLLECTION,
            query_embeddings=[EMB_1],
            n_results=2,
            organization_id=TEST_ORG_A,
        )

        returned_ids = result["ids"][0]
        assert "doc-alpha" in returned_ids, "doc-alpha should be in query results"

        # Confirm metadata matches
        for meta in result["metadatas"][0]:
            assert meta["organization_id"] == TEST_ORG_A
            assert meta["module_id"] == "mod-001"
            assert meta["source_type"] == "doc"

    finally:
        chroma_client.delete_by_metadata(TEST_COLLECTION, organization_id=TEST_ORG_A)


# ---------------------------------------------------------------------------
# Test 3: Tenant isolation (positive + negative assertions)
# ---------------------------------------------------------------------------

def test_tenant_isolation():
    """
    Org B must not see org A's data (negative path).
    Org A must still see its own data (positive path — catches over-filtering bugs).
    """
    try:
        chroma_client.add_documents(
            collection_name=TEST_COLLECTION,
            documents=["org A doc 1", "org A doc 2"],
            embeddings=[EMB_1, EMB_2],
            ids=["iso-doc-1", "iso-doc-2"],
            organization_id=TEST_ORG_A,
            source_type="doc",
        )

        # Positive assertion: org A can see its own docs via get()
        org_a_result = chroma_client.get_documents(
            collection_name=TEST_COLLECTION,
            organization_id=TEST_ORG_A,
        )
        assert len(org_a_result["ids"]) == 2, (
            f"Org A should see 2 documents, got {len(org_a_result['ids'])}"
        )

        # Negative assertion: org B sees nothing from org A's data
        org_b_result = chroma_client.get_documents(
            collection_name=TEST_COLLECTION,
            organization_id=TEST_ORG_B,
        )
        assert len(org_b_result["ids"]) == 0, (
            f"Tenant isolation failure: org B saw {len(org_b_result['ids'])} documents belonging to org A"
        )

    finally:
        chroma_client.delete_by_metadata(TEST_COLLECTION, organization_id=TEST_ORG_A)


# ---------------------------------------------------------------------------
# Test 4: Delete by metadata
# ---------------------------------------------------------------------------

def test_delete_by_metadata():
    """Add docs, delete by org, verify via get() that collection is empty for that org."""
    chroma_client.add_documents(
        collection_name=TEST_COLLECTION,
        documents=["delete me 1", "delete me 2"],
        embeddings=[EMB_1, EMB_2],
        ids=["del-doc-1", "del-doc-2"],
        organization_id=TEST_ORG_A,
        source_type="doc",
    )

    # Confirm inserted
    before = chroma_client.get_documents(TEST_COLLECTION, organization_id=TEST_ORG_A)
    assert len(before["ids"]) == 2

    # Delete
    chroma_client.delete_by_metadata(TEST_COLLECTION, organization_id=TEST_ORG_A)

    # Verify empty via get(), not query()
    after = chroma_client.get_documents(TEST_COLLECTION, organization_id=TEST_ORG_A)
    assert len(after["ids"]) == 0, (
        f"Expected 0 documents after delete, got {len(after['ids'])}"
    )


# ---------------------------------------------------------------------------
# Test 5: organization_id validation
# ---------------------------------------------------------------------------

def test_empty_organization_id_raises():
    """add_documents() with empty/None org_id must raise ChromaValidationError before touching Chroma."""
    with pytest.raises(ChromaValidationError):
        chroma_client.add_documents(
            collection_name=TEST_COLLECTION,
            documents=["should not be stored"],
            embeddings=[EMB_1],
            ids=["bad-doc"],
            organization_id="",
        )

    with pytest.raises(ChromaValidationError):
        chroma_client.add_documents(
            collection_name=TEST_COLLECTION,
            documents=["should not be stored"],
            embeddings=[EMB_1],
            ids=["bad-doc"],
            organization_id="   ",
        )


if __name__ == "__main__":
    pytest.main([__file__, "-s"])
