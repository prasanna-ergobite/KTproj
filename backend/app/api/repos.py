"""
Repository Management & Ingestion API Router.
Provides asynchronous repository ingestion trigger and background task status polling.
"""

from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.postgres_client import get_db
from app.models.db_models import Task
from app.core.constants import SYSTEM_USER_ID, ensure_system_user
from app.graphs.repo_ingestion import run_repo_ingestion_task, validate_repo_url, sanitize_url

router = APIRouter(prefix="/repos", tags=["Repositories"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------

class RepoIngestRequest(BaseModel):
    repo_url: str = Field(..., description="Git repository URL or allowed local path")
    organization_id: str = Field(..., description="Tenant organization identifier")
    branch: str = Field("main", description="Git branch to clone and analyze")


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    payload: dict | None = None
    result: dict | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def ingest_repository(
    request: RepoIngestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger asynchronous repository ingestion pipeline.
    Enqueues git parsing, Neo4j graph construction, and vector store indexing
    as a background task. Returns HTTP 202 with task_id for status polling.
    """
    # Pre-flight input validation
    try:
        validate_repo_url(request.repo_url)
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )

    # Ensure system user sentinel exists for foreign key constraint
    system_user = ensure_system_user(db)

    # Create Task record in Postgres
    sanitized_url = sanitize_url(request.repo_url)
    task_payload = {
        "repo_url": sanitized_url,
        "organization_id": request.organization_id.strip(),
        "branch": request.branch.strip(),
    }

    new_task = Task(
        user_id=system_user.id,
        type="repo_ingestion",
        status="pending",
        payload=task_payload,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    task_id_str = str(new_task.id)

    # Enqueue background task
    background_tasks.add_task(
        run_repo_ingestion_task,
        task_id=task_id_str,
        repo_url=request.repo_url.strip(),
        organization_id=request.organization_id.strip(),
        branch=request.branch.strip(),
    )

    return {
        "task_id": task_id_str,
        "status": "pending",
        "message": "Repository ingestion task queued successfully.",
    }


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    db: Session = Depends(get_db),
):
    """
    Poll the status and output result of a background repository ingestion task.
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
