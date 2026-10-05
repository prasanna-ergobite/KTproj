"""
Developer Onboarding Pack Generation API Router.
Provides asynchronous onboarding pack generation trigger and status polling.
"""

from uuid import UUID
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, status, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.postgres_client import get_db
from app.models.db_models import Task
from app.core.constants import ensure_system_user
from app.graphs.onboarding_pack import run_onboarding_pack_task

router = APIRouter(prefix="/onboarding-pack", tags=["Onboarding Pack"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------

class OnboardingPackRequest(BaseModel):
    module_id: str = Field(..., description="Target module identifier in Neo4j graph")
    organization_id: str = Field(..., description="Tenant organization identifier")
    include_markdown: bool = Field(True, description="Whether to include rendered Markdown string in result")


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
async def generate_onboarding_pack(
    request: OnboardingPackRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger asynchronous developer onboarding pack generation pipeline.
    Enqueues Neo4j topology queries, Chroma metadata enrichment, and LLM prose synthesis
    as a background task. Returns HTTP 202 with task_id for status polling.
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
        "include_markdown": request.include_markdown,
    }

    new_task = Task(
        user_id=system_user.id,
        type="onboarding_pack",
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
        run_onboarding_pack_task,
        task_id=task_id_str,
        module_id=request.module_id.strip(),
        organization_id=request.organization_id.strip(),
        include_markdown=request.include_markdown,
    )

    return {
        "task_id": task_id_str,
        "status": "pending",
        "message": "Onboarding pack generation task queued successfully.",
    }


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_onboarding_pack_task_status(
    task_id: str,
    db: Session = Depends(get_db),
):
    """
    Poll the status and result of a background onboarding pack generation task.
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
