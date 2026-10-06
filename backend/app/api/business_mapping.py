"""
Business-to-Code Mapping API Router (Milestone 20).
Provides synchronous single-chunk business requirement mapping endpoint
and asynchronous full-document batch mapping pipeline trigger and task status polling.
"""

import logging
import time
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.postgres_client import get_db, SessionLocal
from app.models.db_models import Task
from app.core.constants import ensure_system_user
from app.db.chroma_client import chroma_client
from app.db.task_helpers import update_task_completed, update_task_failed
from app.core.business_mapping import (
    map_business_chunk_to_code,
    BusinessMappingResult,
    BusinessChunkRef,
)

logger = logging.getLogger("autokt.api.business_mapping")

router = APIRouter(prefix="/business-mapping", tags=["Business Mapping"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------

class BusinessMappingRequest(BaseModel):
    organization_id: str = Field(..., description="Tenant organization identifier")
    repository_id: str = Field(..., description="Target repository identifier in Neo4j and Chroma")
    chunk_id: Optional[str] = Field(None, description="Document chunk ID (XOR with business_text)")
    business_text: Optional[str] = Field(None, description="Free-text business concept / requirement (XOR with chunk_id)")
    use_llm_judge: bool = Field(True, description="Whether to execute Stage 3 LLM grounded judging stage")
    max_mappings: int = Field(5, ge=1, le=10, description="Maximum mappings to return")


class DocumentMappingRequest(BaseModel):
    organization_id: str = Field(..., description="Tenant organization identifier")
    repository_id: str = Field(..., description="Target repository identifier in Neo4j and Chroma")
    document_id: str = Field(..., description="Target business document node ID in Chroma (e.g. 'bdoc:repo:chatdoc/ChatDoc_PRD.pdf')")
    use_llm_judge: bool = Field(True, description="Whether to execute Stage 3 LLM grounded judging stage")
    max_mappings_per_chunk: int = Field(3, ge=1, le=10, description="Maximum mappings per business doc chunk")


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    payload: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Background Task Runner for Full-Document Batch Mapping
# ---------------------------------------------------------------------------

def run_business_mapping_document_task(
    task_id: str,
    organization_id: str,
    repository_id: str,
    document_id: str,
    use_llm_judge: bool = True,
    max_mappings_per_chunk: int = 3,
) -> Dict[str, Any]:
    """
    Executes background batch business-to-code mapping for all chunks of a business document.
    """
    t_start = time.perf_counter()
    org_id = organization_id.strip()
    repo_id = repository_id.strip()
    doc_id = document_id.strip()

    logger.info("Starting background business mapping task %s for doc '%s'...", task_id, doc_id)

    try:
        # Fetch all chunks for target document from docs_chunks
        doc_res = chroma_client.get_documents(
            collection_name="docs_chunks",
            organization_id=org_id,
            extra_filter={"doc_id": {"$eq": doc_id}},
        )

        c_ids = doc_res.get("ids", [])
        c_docs = doc_res.get("documents", [])
        c_metas = doc_res.get("metadatas") or [{}] * len(c_ids)

        if not c_ids:
            err_msg = f"No document chunks found for document_id '{doc_id}' in organization '{org_id}'."
            update_task_failed(task_id, err_msg)
            return {"error": err_msg}

        chunk_results: List[Dict[str, Any]] = []
        chunks_mapped = 0
        chunks_unmapped = 0
        overall_quality = "ok"

        for cid, text, meta in zip(c_ids, c_docs, c_metas):
            txt = (text or "").strip()
            heading = meta.get("heading_path", "")

            # Known limitation #1: Filter out short sentence fragments or stub chunks
            if len(txt) < 100 or meta.get("is_stub_or_empty", False):
                chunk_results.append({
                    "chunk_id": cid,
                    "heading_path": heading,
                    "skipped": True,
                    "reason": "Chunk text too short or empty (PDF page boundary fragment).",
                    "mappings": [],
                })
                chunks_unmapped += 1
                continue

            res = map_business_chunk_to_code(
                organization_id=org_id,
                repository_id=repo_id,
                business_text=txt,
                chunk_id=cid,
                heading_path=heading,
                use_llm_judge=use_llm_judge,
                max_mappings=max_mappings_per_chunk,
            )

            if res.generation_quality == "grounding_warning":
                overall_quality = "grounding_warning"

            if res.mappings:
                chunks_mapped += 1
            else:
                chunks_unmapped += 1

            chunk_results.append(res.model_dump())

        total_ms = (time.perf_counter() - t_start) * 1000.0

        summary_payload = {
            "document_id": doc_id,
            "organization_id": org_id,
            "repository_id": repo_id,
            "chunks_processed": len(c_ids),
            "chunks_mapped": chunks_mapped,
            "chunks_unmapped": chunks_unmapped,
            "overall_generation_quality": overall_quality,
            "chunk_mappings": chunk_results,
            "telemetry": {
                "total_ms": round(total_ms, 2),
                "use_llm_judge": use_llm_judge,
            },
        }

        update_task_completed(task_id, summary_payload)
        logger.info("Business mapping task %s completed: %d/%d chunks mapped.", task_id, chunks_mapped, len(c_ids))
        return summary_payload

    except Exception as exc:
        logger.error("Business mapping task %s failed: %s", task_id, exc)
        update_task_failed(task_id, str(exc))
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=BusinessMappingResult, status_code=status.HTTP_200_OK)
async def map_business_concept(request: BusinessMappingRequest):
    """
    Synchronously map a single business requirement text chunk to corresponding source code.

    Validation rules:
    - Exactly one of 'chunk_id' OR 'business_text' must be supplied.
    """
    org_id = request.organization_id.strip()
    repo_id = request.repository_id.strip()

    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id parameter cannot be empty.",
        )
    if not repo_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repository_id parameter cannot be empty.",
        )

    cid = (request.chunk_id or "").strip()
    btext = (request.business_text or "").strip()

    if not cid and not btext:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one of 'chunk_id' or 'business_text' must be provided.",
        )
    if cid and btext:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide either 'chunk_id' OR 'business_text', not both.",
        )

    resolved_text = btext
    heading_path = ""

    # G2 fix: Resolve text from docs_chunks if chunk_id is provided
    if cid:
        try:
            # Extract doc_id if scoped chunk ID
            parent_doc_id = cid.rsplit(":chunk:", 1)[0] if ":chunk:" in cid else cid
            res_docs = chroma_client.get_documents(
                collection_name="docs_chunks",
                organization_id=org_id,
                extra_filter={"doc_id": {"$eq": parent_doc_id}},
            )
            c_ids = res_docs.get("ids", [])
            c_texts = res_docs.get("documents", [])
            c_metas = res_docs.get("metadatas") or [{}] * len(c_ids)

            matched_idx = None
            for idx, item_id in enumerate(c_ids):
                if item_id == cid or item_id.endswith(cid):
                    matched_idx = idx
                    break

            if matched_idx is not None:
                resolved_text = c_texts[matched_idx]
                heading_path = c_metas[matched_idx].get("heading_path", "")
            else:
                # Fallback to first chunk text if chunk ID not explicitly matched in ID array
                if c_texts:
                    resolved_text = c_texts[0]
                    heading_path = c_metas[0].get("heading_path", "")
                else:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Chunk ID '{cid}' not found in docs_chunks for tenant '{org_id}'.",
                    )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed resolving chunk_id '%s': %s", cid, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error resolving chunk_id '{cid}': {exc}",
            )

    return map_business_chunk_to_code(
        organization_id=org_id,
        repository_id=repo_id,
        business_text=resolved_text,
        chunk_id=cid,
        heading_path=heading_path,
        use_llm_judge=request.use_llm_judge,
        max_mappings=request.max_mappings,
    )


