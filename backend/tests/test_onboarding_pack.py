"""
Unit and Integration Test Suite for Developer Onboarding Pack Generator.
"""

import time
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.core.llm_client import generate_text, LLMClientError
from app.core.onboarding_renderer import render_markdown
from app.db.neo4j_client import neo4j_client
from app.db.chroma_client import chroma_client

client = TestClient(app)

TEST_ORG_ID = "test-onboarding-org"
TEST_REPO_URL = "https://github.com/MuhammadUsman-Khan/youtube-chatbot.git"


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

def test_render_markdown_produces_all_sections():
    """Verify render_markdown produces all 9 section headers."""
    sample_pack = {
        "module_id": "mod-1",
        "module_name": "app",
        "module_path": "app",
        "organization_id": TEST_ORG_ID,
        "generated_at": "2026-08-17T12:00:00Z",
        "generation_quality": "ok",
        "sections": {
            "module_purpose": {"source": "llm_generated", "content": "This module handles chat logic."},
            "key_files": {
                "source": "graph",
                "files": [{"filename": "main.py", "relative_path": "app/main.py", "language": "py", "function_count": 2, "class_count": 0}],
            },
            "entry_points": {
                "source": "graph",
                "functions": [{"function_name": "run_app", "file_path": "app/main.py", "is_documented": True, "chunk_type": "function"}],
            },
            "who_to_talk_to": {
                "source": "graph",
                "caveat": "File-level ownership only",
                "owners": [{"name": "Usman", "email": "usman@example.com", "total_commits": 10, "last_active": "2026-08-01"}],
            },
            "existing_docs": {
                "source": "graph",
                "documents": [{"filename": "README.md", "relative_path": "README.md", "version": 1, "lead_excerpt": "Project README"}],
            },
            "dependencies": {
                "source": "graph",
                "status": "not_yet_available",
                "reason": "Static import analysis not built yet.",
                "related_modules": [],
            },
            "project_requirements": {
                "source": "computed",
                "scope": "repository",
                "status": "found",
                "manifest_files": ["requirements.txt"],
                "declared_dependencies": ["flask>=2.0", "requests"],
                "setup_instructions": "See README.md for setup instructions.",
            },
            "doc_coverage": {
                "source": "computed",
                "total_functions": 2,
                "documented_functions": 1,
                "undocumented_functions": 1,
                "coverage_pct": 50.0,
                "has_readme": True,
                "linked_doc_count": 1,
            },
            "suggested_first_tasks": {
                "source": "computed",
                "tasks": [{"priority": "medium", "description": "Add docstring to main", "rationale": "50% coverage"}],
            },
            "onboarding_narrative": {"source": "llm_generated", "content": "Start by reading main.py."},
        },
        "known_limitations": ["owner_granularity: file-level blame"],
        "telemetry": {"total_ms": 100.0, "graph_query_ms": 10.0, "vector_query_ms": 20.0, "llm_generation_ms": 70.0, "llm_provider": "stub"},
    }

    md = render_markdown(sample_pack)
    assert "# Developer Onboarding Pack: `app`" in md
    assert "## 1. Module Purpose & Overview" in md
    assert "## 2. Key Files" in md
    assert "## 3. Entry Points & Top-Level Functions" in md
    assert "## 4. Who To Talk To (Code Owners)" in md
    assert "## 5. Existing Documentation" in md
    assert "## 6. Module Dependencies" in md
    assert "## 7. Project Requirements & Setup" in md
    assert "## 8. Documentation Coverage & Health" in md
    assert "## 9. Suggested First Onboarding Tasks" in md
    assert "## 10. Getting Started Narrative" in md
    assert "`main.py`" in md
    assert "`usman@example.com`" in md
    assert "`requirements.txt`" in md


def test_render_markdown_handles_empty_owners():
    """Verify render_markdown renders cleanly when code owners is empty."""
    empty_pack = {
        "module_name": "empty_mod",
        "sections": {
            "who_to_talk_to": {"source": "graph", "owners": []},
        },
    }
    md = render_markdown(empty_pack)
    assert "*No git commit authorship records found for files in this module.*" in md


def test_stub_llm_returns_template_string():
    """Verify LLM client in stub mode returns non-empty template text without external API calls."""
    res = generate_text("Tell me what this module does.", system_instruction="System prompt test.", provider="stub")
    assert "[STUB LLM GENERATION" in res
    assert "Summary synthesized from prompt context" in res


def test_llm_client_unknown_provider_raises():
    """Verify LLM client raises LLMClientError when an unknown provider is specified."""
    with pytest.raises(LLMClientError):
        generate_text("test prompt", provider="invalid_provider")


