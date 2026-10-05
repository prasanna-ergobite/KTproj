"""
Business Document Upload & Ingestion API Router.
Provides asynchronous business document (PRD, BRD, requirements, overviews) ingestion
and background task status polling.
"""

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
from app.graphs.business_doc_ingestion import run_business_doc_ingestion_task

router = APIRouter(prefix="/business-docs", tags=["Business Documents"])

ALLOWED_DOC_EXTENSIONS = {".md", ".txt", ".rst", ".pdf", ".docx"}
MAX_DOC_FILE_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_DOC_FILES_PER_REQUEST = 50


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_business_documents(
    files: List[UploadFile] = File(...),
    organization_id: str = Form(...),
    repository_id: str = Form(...),
    doc_type: str = Form("business"),
    module_id: str = Form(""),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    """
    Upload and queue business/product documents (PRDs, BRDs, Specs, Overviews).
    Enqueues parsing, chunking, Neo4j Repository/Module linking, and ChromaDB indexing.
    Returns HTTP 202 with task_id for status polling.
    """
    clean_org_id = organization_id.strip()
    if not clean_org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id parameter cannot be empty.",
        )

    clean_repo_id = repository_id.strip()
    if not clean_repo_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repository_id parameter cannot be empty.",
        )

    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one file must be provided.",
        )

    if len(files) > MAX_DOC_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_DOC_FILES_PER_REQUEST} files permitted per upload request.",
        )

    filenames: List[str] = []
    for file_obj in files:
        fn = Path(file_obj.filename or "").name
        if not fn:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File missing valid filename.",
            )
        ext = Path(fn).suffix.lower()
        if ext not in ALLOWED_DOC_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported file extension '{ext}' for file '{fn}'. Allowed: {sorted(list(ALLOWED_DOC_EXTENSIONS))}",
            )
        if file_obj.size and file_obj.size > MAX_DOC_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{fn}' exceeds maximum allowed size of 10 MB.",
            )
        filenames.append(fn)

    system_user = ensure_system_user(db)

    # Create task record in Postgres
    task_payload = {
        "organization_id": clean_org_id,
        "repository_id": clean_repo_id,
        "doc_type": doc_type.strip() or "business",
        "module_id": module_id.strip(),
        "files_count": len(files),
        "filenames": filenames,
    }

    new_task = Task(
        user_id=system_user.id,
        type="business_doc_ingestion",
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
    temp_dir = Path("tmp_business_docs") / clean_org_id / task_id_str
    temp_dir.mkdir(parents=True, exist_ok=True)

    abs_temp_paths: List[str] = []
    for file_obj, fn in zip(files, filenames):
        dest_path = temp_dir / fn
        with open(dest_path, "wb") as out_file:
            content = await file_obj.read()
            if len(content) > MAX_DOC_FILE_SIZE_BYTES:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File '{fn}' exceeds maximum allowed size of 10 MB.",
                )
            out_file.write(content)
        abs_temp_paths.append(str(dest_path.resolve()))

    # Enqueue background task
    background_tasks.add_task(
        run_business_doc_ingestion_task,
        task_id=task_id_str,
        file_paths=abs_temp_paths,
        filenames=filenames,
        organization_id=clean_org_id,
        repository_id=clean_repo_id,
        doc_type=doc_type.strip() or "business",
        module_id=module_id.strip(),
    )

    return {
        "task_id": task_id_str,
        "status": "pending",
        "message": f"{len(files)} business document(s) queued for ingestion.",
    }


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_business_doc_task_status(
    task_id: str,
    db: Session = Depends(get_db),
):
    """
    Poll the status and output result of a background business document ingestion task.
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