@router.post("/document", status_code=status.HTTP_202_ACCEPTED)
async def map_business_document(
    request: DocumentMappingRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger asynchronous batch Business-to-Code Mapping for all chunks of a business document.
    Returns HTTP 202 with task_id for status polling.
    """
    org_id = request.organization_id.strip()
    repo_id = request.repository_id.strip()
    doc_id = request.document_id.strip()

    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id parameter cannot be empty.",
        )
    if not repo_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repository_id parameter cannot be empty.",
        )
    if not doc_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="document_id parameter cannot be empty.",
        )

    system_user = ensure_system_user(db)

    task_payload = {
        "organization_id": org_id,
        "repository_id": repo_id,
        "document_id": doc_id,
        "use_llm_judge": request.use_llm_judge,
        "max_mappings_per_chunk": request.max_mappings_per_chunk,
    }

    new_task = Task(
        user_id=system_user.id,
        type="business_mapping_doc",
        status="pending",
        payload=task_payload,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    task_id_str = str(new_task.id)

    background_tasks.add_task(
        run_business_mapping_document_task,
        task_id=task_id_str,
        organization_id=org_id,
        repository_id=repo_id,
        document_id=doc_id,
        use_llm_judge=request.use_llm_judge,
        max_mappings_per_chunk=request.max_mappings_per_chunk,
    )

    return {
        "task_id": task_id_str,
        "status": "pending",
        "message": "Full-document business-to-code mapping task queued successfully.",
    }


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_business_mapping_task_status(
    task_id: str,
    db: Session = Depends(get_db),
):
    """
    Poll the status and result of a background full-document business mapping task.
    """
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid task_id format '{task_id}'. Must be a valid UUID.",
        )

    task = db.query(Task).filter(Task.id == task_uuid).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID '{task_id}' not found.",
        )

    return TaskStatusResponse(
        task_id=str(task.id),
        status=task.status,
        payload=task.payload,
        result=task.result,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
