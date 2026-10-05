"""
Unit tests for app/core/doc_chunker.py (15 unit tests).
"""

import sys
from pathlib import Path
import pytest

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.core.doc_chunker import (
    chunk_document,
    DocChunk,
    FIXED_WINDOW_TARGET_CHARS,
    FIXED_WINDOW_OVERLAP_CHARS,
    HEADING_AWARE_MAX_CHARS,
)


def test_empty_document_returns_empty_list():
    """Test 1: Empty or whitespace input returns [] without error."""
    assert chunk_document("", "doc1", "empty.md") == []
    assert chunk_document("   \n\t\n  ", "doc2", "blank.txt") == []


def test_short_doc_fits_in_one_chunk():
    """Test 2: Doc smaller than target size fits into a single chunk."""
    text = "# Introduction\nThis is a short document that easily fits in one chunk."
    chunks = chunk_document(text, "doc:short", "short.md", chunking_method="heading_aware")
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].heading_path == ["Introduction"]


def test_md_single_heading_produces_heading_body_chunk():
    """Test 3: Single # Heading + body produces 1 heading-aware chunk with correct path."""
    text = "# Overview\nAutoKT automates knowledge transfer using living graphs."
    chunks = chunk_document(text, "doc:overview", "overview.md")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "heading_aware"
    assert c.heading_path == ["Overview"]
    assert "AutoKT automates knowledge transfer" in c.text
    # New enriched fields
    assert c.chunk_type == "heading_body"
    assert c.chunk_total == 1
    assert isinstance(c.has_code_fence, bool)
    assert isinstance(c.is_stub_or_empty, bool)


def test_md_multiple_headings_split_correctly():
    """Test 4: 3 ## headings produce 3 distinct chunks with correct heading_path."""
    text = (
        "## Architecture\nFastAPI backend with Neo4j graph.\n\n"
        "## Database\nPostgreSQL tables store tasks and histories.\n\n"
        "## Vector Store\nChromaDB stores document embeddings.\n"
    )
    chunks = chunk_document(text, "doc:multi", "multi.md")
    assert len(chunks) == 3
    paths = [c.heading_path for c in chunks]
    assert paths == [["Architecture"], ["Database"], ["Vector Store"]]


def test_md_nested_headings_produce_heading_path():
    """Test 5: ## H2 under # H1 produces parent and child chunks with parent_section_id link."""
    text = (
        "# System Architecture\n"
        "This document describes the AutoKT system architecture in full production detail, "
        "covering foundational components, multi-tenant isolation, data indexing pipelines, "
        "and integration with vector and graph database engines.\n\n"
        "## Backend Engine\n"
        "The backend engine runs FastAPI and Python services, providing async background execution, "
        "PostgreSQL task state persistence, Neo4j graph operations, ChromaDB vector indexing, "
        "and comprehensive error handling across all API endpoints.\n"
    )
    chunks = chunk_document(text, "doc:nested", "nested.md")
    assert len(chunks) == 2

    parent_chunk = chunks[0]
    child_chunk = chunks[1]

    assert parent_chunk.heading_path == ["System Architecture"]
    assert parent_chunk.section_level == 1
    assert parent_chunk.parent_section_id == ""

    assert child_chunk.heading_path == ["System Architecture", "Backend Engine"]
    assert child_chunk.section_level == 2
    assert child_chunk.parent_section_id == parent_chunk.chunk_id


def test_heading_with_no_direct_body_emits_empty_body_chunk():
    """Test 6: ## Section with no direct body text still emits a container chunk."""
    text = (
        "# Parent Section\n"
        "## Child Section\n"
        "Child section content goes here with full detailed explanations of system workflow, "
        "ensuring the section body length easily satisfies the minimum token threshold requirement "
        "by expanding paragraph detail over three hundred characters in total length.\n"
    )
    chunks = chunk_document(text, "doc:container", "container.md")
    assert len(chunks) == 2
    parent_chunk = chunks[0]
    assert parent_chunk.heading_path == ["Parent Section"]
    assert parent_chunk.section_level == 1
    assert chunks[1].parent_section_id == parent_chunk.chunk_id


