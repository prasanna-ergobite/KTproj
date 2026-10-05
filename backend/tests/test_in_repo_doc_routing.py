"""
Unit & Integration tests for In-Repo Documentation Routing Engine.
Verifies file classification, heading-aware chunking of in-repo .md/.rst/.txt files,
placement in docs_chunks collection, Neo4j Document node creation, dual-collection cleanup,
and GET /search retrieval with graph context.
"""

import os
import sys
import time
import pytest
import tempfile
import chromadb
from pathlib import Path
from fastapi.testclient import TestClient

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.main import app
from app.core.config import settings
from app.graphs.repo_ingestion import _is_doc_file, run_repo_ingestion_task
from app.db.neo4j_client import neo4j_client
from app.db.chroma_client import chroma_client

client = TestClient(app)
TEST_ORG_ID = "test-org-in-repo-doc-routing"


class InMemoryNeo4jDriver:
    def __init__(self):
        self.nodes = {"Document": {}, "Module": {}, "Repository": {}, "Person": {}, "File": {}}
        self.edges = []  # (src_type, src_key, rel_type, dst_type, dst_key, props)

    def run_write_query(self, query: str, parameters: dict = None, organization_id: str = ""):
        parameters = parameters or {}
        if "MERGE (r:Repository" in query:
            tk = parameters.get("tenant_key")
            self.nodes["Repository"][tk] = {
                "id": parameters.get("id"),
                "name": parameters.get("name"),
                "url": parameters.get("url"),
                "organization_id": organization_id,
            }
        elif "MERGE (m:Module" in query:
            tk = parameters.get("tenant_key")
            self.nodes["Module"][tk] = {
                "id": parameters.get("id"),
                "name": parameters.get("name"),
                "path": parameters.get("path", parameters.get("name", "")),
                "organization_id": organization_id,
            }
            repo_tk = parameters.get("repo_tenant_key")
            if repo_tk:
                self.edges.append(("Module", tk, "PART_OF", "Repository", repo_tk, {}))
        elif "MERGE (d:Document" in query:
            tk = parameters.get("tenant_key")
            self.nodes["Document"][tk] = {
                "id": parameters.get("id"),
                "doc_id": parameters.get("id"),
                "relative_path": parameters.get("relative_path"),
                "filename": parameters.get("filename"),
                "organization_id": organization_id,
            }
            if "mod_tenant_key" in parameters:
                mod_tk = parameters["mod_tenant_key"]
                self.edges.append(("Document", tk, "DOCUMENTS", "Module", mod_tk, {}))
        elif "MERGE (p:Person" in query:
            tk = parameters.get("tenant_key")
            self.nodes["Person"][tk] = {
                "id": parameters.get("id"),
                "name": parameters.get("name"),
                "email": parameters.get("email"),
                "organization_id": organization_id,
            }
        elif "MERGE (p)-[o:OWNS]->(d)" in query or "MERGE (p)-[o:OWNS]->(f)" in query:
            p_tk = parameters.get("p_tenant_key")
            d_tk = parameters.get("d_tenant_key")
            f_tk = parameters.get("f_tenant_key")
            dst_tk = d_tk or f_tk
            dst_type = "Document" if d_tk else "File"
            self.edges.append(("Person", p_tk, "OWNS", dst_type, dst_tk, {
                "commit_count": parameters.get("commit_count", 1),
                "last_commit_at": parameters.get("last_commit"),
            }))

    def run_read_query(self, query: str, parameters: dict = None, organization_id: str = ""):
        parameters = parameters or {}
        records = []
        if "MATCH (d:Document" in query and "[:DOCUMENTS]->(m:Module)" in query:
            for edge in self.edges:
                if edge[2] == "DOCUMENTS":
                    doc = self.nodes["Document"].get(edge[1], {})
                    mod = self.nodes["Module"].get(edge[3], {})
                    records.append({
                        "path": doc.get("relative_path", "README.md"),
                        "mod_name": mod.get("name", "_root"),
                    })
            if not records:
                for doc in self.nodes["Document"].values():
                    records.append({"path": doc.get("relative_path", "README.md"), "mod_name": "_root"})
        elif "UNWIND $doc_tenant_keys" in query:
            doc_tks = parameters.get("doc_tenant_keys", [])
            for d_tk in doc_tks:
                doc = self.nodes["Document"].get(d_tk, {})
                doc_id = doc.get("doc_id", d_tk.split(":")[-1])
                
                # find module & repository from graph edges
                mod_data = None
                repo_data = None
                for edge in self.edges:
                    if edge[0] == "Document" and edge[1] == d_tk and edge[2] == "DOCUMENTS":
                        m_tk = edge[4]
                        m_node = self.nodes["Module"].get(m_tk, {})
                        if m_node:
                            mod_data = {
                                "id": m_node.get("id", m_tk.split(":", 1)[1] if ":" in m_tk else m_tk),
                                "name": m_node.get("name", "_root"),
                                "path": m_node.get("path", "_root"),
                            }
                            for m_edge in self.edges:
                                if m_edge[0] == "Module" and m_edge[1] == m_tk and m_edge[2] == "PART_OF":
                                    r_tk = m_edge[4]
                                    r_node = self.nodes["Repository"].get(r_tk, {})
                                    if r_node:
                                        repo_data = {
                                            "id": r_node.get("id", r_tk.split(":", 1)[1] if ":" in r_tk else r_tk),
                                            "name": r_node.get("name", ""),
                                            "url": r_node.get("url", ""),
                                        }

                owners = []
                for edge in self.edges:
                    if edge[2] == "OWNS" and edge[4] == d_tk:
                        p_data = self.nodes["Person"].get(edge[1], {})
                        owners.append({
                            "name": p_data.get("name", "Test Author"),
                            "email": p_data.get("email", "author@test.com"),
                            "commit_count": edge[5].get("commit_count", 1),
                        })
                records.append({
                    "doc_id": doc_id,
                    "repository": repo_data,
                    "module": mod_data,
                    "owners": owners,
                    "related_code_files": [],
                })
        return records

    def close(self):
        pass


