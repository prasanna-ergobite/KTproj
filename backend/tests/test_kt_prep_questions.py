"""
Unit and Integration Test Suite for KT Prep Questions Generator.
"""

import time
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.core.module_context import ModuleContext
from app.graphs.kt_prep_questions import (
    _build_gap_signals,
    _rule_based_questions,
    _generate_questions,
    run_kt_prep_questions_task,
)

client = TestClient(app)

TEST_ORG_ID = "test-kt-prep-org"
TEST_REPO_URL = "https://github.com/MuhammadUsman-Khan/youtube-chatbot.git"


def _create_mock_context(
    coverage_pct: float = 75.0,
    has_readme: bool = True,
    owners_count: int = 2,
    docs_count: int = 1,
    has_manifest: bool = True,
    undocumented_fn_count: int = 1,
) -> ModuleContext:
    """Utility helper constructing synthetic ModuleContext instances for unit testing."""
    raw_files = [{"id": "f-1", "path": "app/main.py", "language": "py"}]
    raw_functions = [{"name": "run_app", "file_id": "f-1", "start_line": 10}]
    raw_classes = []

    key_files_list = [{
        "file_id": "f-1",
        "filename": "main.py",
        "relative_path": "app/main.py",
        "function_count": 1,
        "class_count": 0,
        "language": "py",
    }]

    entry_points_list = [{
        "function_name": "run_app",
        "file_path": "app/main.py",
        "is_documented": undocumented_fn_count == 0,
        "chunk_type": "function",
    }]

    chroma_func_meta = {
        ("f-1", "run_app", 10): {
            "is_documented": undocumented_fn_count == 0,
            "chunk_type": "function",
            "parent_class": "",
            "token_count": 150,
        }
    }

    owners_list = []
    if owners_count == 1:
        owners_list = [{"name": "Alice", "email": "alice@example.com", "total_commits": 10, "last_active": "2026-08-01"}]
    elif owners_count > 1:
        owners_list = [
            {"name": "Alice", "email": "alice@example.com", "total_commits": 10, "last_active": "2026-08-01"},
            {"name": "Bob", "email": "bob@example.com", "total_commits": 5, "last_active": "2026-08-02"},
        ]

    existing_docs_list = []
    if docs_count > 0:
        existing_docs_list.append({
            "doc_id": "d-1",
            "filename": "README.md" if has_readme else "doc.md",
            "relative_path": "README.md" if has_readme else "docs/doc.md",
            "version": 1,
            "lead_excerpt": "Project README",
        })

    project_requirements_data = {
        "status": "found" if has_manifest else "not_found",
        "manifest_files": ["requirements.txt"] if has_manifest else [],
    }

    return ModuleContext(
        module_id="mod-1",
        module_name="app",
        module_path="app",
        organization_id=TEST_ORG_ID,
        raw_files=raw_files,
        raw_functions=raw_functions,
        raw_classes=raw_classes,
        key_files_list=key_files_list,
        entry_points_list=entry_points_list,
        chroma_func_meta=chroma_func_meta,
        owners_list=owners_list,
        existing_docs_list=existing_docs_list,
        has_readme=has_readme,
        project_requirements_data=project_requirements_data,
        total_func_count=1,
        documented_count=1 if undocumented_fn_count == 0 else 0,
        undocumented_count=undocumented_fn_count,
        coverage_pct=coverage_pct,
        doc_coverage_data={"coverage_pct": coverage_pct},
        semantic_snippets=["Main app handles user requests."],
        graph_time_ms=10.0,
        vector_time_ms=5.0,
    )


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

def test_build_gap_signals_undocumented_fn():
    """Verify gap signal builder identifies undocumented entry functions."""
    ctx = _create_mock_context(undocumented_fn_count=1)
    signals = _build_gap_signals(ctx)
    signal_types = [s["type"] for s in signals]
    assert "undocumented_function" in signal_types
    undoc_sig = next(s for s in signals if s["type"] == "undocumented_function")
    assert undoc_sig["entity"] == "run_app"


