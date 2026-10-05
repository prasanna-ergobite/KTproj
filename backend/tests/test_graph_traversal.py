"""
Unit and Integration Tests for Query Intent Router and Deterministic Graph Traversal Engine.
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.query_router import classify_query_intent, QueryIntent


def test_classify_query_intent_same_module_patterns():
    queries = [
        ("What other functions are in the same module as summarize()?", "summarize"),
        ("siblings of function process_documents", "process_documents"),
        ("what functions exist in the same module as handle_question", "handle_question"),
        ("functions in the same module as query", "query"),
    ]
    for q, expected_entity in queries:
        res = classify_query_intent(q)
        assert res.intent == QueryIntent.SAME_MODULE_FUNCTIONS, f"Failed for query: {q}"
        assert res.entity_name == expected_entity, f"Failed entity for query: {q}"


def test_classify_query_intent_same_file_patterns():
    queries = [
        ("other functions in the same file as summarize", "summarize"),
        ("what functions exist in the same file as query()", "query"),
    ]
    for q, expected_entity in queries:
        res = classify_query_intent(q)
        assert res.intent == QueryIntent.SAME_FILE_FUNCTIONS, f"Failed for query: {q}"
        assert res.entity_name == expected_entity, f"Failed entity for query: {q}"


def test_classify_query_intent_file_symbols():
    queries = [
        ("what functions are in document_utils.py", "document_utils.py"),
        ("show me app.py", "app.py"),
        ("what is in conversation.py", "conversation.py"),
        ("symbols in src/document_utils.py", "src/document_utils.py"),
    ]
    for q, expected_entity in queries:
        res = classify_query_intent(q)
        assert res.intent == QueryIntent.FILE_SYMBOLS, f"Failed for query: {q}"
        assert res.entity_name == expected_entity, f"Failed entity for query: {q}"


def test_classify_query_intent_containing_file():
    queries = [
        ("which file contains function summarize", "summarize"),
        ("where is summarize defined", "summarize"),
        ("what file defines query", "query"),
        ("file location of handle_question", "handle_question"),
    ]
    for q, expected_entity in queries:
        res = classify_query_intent(q)
        assert res.intent == QueryIntent.CONTAINING_FILE, f"Failed for query: {q}"
        assert res.entity_name == expected_entity, f"Failed entity for query: {q}"


def test_classify_query_intent_containing_module():
    queries = [
        ("which module contains document_utils.py", "document_utils.py"),
        ("what module is app.py in", "app.py"),
    ]
    for q, expected_entity in queries:
        res = classify_query_intent(q)
        assert res.intent == QueryIntent.CONTAINING_MODULE, f"Failed for query: {q}"
        assert res.entity_name == expected_entity, f"Failed entity for query: {q}"


def test_classify_query_intent_semantic_fallback():
    queries = [
        "how does document summarization work",
        "what embedding model are we using?",
        "explain the error handling flow",
    ]
    for q in queries:
        res = classify_query_intent(q)
        assert res.intent == QueryIntent.SEMANTIC, f"Failed for query: {q}"
        assert res.entity_name is None


def test_structural_search_endpoint_bypasses_vector_embedding():
    client = TestClient(app)

    mock_traversal_record = [
        {
            "repository": {"id": "repo:chatdoc", "name": "chatdoc", "url": "https://github.com/test/repo"},
            "module": {"id": "mod:src", "name": "src", "path": "src"},
            "file_path": "src/document_utils.py",
            "file_id": "chatdoc:file:src/document_utils.py",
            "function_name": "query",
            "function_id": "repo:chatdoc/src/document_utils.py::query",
            "start_line": 32,
            "end_line": 40,
            "owners": [{"name": "Erfan", "email": "erfan@test.com", "commit_count": 5}],
        }
    ]

    with patch("app.api.search.execute_structural_traversal", return_value=mock_traversal_record):
        response = client.get(
            "/search",
            params={
                "q": "What other functions are in the same module as summarize()?",
                "organization_id": "test_org_structural",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["organization_id"] == "test_org_structural"
    assert data["total_results"] == 1
    assert data["telemetry"]["embedding_ms"] == 0.0
    assert data["telemetry"]["vector_search_ms"] == 0.0
    assert data["telemetry"]["graph_traversal_ms"] >= 0.0
    assert len(data["results"]) == 1
    hit = data["results"][0]
    assert hit["result_type"] == "code"
    assert hit["metadata"]["function_name"] == "query"
    assert hit["graph_context"]["module"]["name"] == "src"
    assert hit["graph_context"]["owners"][0]["name"] == "Erfan"
