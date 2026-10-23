"""Reverse-engineering repository intake API."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import ensure_system_user
from app.db.postgres_client import get_db
from app.models.db_models import Task
from app.reverse_engineering.pipeline import run_reverse_engineering_task
from app.reverse_engineering.repository import sanitize_repository_url, validate_git_ref, validate_remote_repository_url

router = APIRouter(prefix="/reverse-engineering", tags=["Reverse Engineering"])
TASK_TYPE = "reverse_engineering"


class ReverseEngineeringRequest(BaseModel):
    repo_url: str = Field(..., min_length=1, max_length=2048)
    organization_id: str = Field(..., min_length=1, max_length=200)
    ref: str | None = Field(default=None, max_length=255, description="Optional branch, tag, or commit ref")


class ReverseEngineeringTaskResponse(BaseModel):
    task_id: str
    status: str
    payload: dict | None = None
    result: dict | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message: str | None = None


class DocumentListResponse(BaseModel):
    task_id: str
    documents: list[dict]


class DocumentPreviewResponse(BaseModel):
    task_id: str
    document: dict
    markdown: str


class VerificationResponse(BaseModel):
    task_id: str
    report: dict
    findings: list[dict]


class AcceptanceResponse(BaseModel):
    task_id: str
    report: dict


class RecentRunSummary(BaseModel):
    task_id: str
    status: str
    repo_url: str | None = None
    repository_name: str | None = None
    requested_ref: str | None = None
    commit_sha: str | None = None
    verification_status: str | None = None
    acceptance_status: str | None = None
    documents_completed: int = 0
    documents_total: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RecentRunsResponse(BaseModel):
    organization_id: str
    runs: list[RecentRunSummary]


def _owned_task(task_id: str, organization_id: str, db: Session) -> Task:
    try:
        task_uuid = UUID(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="task_id must be a valid UUID.") from exc
    task = db.query(Task).filter(Task.id == task_uuid, Task.type == TASK_TYPE).first()
    if not task or (task.payload or {}).get("organization_id") != organization_id.strip():
        raise HTTPException(status_code=404, detail="Reverse-engineering task not found.")
    return task


def _document_artifact(task: Task, document_id: str) -> tuple[dict, Path]:
    document = next((item for item in (task.result or {}).get("documents", []) if item.get("id") == document_id), None)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found for this analysis run.")
    if document.get("generation_status") != "completed" or not document.get("artifact_ref"):
        raise HTTPException(status_code=409, detail="Document generation did not complete.")
    run_root = (Path(settings.REVERSE_ENGINEERING_ARTIFACT_ROOT).resolve() / str(task.id)).resolve()
    artifact = (run_root / document["artifact_ref"]).resolve()
    try:
        artifact.relative_to(run_root)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid document artifact reference.") from exc
    if not artifact.is_file():
        raise HTTPException(status_code=404, detail="Document artifact is missing.")
    return document, artifact


def _run_artifact(task: Task, relative_path: str, *, max_bytes: int = 10 * 1024 * 1024) -> Path:
    run_root = (Path(settings.REVERSE_ENGINEERING_ARTIFACT_ROOT).resolve() / str(task.id)).resolve()
    artifact = (run_root / relative_path).resolve()
    try:
        artifact.relative_to(run_root)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid analysis artifact reference.") from exc
    if not artifact.is_file():
        raise HTTPException(status_code=404, detail="Analysis artifact is missing.")
    if artifact.stat().st_size > max_bytes:
        raise HTTPException(status_code=413, detail="Analysis artifact is too large to return.")
    return artifact


@router.post("", response_model=ReverseEngineeringTaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_reverse_engineering(
    request: ReverseEngineeringRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    organization_id = request.organization_id.strip()
    if not organization_id:
        raise HTTPException(status_code=400, detail="organization_id cannot be blank.")
    allowed_hosts = settings.REVERSE_ENGINEERING_ALLOWED_GIT_HOSTS.split(",")
    try:
        repo_url = validate_remote_repository_url(request.repo_url, allowed_hosts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        ref = validate_git_ref(request.ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    system_user = ensure_system_user(db)
    now = datetime.now(timezone.utc)
    payload = {
        "repo_url": sanitize_repository_url(repo_url),
        "organization_id": organization_id,
        "ref": ref,
    }
    task = Task(user_id=system_user.id, type=TASK_TYPE, status="pending", payload=payload, created_at=now, updated_at=now)
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = str(task.id)
    background_tasks.add_task(run_reverse_engineering_task, task_id, repo_url, organization_id, ref)
    return ReverseEngineeringTaskResponse(
        task_id=task_id,
        status="pending",
        payload=payload,
        created_at=task.created_at,
        updated_at=task.updated_at,
        message="Repository analysis queued.",
    )


@router.get("/tasks/{task_id}", response_model=ReverseEngineeringTaskResponse)
async def get_reverse_engineering_task(
    task_id: str,
    organization_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    task = _owned_task(task_id, organization_id, db)
    return ReverseEngineeringTaskResponse(
        task_id=str(task.id), status=task.status, payload=task.payload, result=task.result,
        created_at=task.created_at, updated_at=task.updated_at,
    )


@router.get("/tasks", response_model=RecentRunsResponse)
async def list_reverse_engineering_runs(
    organization_id: str = Query(..., min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    organization_id = organization_id.strip()
    if not organization_id:
        raise HTTPException(status_code=400, detail="organization_id cannot be blank.")
    tasks = (
        db.query(Task)
        .filter(Task.type == TASK_TYPE, Task.payload["organization_id"].astext == organization_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
        .all()
    )
    runs = []
    for task in tasks:
        payload = task.payload or {}
        result = task.result or {}
        documents = result.get("documents", [])
        runs.append(RecentRunSummary(
            task_id=str(task.id),
            status=task.status,
            repo_url=result.get("repo_url") or payload.get("repo_url"),
            repository_name=result.get("repository_name"),
            requested_ref=result.get("requested_ref") or payload.get("ref"),
            commit_sha=result.get("commit_sha"),
            verification_status=(result.get("verification") or {}).get("status"),
            acceptance_status=(result.get("acceptance") or {}).get("status"),
            documents_completed=sum(item.get("generation_status") == "completed" for item in documents),
            documents_total=len(documents),
            created_at=task.created_at,
            updated_at=task.updated_at,
        ))
    return RecentRunsResponse(organization_id=organization_id, runs=runs)


@router.get("/tasks/{task_id}/documents", response_model=DocumentListResponse)
async def list_reverse_engineering_documents(
    task_id: str,
    organization_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    task = _owned_task(task_id, organization_id, db)
    return DocumentListResponse(task_id=str(task.id), documents=(task.result or {}).get("documents", []))


@router.get("/tasks/{task_id}/verification", response_model=VerificationResponse)
async def get_reverse_engineering_verification(
    task_id: str,
    organization_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    task = _owned_task(task_id, organization_id, db)
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="Verification has not completed.")
    report_path = _run_artifact(task, "verification/report.json")
    findings_path = _run_artifact(task, "verification/findings.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    findings = json.loads(findings_path.read_text(encoding="utf-8")).get("findings", [])
    return VerificationResponse(task_id=str(task.id), report=report, findings=findings)


@router.get("/tasks/{task_id}/acceptance", response_model=AcceptanceResponse)
async def get_reverse_engineering_acceptance(
    task_id: str,
    organization_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    task = _owned_task(task_id, organization_id, db)
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="Acceptance evaluation has not completed.")
    report_path = _run_artifact(task, "acceptance/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return AcceptanceResponse(task_id=str(task.id), report=report)


@router.get("/tasks/{task_id}/documents/{document_id}", response_model=DocumentPreviewResponse)
async def preview_reverse_engineering_document(
    task_id: str,
    document_id: str,
    organization_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    task = _owned_task(task_id, organization_id, db)
    document, artifact = _document_artifact(task, document_id)
    if artifact.stat().st_size > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Document is too large to preview.")
    return DocumentPreviewResponse(task_id=str(task.id), document=document, markdown=artifact.read_text(encoding="utf-8"))


@router.get("/tasks/{task_id}/documents/{document_id}/download", response_class=FileResponse)
async def download_reverse_engineering_document(
    task_id: str,
    document_id: str,
    organization_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    task = _owned_task(task_id, organization_id, db)
    document, artifact = _document_artifact(task, document_id)
    return FileResponse(
        path=artifact,
        media_type="text/markdown; charset=utf-8",
        filename=Path(document["artifact_ref"]).name,
    )


@router.get("/tasks/{task_id}/download", response_class=FileResponse)
async def download_reverse_engineering_bundle(
    task_id: str,
    organization_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    task = _owned_task(task_id, organization_id, db)
    bundle = (task.result or {}).get("bundle") or {}
    artifact_ref = bundle.get("artifact_ref")
    if task.status != "completed" or not artifact_ref:
        raise HTTPException(status_code=409, detail="Documentation bundle is not available.")
    artifact = _run_artifact(task, artifact_ref, max_bytes=settings.REVERSE_ENGINEERING_MAX_BUNDLE_BYTES)
    headers = {"X-Content-SHA256": bundle["sha256"]} if bundle.get("sha256") else None
    return FileResponse(
        path=artifact,
        media_type="application/zip",
        filename=bundle.get("filename") or "reverse-engineering-documentation.zip",
        headers=headers,
    )

