"""
Business Document Ingestion Graph / Pipeline Engine for AutoKT.
Parses uploaded business documents (PRDs, BRDs, specs, etc.), chunks text,
creates Document graph nodes linked to Repository (and optionally Module) in Neo4j,
and indexes vector embeddings in ChromaDB with doc_type and repository_id metadata.
"""

import os
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

logger = logging.getLogger("autokt.business_doc_ingestion")


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


def run_business_doc_ingestion_task(
    task_id: str,
    file_paths: List[str],
    filenames: List[str],
    organization_id: str,
    repository_id: str,
    doc_type: str = "business",
    module_id: str = "",
) -> Dict[str, Any]:
    """
    Asynchronous task entry point for business document upload and ingestion.
    Executes text parsing, chunking, Neo4j Document node MERGE, Repository/Module graph linking,
    and ChromaDB vector store indexing with doc_type and repository_id metadata.
    """
    logger.info(
        "Starting business doc ingestion task %s for org '%s', repo '%s' (%d files)...",
        task_id, organization_id, repository_id, len(file_paths)
    )

    # 1. Update task state to running
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status = "running"
            task.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        logger.error("Failed to set business doc ingestion task status to running: %s", exc)
    finally:
        db.close()

    summary: Dict[str, Any] = {
        "organization_id": organization_id,
        "repository_id": repository_id,
        "doc_type": doc_type,
        "module_id": module_id,
        "files_processed": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "chunks_count": 0,
        "documents_linked": 0,
        "error": None,
    }

    temp_dir = Path("tmp_business_docs") / organization_id / task_id

    try:
        for abs_file_path_str, filename in zip(file_paths, filenames):
            abs_file_path = Path(abs_file_path_str)
            if not abs_file_path.exists():
                logger.warning("File path '%s' does not exist. Skipping.", abs_file_path)
                summary["files_failed"] += 1
                continue

            try:
                # Step 1: Compute identity & tenant keys
                file_bytes = abs_file_path.read_bytes()
                content_hash = hashlib.sha256(file_bytes).hexdigest()[:32]
                doc_id = f"bdoc:{repository_id}/{filename}"
                tenant_key = f"{organization_id}:{doc_id}"
                ingested_at = datetime.now(timezone.utc).isoformat()

                # Step 2: Prior version lookup (dedup by org_id + doc_id)
                cypher_lookup = (
                    "MATCH (d:Document {organization_id: $organization_id, id: $doc_id}) "
                    "RETURN d.tenant_key AS tk, d.content_hash AS ch, "
                    "coalesce(d.version, 0) AS ver"
                )
                prior_docs = neo4j_client.run_read_query(
                    cypher_lookup,
                    parameters={"doc_id": doc_id},
                    organization_id=organization_id,
                )

                if prior_docs:
                    prior = prior_docs[0]
                    prior_content_hash = prior.get("ch", "")
                    if prior_content_hash == content_hash:
                        logger.info("Skipping business doc '%s': content_hash unchanged.", filename)
                        summary["files_skipped"] += 1
                        continue
                    else:
                        version = prior.get("ver", 0) + 1
                        logger.info(
                            "Replacing prior version of business doc '%s' (old_hash=%s -> new_hash=%s, new_version=%d)",
                            filename, prior_content_hash, content_hash, version
                        )
                        # Delete old Chroma chunks
                        try:
                            chroma_client.delete_by_metadata(
                                "docs_chunks",
                                organization_id=organization_id,
                                extra_filter={"doc_id": {"$eq": doc_id}},
                            )
                        except Exception as chroma_del_exc:
                            logger.warning("Error deleting prior Chroma chunks for '%s': %s", filename, chroma_del_exc)

                        # Delete old Neo4j Document node
                        try:
                            cypher_del = "MATCH (d:Document {tenant_key: $tenant_key}) DETACH DELETE d"
                            neo4j_client.run_write_query(
                                cypher_del,
                                parameters={"tenant_key": tenant_key},
                                organization_id=organization_id,
                            )
                        except Exception as neo_del_exc:
                            logger.warning("Error deleting prior Neo4j Document node for '%s': %s", filename, neo_del_exc)
                else:
                    version = 1

                # Step 3: Parse + chunk
                text, chunking_method = parse_document(abs_file_path, filename)
                chunks: List[DocChunk] = chunk_document(
                    text=text,
                    doc_id=doc_id,
                    filename=filename,
                    source_type="doc",
                    chunking_method=chunking_method,
                )

                file_format = Path(filename).suffix.lstrip(".").lower() or "unknown"
                last_modified_date = ingested_at

                if not settings.EMBEDDING_PROVIDER or settings.EMBEDDING_PROVIDER == "stub":
                    token_counts = [0] * len(chunks)
                else:
                    token_counts = count_tokens([c.text for c in chunks])

                # Step 4: MERGE Document node in Neo4j
                cypher_merge_doc = (
                    "MERGE (d:Document {tenant_key: $tenant_key}) "
                    "SET d.id = $id, d.organization_id = $organization_id, "
                    "d.relative_path = '', d.filename = $filename, "
                    "d.content_hash = $content_hash, d.source_type = 'doc', "
                    "d.doc_type = $doc_type, d.repository_id = $repository_id, "
                    "d.module_id = $module_id, d.chunks_count = $chunks_count, "
                    "d.ingested_at = $ingested_at, d.version = $version "
                    "RETURN d"
                )
                neo4j_client.run_write_query(
                    cypher_merge_doc,
                    parameters={
                        "tenant_key": tenant_key,
                        "id": doc_id,
                        "filename": filename,
                        "content_hash": content_hash,
                        "doc_type": doc_type or "business",
                        "repository_id": repository_id,
                        "module_id": module_id or "",
                        "chunks_count": len(chunks),
                        "ingested_at": ingested_at,
                        "version": version,
                    },
                    organization_id=organization_id,
                )

                # Step 5a: MERGE DOCUMENTS edge to Repository (always)
                cypher_repo_edge = (
                    "MATCH (d:Document {tenant_key: $tenant_key}), "
                    "(r:Repository {organization_id: $organization_id, id: $repository_id}) "
                    "MERGE (d)-[:DOCUMENTS {confidence: 1.0, source: 'explicit_upload'}]->(r)"
                )
                try:
                    neo4j_client.run_write_query(
                        cypher_repo_edge,
                        parameters={
                            "tenant_key": tenant_key,
                            "repository_id": repository_id,
                        },
                        organization_id=organization_id,
                    )
                    summary["documents_linked"] += 1
                except Exception as edge_exc:
                    logger.warning("Could not MERGE DOCUMENTS repo edge for '%s': %s", filename, edge_exc)

                # Step 5b: MERGE DOCUMENTS edge to Module (if module_id provided)
                if module_id and module_id.strip():
                    mod_tenant_key = f"{organization_id}:{module_id.strip()}"
                    cypher_mod_edge = (
                        "MATCH (d:Document {tenant_key: $tenant_key}), "
                        "(m:Module {tenant_key: $mod_tenant_key}) "
                        "MERGE (d)-[:DOCUMENTS {confidence: 1.0, source: 'explicit_upload'}]->(m)"
                    )
                    try:
                        neo4j_client.run_write_query(
                            cypher_mod_edge,
                            parameters={
                                "tenant_key": tenant_key,
                                "mod_tenant_key": mod_tenant_key,
                            },
                            organization_id=organization_id,
                        )
                    except Exception as mod_edge_exc:
                        logger.warning("Could not MERGE DOCUMENTS module edge for '%s': %s", filename, mod_edge_exc)

                # Step 6: Chroma write to docs_chunks
                if settings.EMBEDDING_PROVIDER and chunks:
                    try:
                        extra_metadata = [
                            {
                                "doc_id": doc_id,
                                "heading_path": " > ".join(c.heading_path),
                                "parent_section_id": c.parent_section_id,
                                "section_level": c.section_level,
                                "chunking_method": c.chunking_method,
                                "chunk_index": c.chunk_index,
                                "relative_path": "",
                                "chunk_type": c.chunk_type,
                                "chunk_total": c.chunk_total,
                                "file_format": file_format,
                                "content_hash": hashlib.sha256(c.text.encode("utf-8")).hexdigest(),
                                "last_modified_date": last_modified_date,
                                "ingested_at": ingested_at,
                                "version": version,
                                "uploaded_by": "",
                                "source_confidence": "explicit_upload",
                                "token_count": token_counts[i],
                                "has_code_fence": c.has_code_fence,
                                "is_stub_or_empty": c.is_stub_or_empty,
                                "visibility": "internal",
                                "doc_type": doc_type or "business",
                                "repository_id": repository_id or "",
                            }
                            for i, c in enumerate(chunks)
                        ]

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
                            module_id=module_id if module_id else None,
                            source_type="doc",
                            extra_metadata=extra_metadata,
                        )
                    except Exception as chroma_add_exc:
                        logger.warning("Failed to add business vector chunks to ChromaDB for '%s': %s", filename, chroma_add_exc)

                summary["chunks_count"] += len(chunks)
                summary["files_processed"] += 1

            except Exception as file_exc:
                logger.error("Error processing business doc file '%s': %s", filename, file_exc)
                summary["files_failed"] += 1

        # Determine final completion status
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
        logger.info("Business document ingestion task %s completed with status '%s'.", task_id, final_status)
        return summary

    except Exception as exc:
        logger.error("Business document ingestion task %s failed: %s", task_id, exc)
        summary["error"] = str(exc)
        _update_task_result(task_id, "failed", summary)
        return summary

    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
