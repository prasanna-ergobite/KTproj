"""Lifecycle recovery and checkout cleanup for reverse-engineering runs."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger("autokt.reverse_engineering.lifecycle")
TASK_TYPE = "reverse_engineering"


def interrupted_result(existing: dict[str, Any] | None, detected_at: datetime) -> dict[str, Any]:
    """Preserve available progress while producing an explicit terminal interruption result."""
    return {
        **(existing or {}),
        "stage": "interrupted",
        "error": "The backend restarted before this analysis completed. Start a new analysis run.",
        "interrupted_at": detected_at.astimezone(timezone.utc).isoformat(),
    }


def cleanup_run_checkout(work_root: Path, task_id: str) -> bool:
    """Remove one UUID-named run checkout after validating its resolved boundary."""
    normalized_id = str(UUID(task_id))
    root = work_root.resolve()
    target = (root / normalized_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Run checkout escapes the configured work root.") from exc
    if target == root:
        raise ValueError("Refusing to remove the reverse-engineering work root.")
    if not target.exists():
        return False
    if target.is_symlink():
        raise ValueError("Refusing to recursively remove a symbolic run checkout.")
    shutil.rmtree(target)
    return True


def recover_stale_reverse_engineering_tasks(
    work_root: Path,
    *,
    stale_after_minutes: int,
    cleanup_checkouts: bool = True,
    now: datetime | None = None,
) -> int:
    """Mark stale active tasks interrupted and remove their abandoned checkouts."""
    from app.db.postgres_client import SessionLocal
    from app.models.db_models import Task

    if stale_after_minutes < 0:
        raise ValueError("stale_after_minutes cannot be negative.")
    detected_at = now or datetime.now(timezone.utc)
    cutoff = detected_at - timedelta(minutes=stale_after_minutes)
    with SessionLocal() as db:
        tasks = (
            db.query(Task)
            .filter(
                Task.type == TASK_TYPE,
                Task.status.in_(("pending", "running")),
                Task.updated_at < cutoff,
            )
            .all()
        )
        task_ids = [str(task.id) for task in tasks]
        for task in tasks:
            task.status = "failed"
            task.result = interrupted_result(task.result, detected_at)
            task.updated_at = detected_at
        db.commit()

    if cleanup_checkouts:
        for task_id in task_ids:
            try:
                cleanup_run_checkout(work_root, task_id)
            except (OSError, ValueError):
                logger.exception("Failed to clean stale reverse-engineering checkout for task %s", task_id)
    return len(task_ids)