def test_build_gap_signals_complex_undocumented():
    """Verify gap signal builder identifies large undocumented functions (>200 tokens)."""
    ctx = _create_mock_context(undocumented_fn_count=1)
    ctx.chroma_func_meta[("f-1", "run_app", 10)]["token_count"] = 500
    signals = _build_gap_signals(ctx)
    signal_types = [s["type"] for s in signals]
    assert "complex_undocumented" in signal_types
    comp_sig = next(s for s in signals if s["type"] == "complex_undocumented")
    assert comp_sig["entity"] == "run_app"
    assert comp_sig["token_count"] == 500


def test_build_gap_signals_sole_owner():
    """Verify sole owner / single committer risk is flagged."""
    ctx = _create_mock_context(owners_count=1)
    signals = _build_gap_signals(ctx)
    signal_types = [s["type"] for s in signals]
    assert "sole_owner_file" in signal_types
    sole_sig = next(s for s in signals if s["type"] == "sole_owner_file")
    assert sole_sig["entity"] == "Alice"


def test_build_gap_signals_no_owners():
    """Verify empty code owners list is flagged as no_owners."""
    ctx = _create_mock_context(owners_count=0)
    signals = _build_gap_signals(ctx)
    signal_types = [s["type"] for s in signals]
    assert "no_owners" in signal_types


def test_build_gap_signals_no_docs():
    """Verify missing documentation files and missing README are flagged."""
    ctx = _create_mock_context(docs_count=0, has_readme=False)
    signals = _build_gap_signals(ctx)
    signal_types = [s["type"] for s in signals]
    assert "no_linked_docs" in signal_types
    assert "no_readme" in signal_types


def test_build_gap_signals_low_coverage():
    """Verify low documentation coverage (<50%) is flagged."""
    ctx = _create_mock_context(coverage_pct=25.0)
    signals = _build_gap_signals(ctx)
    signal_types = [s["type"] for s in signals]
    assert "low_doc_coverage" in signal_types


def test_build_gap_signals_clean_module_minimal():
    """Verify a clean, fully documented module emits only cross_module_deps_unknown."""
    ctx = _create_mock_context(
        coverage_pct=100.0,
        has_readme=True,
        owners_count=2,
        docs_count=1,
        has_manifest=True,
        undocumented_fn_count=0,
    )
    signals = _build_gap_signals(ctx)
    assert len(signals) == 1
    assert signals[0]["type"] == "cross_module_deps_unknown"


def test_rule_based_questions_cap_at_8():
    """Verify rule-based question generator caps output at 8 questions."""
    many_signals = [
        {"type": "complex_undocumented", "entity": f"fn_{i}", "severity": 1, "description": f"desc_{i}"}
        for i in range(15)
    ]
    res = _rule_based_questions(many_signals)
    lines = [line for line in res.splitlines() if line.strip()]
    assert len(lines) <= 8


def test_rule_based_questions_uses_entity_names():
    """Verify entity names (function/file names) appear in rule-based output."""
    signals = [
        {"type": "complex_undocumented", "entity": "parse_config", "severity": 1, "description": "desc"},
        {"type": "sole_owner_file", "entity": "Alice", "severity": 2, "description": "desc"},
    ]
    res = _rule_based_questions(signals)
    assert "parse_config" in res
    assert "Alice" in res


def test_no_gaps_short_circuit_skips_llm():
    """Verify _generate_questions short-circuits with no_gaps_detected when no real gaps exist."""
    ctx = _create_mock_context(undocumented_fn_count=0)
    signals = [{"type": "cross_module_deps_unknown", "severity": 9, "description": "deps unknown"}]
    text, method = _generate_questions(ctx, signals)
    assert method == "no_gaps_detected"
    assert "well-documented" in text


def test_stub_provider_uses_rule_based_not_llm():
    """Verify stub provider mode uses rule_based_fallback."""
    ctx = _create_mock_context(undocumented_fn_count=1)
    signals = _build_gap_signals(ctx)
    with patch.object(settings, "LLM_PROVIDER", "stub"):
        text, method = _generate_questions(ctx, signals)
        assert method == "rule_based_fallback"
        assert "run_app" in text