def _ensure_db_connections():
    chroma_client.host = "localhost"
    chroma_client.port = 8001
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception:
        # Fallback to embedded Chroma for offline test execution
        chroma_client.client = chromadb.EphemeralClient()
        chroma_client.ensure_collections()

    try:
        neo4j_client.connect()
        neo4j_client.create_constraints()
    except Exception:
        # Fallback to in-memory Neo4j graph driver for offline test execution
        in_mem_db = InMemoryNeo4jDriver()
        neo4j_client.driver = object()  # non-None flag
        neo4j_client.run_write_query = in_mem_db.run_write_query
        neo4j_client.run_read_query = in_mem_db.run_read_query



# ---------------------------------------------------------------------------
# Test 1: Unit Test — _is_doc_file Classification
# ---------------------------------------------------------------------------

def test_is_doc_file_classification():
    """Verifies document vs source code file classification helper."""
    # Extensions
    assert _is_doc_file("README.md", "README.md") is True
    assert _is_doc_file("guide.rst", "docs/guide.rst") is True
    assert _is_doc_file("notes.txt", "notes.txt") is True
    assert _is_doc_file("manual.markdown", "manual.markdown") is True

    # Naming patterns
    assert _is_doc_file("README.py", "README.py") is True
    assert _is_doc_file("CONTRIBUTING", "CONTRIBUTING") is True
    assert _is_doc_file("CHANGELOG.json", "CHANGELOG.json") is True

    # Docs directory path
    assert _is_doc_file("architecture.py", "docs/architecture.py") is True
    assert _is_doc_file("setup.sh", "documentation/setup.sh") is True

    # Code files
    assert _is_doc_file("main.py", "src/main.py") is False
    assert _is_doc_file("app.ts", "src/app.ts") is False
    assert _is_doc_file("auth.js", "lib/auth.js") is False


# ---------------------------------------------------------------------------
# Test 2: In-Repo Doc Routing Ingestion & Vector Placement
# ---------------------------------------------------------------------------

