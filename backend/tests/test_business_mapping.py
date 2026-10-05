"""
Unit and Integration Test Suite for Business-to-Code Mapping (Milestone 20).
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.core.business_mapping import (
    map_business_chunk_to_code,
    BusinessMappingResult,
    CodeMapping,
    CandidateCodeChunk,
)

client = TestClient(app)

TEST_ORG_ID = "test-mapping-org"
TEST_REPO_ID = "repo:test-chatdoc"


# ---------------------------------------------------------------------------
# Core Engine Unit Tests
# ---------------------------------------------------------------------------

@patch("app.core.business_mapping.chroma_client")
@patch("app.core.business_mapping.rerank")
@patch("app.core.business_mapping.generate_text")
def test_stage1_applies_repository_id_filter(mock_gen_text, mock_rerank, mock_chroma):
    """Verify Stage 1 applies extra_filter with repository_id to Chroma query."""
    mock_chroma.query.return_value = {
        "ids": [["c1"]],
        "documents": [["def parse_doc(): pass"]],
        "metadatas": [[{"file_path": "doc_parser.py", "function_name": "parse_doc", "start_line": 1, "end_line": 5}]],
    }
    mock_chroma.get_documents.return_value = {"ids": [], "documents": [], "metadatas": []}
    mock_rerank.return_value = [1.5]
    mock_gen_text.return_value = '[{"candidate_id": 0, "match": true, "confidence": "high", "reasoning": "Matches parse_doc"}]'

    res = map_business_chunk_to_code(
        organization_id=TEST_ORG_ID,
        repository_id=TEST_REPO_ID,
        business_text="Support parsing PDF and DOCX documents",
        use_llm_judge=True,
    )

    mock_chroma.query.assert_called_once()
    kwargs = mock_chroma.query.call_args[1]
    assert kwargs["extra_filter"] == {"repository_id": {"$eq": TEST_REPO_ID}}
    assert len(res.mappings) == 1
    assert res.mappings[0].function_name == "parse_doc"


@patch("app.core.business_mapping.chroma_client")
@patch("app.core.business_mapping.rerank")
def test_stage2_filters_below_threshold(mock_rerank, mock_chroma):
    """Verify Stage 2 filters out candidates below BUSINESS_MAPPING_RERANK_THRESHOLD."""
    mock_chroma.query.return_value = {
        "ids": [["c1", "c2", "c3", "c4"]],
        "documents": [["fn1", "fn2", "fn3", "fn4"]],
        "metadatas": [[
            {"file_path": "f1.py", "function_name": "fn1"},
            {"file_path": "f2.py", "function_name": "fn2"},
            {"file_path": "f3.py", "function_name": "fn3"},
            {"file_path": "f4.py", "function_name": "fn4"},
        ]],
    }
    mock_chroma.get_documents.return_value = {"ids": [], "documents": [], "metadatas": []}
    
    # Threshold is 0.5; candidates c2 (0.2) and c3 (-1.0) should be filtered out
    mock_rerank.return_value = [3.0, 0.2, -1.0, 4.5]

    res = map_business_chunk_to_code(
        organization_id=TEST_ORG_ID,
        repository_id=TEST_REPO_ID,
        business_text="Parse CSV and PDF files",
        use_llm_judge=False,  # Skip LLM
    )

    assert res.telemetry.stage1_candidates == 4
    assert res.telemetry.stage2_survivors == 2
    assert len(res.mappings) == 2
    assert res.mappings[0].function_name == "fn4"
    assert res.mappings[1].function_name == "fn1"


@patch("app.core.business_mapping.chroma_client")
@patch("app.core.business_mapping.rerank")
def test_no_candidates_above_threshold_returns_unmapped(mock_rerank, mock_chroma):
    """Verify that when 0 candidates pass threshold, an unmapped response is returned."""
    mock_chroma.query.return_value = {
        "ids": [["c1", "c2"]],
        "documents": [["text1", "text2"]],
        "metadatas": [[{"file_path": "a.py"}, {"file_path": "b.py"}]],
    }
    mock_chroma.get_documents.return_value = {"ids": [], "documents": [], "metadatas": []}
    mock_rerank.return_value = [-0.5, 0.1]  # Both below 0.5

    res = map_business_chunk_to_code(
        organization_id=TEST_ORG_ID,
        repository_id=TEST_REPO_ID,
        business_text="Strategic high level vision document",
        use_llm_judge=True,
    )

    assert len(res.mappings) == 0
    assert res.unmapped_reason is not None
    assert "No code chunk scored above" in res.unmapped_reason


@patch("app.core.business_mapping.chroma_client")
@patch("app.core.business_mapping.rerank")
@patch("app.core.business_mapping.generate_text")
def test_stage3_parses_llm_json_correctly(mock_gen_text, mock_rerank, mock_chroma):
    """Verify Stage 3 parses JSON response and constructs CodeMapping objects."""
    mock_chroma.query.return_value = {
        "ids": [["c1"]],
        "documents": [["def load_data(): pass"]],
        "metadatas": [[{"file_path": "src/loader.py", "function_name": "load_data", "start_line": 10, "end_line": 20}]],
    }
    mock_chroma.get_documents.return_value = {"ids": [], "documents": [], "metadatas": []}
    mock_rerank.return_value = [2.5]
    
    mock_gen_text.return_value = """```json
    [
      {
        "candidate_id": 0,
        "match": true,
        "confidence": "high",
        "reasoning": "This function directly implements data loading from loader.py."
      }
    ]
    ```"""

    res = map_business_chunk_to_code(
        organization_id=TEST_ORG_ID,
        repository_id=TEST_REPO_ID,
        business_text="Load document data from files",
        use_llm_judge=True,
    )

    assert len(res.mappings) == 1
    m = res.mappings[0]
    assert m.file_path == "src/loader.py"
    assert m.function_name == "load_data"
    assert m.confidence == "high"
    assert m.evidence_type == "llm_reasoning"
    assert m.callers_scope == "same_file_only"
    assert "directly implements data loading" in m.reasoning


@patch("app.core.business_mapping.chroma_client")
@patch("app.core.business_mapping.rerank")
@patch("app.core.business_mapping.generate_text")
def test_stage3_fallback_on_parse_error(mock_gen_text, mock_rerank, mock_chroma):
    """Verify that unparseable LLM output falls back gracefully with grounding_warning."""
    mock_chroma.query.return_value = {
        "ids": [["c1"]],
        "documents": [["def process(): pass"]],
        "metadatas": [[{"file_path": "processor.py", "function_name": "process"}]],
    }
    mock_chroma.get_documents.return_value = {"ids": [], "documents": [], "metadatas": []}
    mock_rerank.return_value = [1.8]
    mock_gen_text.return_value = "Sorry, I am an AI and I cannot produce JSON."

    res = map_business_chunk_to_code(
        organization_id=TEST_ORG_ID,
        repository_id=TEST_REPO_ID,
        business_text="Process documents",
        use_llm_judge=True,
    )

    assert res.generation_quality == "grounding_warning"
    assert len(res.mappings) == 1
    assert res.mappings[0].evidence_type == "reranker_only"
    assert res.mappings[0].confidence == "low"


@patch("app.core.business_mapping.chroma_client")
@patch("app.core.business_mapping.rerank")
@patch("app.core.business_mapping.generate_text")
def test_grounding_check_flags_hallucinated_file(mock_gen_text, mock_rerank, mock_chroma):
    """Verify grounding check flags LLM reasoning that mentions unknown files."""
    mock_chroma.query.return_value = {
        "ids": [["c1"]],
        "documents": [["def parse(): pass"]],
        "metadatas": [[{"file_path": "parser.py", "function_name": "parse"}]],
    }
    mock_chroma.get_documents.return_value = {"ids": [], "documents": [], "metadatas": []}
    mock_rerank.return_value = [2.0]
    
    # LLM mentions hallucinated file nonexistent_file.py
    mock_gen_text.return_value = json.dumps([
        {
            "candidate_id": 0,
            "match": True,
            "confidence": "high",
            "reasoning": "Matches parse() in parser.py and calls nonexistent_file.py for parsing."
        }
    ])

    res = map_business_chunk_to_code(
        organization_id=TEST_ORG_ID,
        repository_id=TEST_REPO_ID,
        business_text="Parse documents",
        use_llm_judge=True,
    )

    assert res.generation_quality == "grounding_warning"
    assert len(res.mappings) == 1
    assert res.mappings[0].confidence == "medium"  # Downgraded from high due to ungrounded mention


@patch("app.core.business_mapping.chroma_client")
@patch("app.core.business_mapping.rerank")
def test_full_stub_pipeline_produces_valid_result(mock_rerank, mock_chroma):
    """Verify end-to-end mapping using stub provider."""
    mock_chroma.query.return_value = {
        "ids": [["c1"]],
        "documents": [["def run_pipeline(): pass"]],
        "metadatas": [[{"file_path": "pipeline.py", "function_name": "run_pipeline"}]],
    }
    mock_chroma.get_documents.return_value = {"ids": [], "documents": [], "metadatas": []}
    mock_rerank.return_value = [2.0]

    with patch.object(settings, "LLM_PROVIDER", "stub"):
        res = map_business_chunk_to_code(
            organization_id=TEST_ORG_ID,
            repository_id=TEST_REPO_ID,
            business_text="Execute automated pipeline",
            use_llm_judge=True,
        )

    assert isinstance(res, BusinessMappingResult)
    assert res.generation_quality in ("ok", "grounding_warning")
    assert res.telemetry.use_llm_judge is True


# ---------------------------------------------------------------------------
# API Endpoint Integration Tests
# ---------------------------------------------------------------------------

def test_post_missing_both_inputs_returns_422():
    """Verify HTTP 422 validation error when neither chunk_id nor business_text is provided."""
    resp = client.post(
        "/business-mapping",
        json={"organization_id": TEST_ORG_ID, "repository_id": TEST_REPO_ID},
    )
    assert resp.status_code == 422
    assert "At least one of 'chunk_id' or 'business_text' must be provided" in resp.json()["detail"]


def test_post_both_inputs_returns_422():
    """Verify HTTP 422 validation error when both chunk_id and business_text are provided."""
    resp = client.post(
        "/business-mapping",
        json={
            "organization_id": TEST_ORG_ID,
            "repository_id": TEST_REPO_ID,
            "chunk_id": "bdoc:chunk:1",
            "business_text": "Sample text",
        },
    )
    assert resp.status_code == 422
    assert "Provide either 'chunk_id' OR 'business_text'" in resp.json()["detail"]


@patch("app.api.business_mapping.map_business_chunk_to_code")
def test_post_valid_request_returns_200(mock_engine):
    """Verify POST /business-mapping returns HTTP 200 with BusinessMappingResult schema."""
    mock_engine.return_value = BusinessMappingResult(
        business_chunk={"chunk_id": "", "heading_path": "", "text_excerpt": "Upload files"},
        mappings=[
            CodeMapping(
                target_type="function",
                file_path="upload.py",
                function_name="upload_file",
                module_name="api",
                code_excerpt="def upload_file(): pass",
                confidence="high",
                rerank_score=3.5,
                evidence_type="llm_reasoning",
                reasoning="Direct match.",
                callers=[],
                callers_scope="same_file_only",
                low_confidence=False,
            )
        ],
        unmapped_reason=None,
        generation_quality="ok",
        telemetry={
            "retrieval_ms": 10.0,
            "reranking_ms": 5.0,
            "llm_judge_ms": 100.0,
            "graph_enrichment_ms": 2.0,
            "total_ms": 117.0,
            "stage1_candidates": 10,
            "stage2_survivors": 2,
            "stage3_confirmed": 1,
            "llm_provider": "stub",
            "use_llm_judge": True,
        },
    )

    resp = client.post(
        "/business-mapping",
        json={
            "organization_id": TEST_ORG_ID,
            "repository_id": TEST_REPO_ID,
            "business_text": "Upload files to system",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["business_chunk"]["text_excerpt"] == "Upload files"
    assert len(data["mappings"]) == 1
    assert data["mappings"][0]["callers_scope"] == "same_file_only"


@patch("app.api.business_mapping.run_business_mapping_document_task")
def test_post_document_returns_202_with_task_id(mock_bg_task):
    """Verify POST /business-mapping/document enqueues task and returns HTTP 202."""
    resp = client.post(
        "/business-mapping/document",
        json={
            "organization_id": TEST_ORG_ID,
            "repository_id": TEST_REPO_ID,
            "document_id": "bdoc:repo:chatdoc/ChatDoc_PRD.pdf",
        },
    )

    assert resp.status_code == 202
    data = resp.json()
    assert "task_id" in data
    assert data["status"] == "pending"


@patch("app.api.business_mapping.chroma_client")
@patch("app.api.business_mapping.map_business_chunk_to_code")
def test_chunk_id_resolution_fetches_text(mock_map, mock_chroma):
    """Verify POST /business-mapping with chunk_id resolves text from docs_chunks."""
    mock_chroma.get_documents.return_value = {
        "ids": ["bdoc:chunk:1"],
        "documents": ["Text resolved from docs_chunks for chunk 1"],
        "metadatas": [{"heading_path": "Section 1"}],
    }
    mock_map.return_value = BusinessMappingResult(
        business_chunk={"chunk_id": "bdoc:chunk:1", "heading_path": "Section 1", "text_excerpt": "Text resolved"},
        mappings=[],
        unmapped_reason="No match",
        generation_quality="ok",
        telemetry={
            "retrieval_ms": 1.0,
            "reranking_ms": 1.0,
            "llm_judge_ms": 0.0,
            "graph_enrichment_ms": 0.0,
            "total_ms": 2.0,
            "stage1_candidates": 0,
            "stage2_survivors": 0,
            "stage3_confirmed": 0,
            "llm_provider": "stub",
            "use_llm_judge": False,
        },
    )

    resp = client.post(
        "/business-mapping",
        json={
            "organization_id": TEST_ORG_ID,
            "repository_id": TEST_REPO_ID,
            "chunk_id": "bdoc:chunk:1",
        },
    )

    assert resp.status_code == 200
    mock_chroma.get_documents.assert_called_once()
    mock_map.assert_called_once()
    assert mock_map.call_args[1]["business_text"] == "Text resolved from docs_chunks for chunk 1"


def test_get_task_status_returns_task():
    """Verify GET /business-mapping/tasks/{task_id} returns 404 on non-existent task UUID."""
    resp = client.get("/business-mapping/tasks/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert "Task with ID" in resp.json()["detail"]
