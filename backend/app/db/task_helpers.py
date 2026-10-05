"""
Shared Postgres task status update helpers.
Used by background pipeline task runners (onboarding_pack, kt_prep_questions, etc.).
"""

import logging
from uuid import UUID
from datetime import datetime, timezone
from typing import Dict, Any

from app.db.postgres_client import SessionLocal
from app.models.db_models import Task

logger = logging.getLogger("autokt.task_helpers")


def update_task_completed(task_id: str, result_data: Dict[str, Any]) -> None:
    """Updates task status to 'completed' and persists result in Postgres."""
    try:
        with SessionLocal() as db:
            task = db.query(Task).filter(Task.id == UUID(task_id)).first()
            if task:
                task.status = "completed"
                task.result = result_data
                task.updated_at = datetime.now(timezone.utc)
                db.commit()
    except Exception as exc:
        logger.error("Failed to update task '%s' to completed: %s", task_id, exc)


def update_task_failed(task_id: str, error_detail: str) -> None:
    """Updates task status to 'failed' and persists error detail in Postgres."""
    try:
        with SessionLocal() as db:
            task = db.query(Task).filter(Task.id == UUID(task_id)).first()
            if task:
                task.status = "failed"
                task.result = {"error": error_detail}
                task.updated_at = datetime.now(timezone.utc)
                db.commit()
    except Exception as exc:
        logger.error("Failed to update task '%s' to failed: %s", task_id, exc)