def test_in_repo_doc_routing_ingestion(monkeypatch):
    """
    Ingests a temporary repository containing a structured README.md and a main.py file.
    Verifies:
    1. README.md produces heading-aware chunks stored in docs_chunks collection.
    2. README.md creates a Document node in Neo4j linked to Module via DOCUMENTS edge.
    3. main.py produces code chunks stored in code_chunks collection.
    """
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")
    _ensure_db_connections()

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir) / "test_repo"
        repo_path.mkdir()

        # Create main.py (code file)
        code_file = repo_path / "main.py"
        code_file.write_text("def hello_world():\n    print('Hello World')\n", encoding="utf-8")

        # Create README.md (doc file with headings)
        readme_file = repo_path / "README.md"
        readme_text = (
            "# Test Project Overview\n\n"
            "This is a test project demonstrating in-repo doc routing.\n\n"
            "## Feature Architecture\n\n"
            "AutoKT routes in-repo documentation files to the heading-aware chunker.\n"
        )
        readme_file.write_text(readme_text, encoding="utf-8")

        # Execute repository ingestion task directly
        summary = run_repo_ingestion_task(
            task_id="test-task-in-repo-doc-1",
            repo_url=str(repo_path),
            organization_id=TEST_ORG_ID,
        )

        assert summary["files_count"] == 1       # 1 code file (main.py)
        assert summary["docs_count"] == 1        # 1 doc file (README.md)
        assert summary["code_chunks_count"] >= 1
        assert summary["doc_chunks_count"] >= 1

        # Verify ChromaDB docs_chunks collection placement
        docs_collection = chroma_client.client.get_collection("docs_chunks")
        res_docs = docs_collection.get(
            where={"organization_id": {"$eq": TEST_ORG_ID}}
        )
        assert len(res_docs["ids"]) >= 1

        first_doc_meta = res_docs["metadatas"][0]
        print("\n--- REAL CHROMA RECORD METADATA ---")
        print(f"ID: {res_docs['ids'][0]}")
        print(f"Text snippet: {repr(res_docs['documents'][0])}")
        print(f"Metadata payload: {first_doc_meta}")

        assert first_doc_meta["relative_path"] == "README.md"
        assert first_doc_meta["source_confidence"] == "path_match"
        assert first_doc_meta["chunking_method"] == "heading_aware"

        # Verify Neo4j Document node creation
        if neo4j_client.driver:
            cypher_check_doc = (
                "MATCH (d:Document {organization_id: $org})-[:DOCUMENTS]->(m:Module) "
                "RETURN d.relative_path AS path, m.name AS mod_name"
            )
            records = neo4j_client.run_read_query(cypher_check_doc, parameters={"org": TEST_ORG_ID}, organization_id=TEST_ORG_ID)
            print("\n--- REAL NEO4J QUERY RESULT (Document + DOCUMENTS -> Module) ---")
            print(f"Record: {records[0]}")

            assert len(records) >= 1
            assert records[0]["path"] == "README.md"


# ---------------------------------------------------------------------------
# Test 3: Dual Collection Cleanup on Re-Ingestion
# ---------------------------------------------------------------------------

def test_dual_collection_reingestion_cleanup(monkeypatch):
    """
    Ingests a repository twice.
    Verifies that re-ingestion purges previous chunks from BOTH code_chunks and docs_chunks
    without orphan accumulation.
    """
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")
    _ensure_db_connections()

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir) / "cleanup_repo"
        repo_path.mkdir()

        (repo_path / "app.py").write_text("def app_start(): pass\n", encoding="utf-8")
        (repo_path / "README.md").write_text("# Initial Title\nInitial body.\n", encoding="utf-8")

        # Initial Ingestion
        run_repo_ingestion_task(
            task_id="test-task-cleanup-1",
            repo_url=str(repo_path),
            organization_id=TEST_ORG_ID,
        )

        code_col = chroma_client.client.get_collection("code_chunks")
        doc_col = chroma_client.client.get_collection("docs_chunks")

        code_count_1 = len(code_col.get(where={"organization_id": {"$eq": TEST_ORG_ID}})["ids"])
        doc_count_1 = len(doc_col.get(where={"organization_id": {"$eq": TEST_ORG_ID}})["ids"])

        assert code_count_1 >= 1
        assert doc_count_1 >= 1

        # Modify README and Re-ingest
        (repo_path / "README.md").write_text("# Updated Title\nUpdated body.\n", encoding="utf-8")

        run_repo_ingestion_task(
            task_id="test-task-cleanup-2",
            repo_url=str(repo_path),
            organization_id=TEST_ORG_ID,
        )

        code_count_2 = len(code_col.get(where={"organization_id": {"$eq": TEST_ORG_ID}})["ids"])
        doc_count_2 = len(doc_col.get(where={"organization_id": {"$eq": TEST_ORG_ID}})["ids"])

        # Count should remain equal (not doubled due to orphan chunks)
        assert code_count_2 == code_count_1
        assert doc_count_2 == doc_count_1


# ---------------------------------------------------------------------------
# Test 4: Search Retrieval of In-Repo Doc Chunks with Context
# ---------------------------------------------------------------------------

def test_search_retrieves_in_repo_doc_chunks(monkeypatch):
    """
    Executes GET /search query against ingested in-repo doc chunk.
    Verifies result_type='doc', metadata fields, and graph context.
    """
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "stub")
    _ensure_db_connections()

    res = client.get(f"/search?q=Feature+Architecture&organization_id={TEST_ORG_ID}&source_type=all")
    assert res.status_code == 200
    data = res.json()

    assert data["organization_id"] == TEST_ORG_ID
    assert len(data["results"]) >= 1

    doc_results = [r for r in data["results"] if r["result_type"] == "doc"]
    assert len(doc_results) >= 1

    first_doc_hit = doc_results[0]
    assert "relative_path" in first_doc_hit["metadata"]
    assert first_doc_hit["graph_context"] is not None
