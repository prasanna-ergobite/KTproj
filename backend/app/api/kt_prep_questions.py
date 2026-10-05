"""
KT Prep Questions API Router.
Provides asynchronous KT Prep Questions trigger and background task status polling.
"""

from uuid import UUID
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.postgres_client import get_db
from app.models.db_models import Task
from app.core.constants import ensure_system_user
from app.graphs.kt_prep_questions import run_kt_prep_questions_task

router = APIRouter(prefix="/kt-prep-questions", tags=["KT Prep Questions"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------

class KTPrepQuestionsRequest(BaseModel):
    module_id: str = Field(..., description="Target module identifier in Neo4j graph")
    organization_id: str = Field(..., description="Tenant organization identifier")


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    payload: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def generate_kt_prep_questions(
    request: KTPrepQuestionsRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger asynchronous KT Prep Questions generation pipeline.
    Enqueues Neo4j topology queries, Chroma metadata enrichment, gap signal extraction,
    and LLM question synthesis as a background task. Returns HTTP 202 with task_id for status polling.
    """
    if not request.module_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="module_id parameter cannot be empty.",
        )
    if not request.organization_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id parameter cannot be empty.",
        )

    # Ensure system user sentinel exists for foreign key constraint
    system_user = ensure_system_user(db)

    task_payload = {
        "module_id": request.module_id.strip(),
        "organization_id": request.organization_id.strip(),
    }

    new_task = Task(
        user_id=system_user.id,
        type="kt_prep_questions",
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
        run_kt_prep_questions_task,
        task_id=task_id_str,
        module_id=request.module_id.strip(),
        organization_id=request.organization_id.strip(),
    )

    return {
        "task_id": task_id_str,
        "status": "pending",
        "message": "KT Prep Questions generation task queued successfully.",
    }


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_kt_prep_questions_task_status(
    task_id: str,
    db: Session = Depends(get_db),
):
    """
    Poll the status and result of a background KT Prep Questions generation task.
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