def test_grounding_check_flags_unknown_email():
    """Verify grounding check sets generation_quality to grounding_warning if LLM mentions an unknown email."""
    ctx = _create_mock_context(undocumented_fn_count=1)
    with patch("app.graphs.kt_prep_questions.assemble_module_context", return_value=ctx), \
         patch("app.graphs.kt_prep_questions.generate_text", return_value="Contact mysterious_stranger@unknown.com for help with run_app."), \
         patch.object(settings, "LLM_PROVIDER", "gemini"):
        res = run_kt_prep_questions_task("test-task-1", "mod-1", TEST_ORG_ID)
        assert res["generation_quality"] == "grounding_warning"


def test_grounding_check_passes_known_email():
    """Verify grounding check keeps generation_quality as ok when LLM mentions a known email."""
    ctx = _create_mock_context(undocumented_fn_count=1)
    with patch("app.graphs.kt_prep_questions.assemble_module_context", return_value=ctx), \
         patch("app.graphs.kt_prep_questions.generate_text", return_value="Contact alice@example.com regarding run_app."), \
         patch.object(settings, "LLM_PROVIDER", "gemini"):
        res = run_kt_prep_questions_task("test-task-2", "mod-1", TEST_ORG_ID)
        assert res["generation_quality"] == "ok"


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_post_kt_prep_validation():
    """Verify API pre-flight input validation for missing or empty parameters."""
    res1 = client.post("/kt-prep-questions", json={"module_id": "  ", "organization_id": TEST_ORG_ID})
    assert res1.status_code == 400
    assert "module_id" in res1.json()["detail"]

    res2 = client.post("/kt-prep-questions", json={"module_id": "app", "organization_id": ""})
    assert res2.status_code == 400
    assert "organization_id" in res2.json()["detail"]


@pytest.mark.integration
def test_kt_prep_nonexistent_module_fails():
    """Verify generating questions for a nonexistent module transitions task to status 'failed'."""
    post_res = client.post(
        "/kt-prep-questions",
        json={"module_id": "nonexistent-mod-999", "organization_id": TEST_ORG_ID},
    )
    assert post_res.status_code == 202
    task_id = post_res.json()["task_id"]

    final_status = "pending"
    for _ in range(20):
        time.sleep(0.3)
        poll_res = client.get(f"/kt-prep-questions/tasks/{task_id}")
        assert poll_res.status_code == 200
        final_status = poll_res.json()["status"]
        if final_status in ("completed", "failed"):
            break

    assert final_status == "failed"
    result = poll_res.json()["result"]
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.integration
def test_kt_prep_end_to_end():
    """
    End-to-end integration test:
    1. Ingest a real repository (youtube-chatbot).
    2. Request KT prep questions for module 'app'.
    3. Poll until completed and verify signal_count, signals_used, questions, and generation_method output.
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
    from app.db.neo4j_client import neo4j_client
    from app.db.chroma_client import chroma_client

    cypher_mod = "MATCH (m:Module {organization_id: $organization_id}) RETURN m.id AS mod_id LIMIT 1"
    recs = neo4j_client.run_read_query(cypher_mod, organization_id=TEST_ORG_ID)
    assert len(recs) > 0, "No module found in Neo4j after repo ingestion."
    module_id = recs[0]["mod_id"]

    # Trigger KT prep questions generation
    prep_res = client.post(
        "/kt-prep-questions",
        json={
            "module_id": module_id,
            "organization_id": TEST_ORG_ID,
        },
    )
    assert prep_res.status_code == 202
    prep_task_id = prep_res.json()["task_id"]

    # Poll task until completion
    task_data = {}
    for _ in range(20):
        time.sleep(0.3)
        poll_res = client.get(f"/kt-prep-questions/tasks/{prep_task_id}")
        assert poll_res.status_code == 200
        task_data = poll_res.json()
        if task_data["status"] in ("completed", "failed"):
            break

    assert task_data["status"] == "completed"
    res_prep = task_data["result"]

    assert res_prep["module_id"] == module_id
    assert "signal_count" in res_prep
    assert "signals_used" in res_prep
    assert "questions" in res_prep
    assert len(res_prep["questions"]) > 0
    assert res_prep["generation_method"] in ("llm", "rule_based_fallback", "no_gaps_detected")

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

