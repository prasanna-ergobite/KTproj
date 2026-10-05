"""
Document Ingestion Graph / Pipeline Engine for AutoKT.
Parses uploaded documents, chunks text (heading-aware or fixed-window),
links documents to Neo4j Module nodes, creates Document graph nodes and
DOCUMENTS relationships, and indexes vector embeddings in ChromaDB.
"""

import os
import re
import shutil
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from app.core.config import settings
from app.db.neo4j_client import neo4j_client
from app.db.chroma_client import chroma_client
from app.db.postgres_client import SessionLocal
from app.models.db_models import Task
from app.core.doc_parser import parse_document
from app.core.doc_chunker import chunk_document, DocChunk
from app.core.embedder import embed_documents, count_tokens

logger = logging.getLogger("autokt.doc_ingestion")

STOP_TOKENS = {
    "api", "test", "util", "utils", "index", "main", "init", "base",
    "core", "lib", "src", "app", "web", "doc", "docs", "readme",
    "changelog", "license", "todo",
}


def _tokenize_relative_path(relative_path: str) -> List[str]:
    """
    Split on path separators and word-boundary delimiters; keep tokens >= 4 chars
    not in STOP_TOKENS.
    """
    flat = relative_path.replace("\\", "/").replace("/", "_")
    stem = Path(flat).stem.lower()
    tokens = re.split(r"[_\-.\s]+", stem)
    return [t for t in tokens if len(t) >= 4 and t not in STOP_TOKENS]


def _token_matches_module(token: str, module_name: str, module_path: str) -> bool:
    """
    True if token matches as a whole word in module name or path.
    Uses \\b word boundaries to avoid substring false positives.
    """
    pattern = re.compile(r"\b" + re.escape(token) + r"\b", re.IGNORECASE)
    return bool(pattern.search(module_name)) or bool(pattern.search(module_path))


def _update_task_result(task_id: str, status: str, result: Dict[str, Any]) -> None:
    """Helper to update Task status and result in PostgreSQL."""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status = status
            task.result = result
            task.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        logger.error("Failed to update task result for task %s: %s", task_id, exc)
    finally:
        db.close()