def test_azure_foundry_llm_missing_credentials_raises():
    """Verify LLM client raises LLMClientError when azure_foundry is selected without API key/URL."""
    with pytest.raises(LLMClientError) as exc_info:
        generate_text("test prompt", provider="azure_foundry", api_key="")
    assert "LLM_API_KEY" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_post_onboarding_pack_validation():
    """Verify API pre-flight input validation for missing or empty parameters."""
    res1 = client.post("/onboarding-pack", json={"module_id": "  ", "organization_id": TEST_ORG_ID})
    assert res1.status_code == 400
    assert "module_id" in res1.json()["detail"]

    res2 = client.post("/onboarding-pack", json={"module_id": "app", "organization_id": ""})
    assert res2.status_code == 400
    assert "organization_id" in res2.json()["detail"]


@pytest.mark.integration
def test_onboarding_pack_nonexistent_module_task_fails():
    """Verify generating a pack for a nonexistent module task transitions to status 'failed'."""
    post_res = client.post(
        "/onboarding-pack",
        json={"module_id": "nonexistent-module-xyz-123", "organization_id": TEST_ORG_ID},
    )
    assert post_res.status_code == 202
    task_id = post_res.json()["task_id"]

    # Poll task until completion or failure
    final_status = "pending"
    for _ in range(20):
        time.sleep(0.3)
        poll_res = client.get(f"/onboarding-pack/tasks/{task_id}")
        assert poll_res.status_code == 200
        final_status = poll_res.json()["status"]
        if final_status in ("completed", "failed"):
            break

    assert final_status == "failed"
    result = poll_res.json()["result"]
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.integration
def test_onboarding_pack_end_to_end():
    """
    End-to-end integration test:
    1. Ingest a real repository (youtube-chatbot).
    2. Request an onboarding pack for module 'app'.
    3. Poll until completed and verify real files, owners, docs, coverage, and markdown output.
    """
    with patch.object(settings, "EMBEDDING_PROVIDER", ""):
        ingest_res = client.post(
            "/repos",
            json={
                "repo_url": TEST_REPO_URL,
                "organization_id": TEST_ORG_ID,
                "branch": "main",
            },
        )
        assert ingest_res.status_code == 202
        ingest_task_id = ingest_res.json()["task_id"]

        for _ in range(30):
            time.sleep(0.5)
            t_res = client.get(f"/repos/tasks/{ingest_task_id}")
            if t_res.json()["status"] in ("completed", "completed_without_embeddings"):
                break

    # Look up the ingested module ID from Neo4j
    cypher_mod = "MATCH (m:Module {organization_id: $organization_id}) RETURN m.id AS mod_id LIMIT 1"
    recs = neo4j_client.run_read_query(cypher_mod, organization_id=TEST_ORG_ID)
    assert len(recs) > 0, "No module found in Neo4j after repo ingestion."
    module_id = recs[0]["mod_id"]

    # Trigger onboarding pack generation
    pack_res = client.post(
        "/onboarding-pack",
        json={
            "module_id": module_id,
            "organization_id": TEST_ORG_ID,
            "include_markdown": True,
        },
    )
    assert pack_res.status_code == 202
    pack_task_id = pack_res.json()["task_id"]

    # Poll task until completion
    task_data = {}
    for _ in range(20):
        time.sleep(0.3)
        poll_res = client.get(f"/onboarding-pack/tasks/{pack_task_id}")
        assert poll_res.status_code == 200
        task_data = poll_res.json()
        if task_data["status"] in ("completed", "failed"):
            break

    assert task_data["status"] == "completed"
    res_pack = task_data["result"]

    assert res_pack["module_id"] == module_id
    assert "sections" in res_pack
    assert "markdown" in res_pack
    assert "# Developer Onboarding Pack:" in res_pack["markdown"]

    # Verify key files section contains real ingested files
    key_files = res_pack["sections"]["key_files"]["files"]
    assert len(key_files) > 0

    # Verify code owners contains real committer
    owners = res_pack["sections"]["who_to_talk_to"]["owners"]
    assert len(owners) > 0
    assert any("usmankhan" in o["email"].lower() for o in owners)

    # Verify documentation coverage block
    doc_cov = res_pack["sections"]["doc_coverage"]
    assert "coverage_pct" in doc_cov
    assert doc_cov["total_functions"] >= 0

    # Verify project requirements section
    proj_req = res_pack["sections"]["project_requirements"]
    assert proj_req["source"] == "computed"
    assert proj_req["scope"] == "repository"
    assert proj_req["status"] in ("found", "not_found")
    if proj_req["status"] == "found":
        assert len(proj_req["manifest_files"]) > 0

    # Clean up test tenant data in Neo4j and Chroma
    try:
        neo4j_client.run_write_query(
            "MATCH (n {organization_id: $organization_id}) DETACH DELETE n",
            organization_id=TEST_ORG_ID,
        )
        chroma_client.delete_by_metadata("code_chunks", organization_id=TEST_ORG_ID)
        chroma_client.delete_by_metadata("docs_chunks", organization_id=TEST_ORG_ID)
    except Exception:
        pass