def test_md_code_fence_not_split():
    """Test 7: Code fence containing # comments is not split into markdown headings."""
    text = (
        "# Code Example\n"
        "Here is a code block:\n"
        "```python\n"
        "# This is a python comment, not a markdown heading\n"
        "def hello():\n"
        "    print('world')\n"
        "```\n"
    )
    chunks = chunk_document(text, "doc:code", "code.md")
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Code Example"]
    assert "def hello():" in chunks[0].text
    assert "# This is a python comment" in chunks[0].text
    # Code fence guard: heading_body chunk should detect the fence markers
    assert chunks[0].has_code_fence is True


def test_md_table_not_split():
    """Test 8: Markdown table is preserved in body text without splitting."""
    text = (
        "# API Summary\n"
        "| Endpoint | Method | Description |\n"
        "| --- | --- | --- |\n"
        "| /repos | POST | Ingest repository |\n"
        "| /docs | POST | Ingest documents |\n"
    )
    chunks = chunk_document(text, "doc:table", "table.md")
    assert len(chunks) == 1
    assert "| /repos | POST | Ingest repository |" in chunks[0].text


def test_short_section_merges_into_parent():
    """Test 9: Very short leaf section (<50 tokens) merges body into parent."""
    text = (
        "# System Component\n"
        "Main system component description text that provides baseline context.\n\n"
        "## SubNote\n"
        "Tiny note.\n"
    )
    chunks = chunk_document(text, "doc:short_merge", "merge.md")
    # Tiny note merged into parent section
    assert len(chunks) == 1
    assert "Tiny note." in chunks[0].text


def test_long_section_sub_splits():
    """Test 10: Section body > HEADING_AWARE_MAX_CHARS sub-splits into multiple chunks sharing heading_path."""
    long_para = "AutoKT platform data processing line. " * 100
    text = f"# Big Section\n{long_para}\n\n{long_para}\n"
    chunks = chunk_document(text, "doc:long", "long.md")
    assert len(chunks) >= 2
    for c in chunks:
        assert c.heading_path == ["Big Section"]
        assert c.chunking_method == "heading_aware"


def test_chunk_ids_are_unique():
    """Test 11: All chunk_id values produced for a document are distinct."""
    text = (
        "# H1\nContent 1\n"
        "## H2\nContent 2\n"
        "### H3\nContent 3\n"
    )
    chunks = chunk_document(text, "doc:unique", "unique.md")
    chunk_ids = [c.chunk_id for c in chunks]
    assert len(chunk_ids) == len(set(chunk_ids))


def test_chunk_header_format():
    """Test 12: Every chunk text starts with standardized header prefix."""
    text = "# Guide\nContent here."
    chunks = chunk_document(text, "doc:hdr", "guide.md")
    assert len(chunks) == 1
    hdr = chunks[0].text
    assert hdr.startswith("# Document: guide.md\n# Section: Guide\n# Chunk: 1/1\n")


def test_fixed_window_fallback_for_txt():
    """Test 13: Fixed window fallback produces heading_path=[] and # Chunk: N/M header."""
    text = "Plain text document content without any markdown headings. " * 50
    chunks = chunk_document(text, "doc:txt", "notes.txt", chunking_method="fixed_window")
    assert len(chunks) >= 1
    c = chunks[0]
    assert c.chunking_method == "fixed_window"
    assert c.heading_path == []
    assert "# Document: notes.txt\n# Section: root\n# Chunk:" in c.text
    # New enriched fields for fixed-window path
    assert c.chunk_type == "fixed_window"
    assert c.chunk_total == len(chunks)
    assert isinstance(c.has_code_fence, bool)
    assert c.is_stub_or_empty is False


if __name__ == "__main__":
    pytest.main([__file__, "-s"])


