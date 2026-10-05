"""
Document Upload & Ingestion API Router.
Provides asynchronous document ingestion trigger and background task status polling.
"""

import os
import shutil
from pathlib import Path
from uuid import UUID
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, UploadFile, File, Form, status
from sqlalchemy.orm import Session

from app.db.postgres_client import get_db
from app.models.db_models import Task
from app.core.constants import ensure_system_user
from app.api.repos import TaskStatusResponse
from app.graphs.doc_ingestion import run_doc_ingestion_task

router = APIRouter(prefix="/docs", tags=["Documents"])

ALLOWED_DOC_EXTENSIONS = {".md", ".txt", ".rst", ".pdf", ".docx"}
MAX_DOC_FILE_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_DOC_FILES_PER_REQUEST = 50


def _sanitize_relative_path(raw: str) -> str:
    """Strip leading separators; normalize to forward slashes; reject path traversal."""
    if not raw or not raw.strip():
        raise ValueError("Relative path cannot be empty.")
    p = Path(raw.replace("\\", "/")).as_posix().lstrip("/")
    if ".." in p.split("/"):
        raise ValueError(f"Path traversal rejected: '{raw}'")
    return p


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_documents(
    files: List[UploadFile] = File(...),
    relative_paths: List[str] = Form(...),
    organization_id: str = Form(...),
    source_type: str = Form("doc"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    """
    Upload and queue documentation files (Markdown, PDF, DOCX, TXT, RST).
    Enqueues parsing, chunking, graph linking, and vector store indexing as a background task.
    Returns HTTP 202 with task_id for polling.
    """
    clean_org_id = organization_id.strip()
    if not clean_org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id parameter cannot be empty.",
        )

    if len(files) != len(relative_paths):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Mismatched files ({len(files)}) and relative_paths ({len(relative_paths)}) count.",
        )

    if len(files) > MAX_DOC_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_DOC_FILES_PER_REQUEST} files permitted per upload request.",
        )

    sanitized_rel_paths: List[str] = []
    for raw_rel_path in relative_paths:
        try:
            sanitized = _sanitize_relative_path(raw_rel_path)
            sanitized_rel_paths.append(sanitized)
        except ValueError as val_err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(val_err),
            )

    # Validate file extension and size before reading
    for file_obj in files:
        ext = Path(file_obj.filename or "").suffix.lower()
        if ext not in ALLOWED_DOC_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported file extension '{ext}' for file '{file_obj.filename}'. Allowed: {sorted(list(ALLOWED_DOC_EXTENSIONS))}",
            )
        
        # Check size if available
        if file_obj.size and file_obj.size > MAX_DOC_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{file_obj.filename}' exceeds maximum allowed size of 10 MB.",
            )

    system_user = ensure_system_user(db)

    # Create task record in Postgres
    task_payload = {
        "organization_id": clean_org_id,
        "source_type": source_type.strip(),
        "files_count": len(files),
        "relative_paths": sanitized_rel_paths,
    }

    new_task = Task(
        user_id=system_user.id,
        type="doc_ingestion",
        status="pending",
        payload=task_payload,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    task_id_str = str(new_task.id)

    # Save uploaded files to temporary storage
    temp_dir = Path("tmp_docs") / clean_org_id / task_id_str
    temp_dir.mkdir(parents=True, exist_ok=True)

    abs_temp_paths: List[str] = []
    for file_obj, rel_path in zip(files, sanitized_rel_paths):
        dest_path = temp_dir / rel_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as out_file:
            content = await file_obj.read()
            if len(content) > MAX_DOC_FILE_SIZE_BYTES:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File '{file_obj.filename}' exceeds maximum allowed size of 10 MB.",
                )
            out_file.write(content)
        abs_temp_paths.append(str(dest_path.resolve()))

    # Enqueue background task
    background_tasks.add_task(
        run_doc_ingestion_task,
        task_id=task_id_str,
        file_paths=abs_temp_paths,
        relative_paths=sanitized_rel_paths,
        organization_id=clean_org_id,
        source_type=source_type.strip(),
    )

    return {
        "task_id": task_id_str,
        "status": "pending",
        "message": f"{len(files)} document(s) queued for ingestion.",
    }


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_doc_task_status(
    task_id: str,
    db: Session = Depends(get_db),
):
    """
    Poll the status and output result of a background document ingestion task.
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