def run_doc_ingestion_task(
    task_id: str,
    file_paths: List[str],
    relative_paths: List[str],
    organization_id: str,
    source_type: str = "doc",
) -> Dict[str, Any]:
    """
    Asynchronous task entry point for document upload and ingestion.
    Executes text parsing, chunking, Neo4j Document node MERGE, Module path-linking,
    and ChromaDB vector store indexing.
    """
    logger.info("Starting document ingestion task %s for org '%s' (%d files)...", task_id, organization_id, len(file_paths))

    # 1. Update task state to running
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status = "running"
            task.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        logger.error("Failed to set doc ingestion task status to running: %s", exc)
    finally:
        db.close()

    summary: Dict[str, Any] = {
        "organization_id": organization_id,
        "files_processed": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "chunks_count": 0,
        "documents_linked": 0,
        "documents_unlinked": 0,
        "error": None,
    }

    temp_dir = Path("tmp_docs") / organization_id / task_id

    try:
        # Fetch candidate modules for path matching once
        modules: List[Dict[str, Any]] = []
        try:
            cypher_modules = (
                "MATCH (m:Module {organization_id: $organization_id}) "
                "RETURN m.id AS id, m.name AS name, m.path AS path "
                "ORDER BY size(m.path) DESC, m.name ASC"
            )
            modules = neo4j_client.run_read_query(cypher_modules, organization_id=organization_id)
        except Exception as mod_exc:
            logger.warning("Could not fetch Neo4j Module nodes for path matching: %s", mod_exc)

        for abs_file_path_str, rel_path in zip(file_paths, relative_paths):
            abs_file_path = Path(abs_file_path_str)
            if not abs_file_path.exists():
                logger.warning("File path '%s' does not exist. Skipping.", abs_file_path)
                summary["files_failed"] += 1
                continue

            try:
                # Step 1: Compute identity
                file_bytes = abs_file_path.read_bytes()
                content_hash = hashlib.sha256(file_bytes).hexdigest()[:32]
                filename = Path(rel_path).name
                doc_id = f"doc:{rel_path}"
                tenant_key = f"{organization_id}:{doc_id}"
                # ingested_at computed once here; reused for last_modified_date proxy
                ingested_at = datetime.now(timezone.utc).isoformat()

                # Step 2: Phase 1 relative_path lookup (prior version detection).
                # Projects d.version so we can increment it on re-ingest.
                cypher_lookup = (
                    "MATCH (d:Document {organization_id: $organization_id, relative_path: $relative_path}) "
                    "RETURN d.tenant_key AS tk, d.content_hash AS ch, d.id AS did, "
                    "coalesce(d.version, 0) AS ver"
                )
                prior_docs = neo4j_client.run_read_query(
                    cypher_lookup,
                    parameters={"relative_path": rel_path},
                    organization_id=organization_id,
                )

                if prior_docs:
                    prior = prior_docs[0]
                    prior_content_hash = prior.get("ch", "")
                    if prior_content_hash == content_hash:
                        # Same content → skip. version is NOT incremented.
                        logger.info("Skipping '%s': content_hash unchanged.", rel_path)
                        summary["files_skipped"] += 1
                        continue
                    else:
                        # Different content → re-ingest path: increment version.
                        version = prior.get("ver", 0) + 1
                        logger.info(
                            "Replacing prior version of '%s' (old_hash=%s -> new_hash=%s, new_version=%d)",
                            rel_path, prior_content_hash, content_hash, version
                        )
                        # Delete old Chroma chunks
                        try:
                            chroma_client.delete_by_metadata(
                                "docs_chunks",
                                organization_id=organization_id,
                                extra_filter={"doc_id": {"$eq": doc_id}},
                            )
                        except Exception as chroma_del_exc:
                            logger.warning("Error deleting prior Chroma chunks for '%s': %s", rel_path, chroma_del_exc)

                        # Delete old Neo4j Document node
                        try:
                            cypher_del = "MATCH (d:Document {tenant_key: $tenant_key}) DETACH DELETE d"
                            neo4j_client.run_write_query(
                                cypher_del,
                                parameters={"tenant_key": tenant_key},
                                organization_id=organization_id,
                            )
                        except Exception as neo_del_exc:
                            logger.warning("Error deleting prior Neo4j Document node for '%s': %s", rel_path, neo_del_exc)
                else:
                    # First ingest → version starts at 1.
                    version = 1

                # Step 3: Parse + chunk
                text, chunking_method = parse_document(abs_file_path, filename)
                chunks: List[DocChunk] = chunk_document(
                    text=text,
                    doc_id=doc_id,
                    filename=filename,
                    source_type=source_type,
                    chunking_method=chunking_method,
                )

                # Step 4: Module path-match
                matched_module_id = ""
                tokens = _tokenize_relative_path(rel_path)
                if tokens and modules:
                    for m in modules:
                        m_name = m.get("name") or ""
                        m_path = m.get("path") or ""
                        if any(_token_matches_module(t, m_name, m_path) for t in tokens):
                            matched_module_id = m.get("id") or ""
                            break

                if matched_module_id:
                    summary["documents_linked"] += 1
                else:
                    summary["documents_unlinked"] += 1

                # Step 4b: Compute per-file derived metadata fields (used in Step 7 Chroma write).
                # file_format: lowercase extension without dot (e.g. "md", "pdf").
                file_format = Path(filename).suffix.lstrip(".").lower() or "unknown"
                # last_modified_date: upload-timestamp proxy (temp file mtime = copy time, not
                # the original file's mtime). Documented limitation in autokt_progress_context.md.
                last_modified_date = ingested_at
                # source_confidence: categorical label from the module-link step.
                # The DOCUMENTS edge confidence is always hardcoded 1.0; a numeric float
                # is deferred until LLM semantic matching produces variable scores.
                source_confidence = "path_match" if matched_module_id else ""
                # token_count: real BPE count via Nomic tokenizer in nomic mode;
                # 0 sentinel in stub mode (avoids loading model unnecessarily during tests).
                if not settings.EMBEDDING_PROVIDER or settings.EMBEDDING_PROVIDER == "stub":
                    token_counts = [0] * len(chunks)
                else:
                    token_counts = count_tokens([c.text for c in chunks])

                # Step 5: MERGE Document node (now also persists version for re-ingest tracking)
                cypher_merge_doc = (
                    "MERGE (d:Document {tenant_key: $tenant_key}) "
                    "SET d.id = $id, d.organization_id = $organization_id, "
                    "d.relative_path = $relative_path, d.filename = $filename, "
                    "d.content_hash = $content_hash, d.source_type = $source_type, "
                    "d.module_id = $module_id, d.chunks_count = $chunks_count, "
                    "d.ingested_at = $ingested_at, d.version = $version "
                    "RETURN d"
                )
                neo4j_client.run_write_query(
                    cypher_merge_doc,
                    parameters={
                        "tenant_key": tenant_key,
                        "id": doc_id,
                        "relative_path": rel_path,
                        "filename": filename,
                        "content_hash": content_hash,
                        "source_type": source_type,
                        "module_id": matched_module_id,
                        "chunks_count": len(chunks),
                        "ingested_at": ingested_at,
                        "version": version,
                    },
                    organization_id=organization_id,
                )

                # Step 6: MERGE DOCUMENTS edge if module matched
                if matched_module_id:
                    mod_tenant_key = f"{organization_id}:{matched_module_id}"
                    cypher_doc_edge = (
                        "MATCH (d:Document {tenant_key: $tenant_key}), "
                        "(m:Module {tenant_key: $mod_tenant_key}) "
                        "MERGE (d)-[:DOCUMENTS {confidence: 1.0, source: 'path_match'}]->(m)"
                    )
                    try:
                        neo4j_client.run_write_query(
                            cypher_doc_edge,
                            parameters={
                                "tenant_key": tenant_key,
                                "mod_tenant_key": mod_tenant_key,
                            },
                            organization_id=organization_id,
                        )
                    except Exception as edge_exc:
                        logger.warning("Could not MERGE DOCUMENTS edge for '%s': %s", rel_path, edge_exc)

                # Step 7: Chroma write (gated on settings.EMBEDDING_PROVIDER)
                if settings.EMBEDDING_PROVIDER and chunks:
                    try:
                        extra_metadata = [
                            {
                                # --- Existing metadata fields ---
                                "doc_id": doc_id,
                                "heading_path": " > ".join(c.heading_path),
                                "parent_section_id": c.parent_section_id,
                                "section_level": c.section_level,
                                "chunking_method": c.chunking_method,
                                "chunk_index": c.chunk_index,
                                "relative_path": rel_path,
                                # --- New enriched metadata fields (Milestone 12) ---
                                "chunk_type": c.chunk_type,
                                "chunk_total": c.chunk_total,
                                "file_format": file_format,
                                # chunk-level SHA-256 (64-char full hex) — distinct from
                                # the doc-level content_hash (32-char prefix on Neo4j node
                                # used for dedup decisions). The dedup path reads d.content_hash
                                # from Neo4j, not from Chroma — no interference possible.
                                "content_hash": hashlib.sha256(c.text.encode("utf-8")).hexdigest(),
                                "last_modified_date": last_modified_date,
                                "ingested_at": ingested_at,
                                "version": version,
                                "uploaded_by": "",          # sentinel: user_id not threaded to bg task yet
                                "source_confidence": source_confidence,
                                "token_count": token_counts[i],
                                "has_code_fence": c.has_code_fence,
                                "is_stub_or_empty": c.is_stub_or_empty,
                                "visibility": "internal",
                            }
                            for i, c in enumerate(chunks)
                        ]

                        # Use stub provider in tests; real nomic embeddings in production
                        if settings.EMBEDDING_PROVIDER == "stub":
                            from tests.helpers.stub_embedder import stub_embed
                            embeddings = stub_embed([c.text for c in chunks])
                        else:
                            embeddings = embed_documents([c.text for c in chunks])

                        chroma_client.add_documents(
                            collection_name="docs_chunks",
                            documents=[c.text for c in chunks],
                            embeddings=embeddings,
                            ids=[c.chunk_id for c in chunks],
                            organization_id=organization_id,
                            module_id=matched_module_id if matched_module_id else None,
                            source_type=source_type,
                            extra_metadata=extra_metadata,
                        )
                    except Exception as chroma_add_exc:
                        logger.warning("Failed to add vector chunks to ChromaDB for '%s': %s", rel_path, chroma_add_exc)

                summary["chunks_count"] += len(chunks)
                summary["files_processed"] += 1

            except Exception as file_exc:
                logger.error("Error processing file '%s': %s", rel_path, file_exc)
                summary["files_failed"] += 1

        # Determine task completion status
        if summary["files_processed"] > 0 or summary["files_skipped"] > 0:
            if not settings.EMBEDDING_PROVIDER:
                final_status = "completed_without_embeddings"
            else:
                final_status = "completed"
        elif summary["files_failed"] > 0:
            final_status = "failed"
        else:
            final_status = "completed"

        _update_task_result(task_id, final_status, summary)
        logger.info("Document ingestion task %s completed with status '%s'.", task_id, final_status)
        return summary

    except Exception as exc:
        logger.error("Document ingestion task %s failed: %s", task_id, exc)
        summary["error"] = str(exc)
        _update_task_result(task_id, "failed", summary)
        return summary

    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