def test_doc_chunk_new_fields_types():
    """Test 14: New enriched fields have correct types and values across heading_aware and fixed_window paths."""
    # heading_aware path
    text_md = "# Installation\nInstall the dependencies with pip install autokt.\n"
    chunks_md = chunk_document(text_md, "doc:fields", "install.md", chunking_method="heading_aware")
    assert len(chunks_md) >= 1
    for c in chunks_md:
        assert c.chunk_type in {"heading_body", "fixed_window"}, f"unexpected chunk_type: {c.chunk_type!r}"
        assert c.chunk_total == len(chunks_md), "chunk_total must equal total document chunks"
        assert isinstance(c.has_code_fence, bool), "has_code_fence must be bool"
        assert isinstance(c.is_stub_or_empty, bool), "is_stub_or_empty must be bool"

    # fixed_window path
    text_txt = "Plain text content without any markdown headings. " * 20
    chunks_txt = chunk_document(text_txt, "doc:fields_fw", "notes.txt", chunking_method="fixed_window")
    assert len(chunks_txt) >= 1
    for c in chunks_txt:
        assert c.chunk_type == "fixed_window"
        assert c.chunk_total == len(chunks_txt)
        assert c.is_stub_or_empty is False


def test_has_code_fence_detection():
    """Test 15: has_code_fence=True when chunk contains fence markers; False otherwise; is_stub_or_empty=True for empty heading body."""
    # Chunk with code fence — should have has_code_fence=True
    text_with_fence = (
        "# Code Example\n"
        "Here is a code block:\n"
        "```python\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "```\n"
    )
    chunks_fence = chunk_document(text_with_fence, "doc:fence", "code.md")
    assert len(chunks_fence) == 1
    assert chunks_fence[0].has_code_fence is True

    # Chunk without any fence markers — should have has_code_fence=False
    text_no_fence = "# Overview\nThis section contains no code blocks, only prose text.\n"
    chunks_no_fence = chunk_document(text_no_fence, "doc:nofence", "prose.md")
    assert len(chunks_no_fence) == 1
    assert chunks_no_fence[0].has_code_fence is False

    # Heading section with empty body — is_stub_or_empty must be True.
    # Strategy: parent heading # P with NO direct body text, and a long-enough child
    # ## C body that exceeds MIN_SECTION_TOKENS (~50 approx tokens = 200 chars) so the
    # child is NOT merged back into the parent. The parent ## heading is emitted with
    # body_text="" → is_stub_or_empty=True.
    long_child_body = (
        "This child section has extensive content that clearly exceeds the minimum section "
        "token threshold of fifty approximate tokens to prevent short-section merging from "
        "collapsing this child into the parent. The parent heading should therefore be emitted "
        "with an empty body, yielding is_stub_or_empty=True on its DocChunk. " * 2
    )
    text_stub = f"# Parent\n## Child\n{long_child_body}\n"
    chunks_stub = chunk_document(text_stub, "doc:stub", "stub.md")
    parent_chunks = [c for c in chunks_stub if c.heading_path == ["Parent"]]
    # With a long enough child, parent must be emitted separately with empty body
    assert len(parent_chunks) > 0, (
        "Parent heading not emitted as a separate chunk — child was merged (child text too short)"
    )
    assert parent_chunks[0].is_stub_or_empty is True


def test_pdf_sourced_markdown_heading_aware_chunks():
    """Test 16: Pre-converted PDF-markdown text produces heading-aware chunks with non-empty heading_path."""
    pdf_md_text = (
        "# 1. Overview\n"
        "This is the top-level section converted from PDF text.\n\n"
        "## 1.1 Architectural Details\n"
        "Detailed architecture specs extracted from PDF page 2.\n"
    )
    chunks = chunk_document(pdf_md_text, "bdoc:sample.pdf", "sample.pdf", chunking_method="heading_aware")
    assert len(chunks) >= 1
    
    paths = [c.heading_path for c in chunks]
    assert ["1. Overview"] in paths or any("1. Overview" in p for p in paths)
    assert any(c.chunking_method == "heading_aware" for c in chunks)
    assert not all(c.heading_path == [] for c in chunks)

