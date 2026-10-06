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
from app.db.neo4j_client import neo4j_client
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


class RepositorySummary(BaseModel):
    id: str = Field(..., description="Unique repository identifier, e.g. repo:simple-rag")
    name: str = Field(..., description="Repository name")
    organization_id: str = Field(..., description="Tenant organization identifier")
    url: str = Field(..., description="Repository source URL or clone path")
    default_branch: str = Field("main", description="Default branch name")
    modules_count: int = Field(0, description="Number of modules identified in this repository")
    files_count: int = Field(0, description="Number of source files ingested")
    docs_count: int = Field(0, description="Number of documents associated with this repository")


class RepositoryListResponse(BaseModel):
    repositories: list[RepositorySummary]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=RepositoryListResponse)
async def list_repositories(
    organization_id: str | None = None,
):
    """
    List all ingested repositories from the knowledge graph.
    Optionally filters by organization_id if provided.
    """
    if neo4j_client.driver is None:
        try:
            neo4j_client.connect()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Neo4j database connection unavailable: {exc}",
            )

    try:
        with neo4j_client.driver.session() as session:
            if organization_id and organization_id.strip():
                query = """
                MATCH (r:Repository {organization_id: $organization_id})
                OPTIONAL MATCH (m:Module)-[:PART_OF]->(r)
                OPTIONAL MATCH (f:File)-[:PART_OF]->(m)
                OPTIONAL MATCH (d:Document)-[:DOCUMENTS]->(m)
                OPTIONAL MATCH (dr:Document)-[:DOCUMENTS]->(r)
                RETURN r.id AS id, r.name AS name, r.organization_id AS organization_id, 
                       r.url AS url, coalesce(r.default_branch, 'main') AS default_branch,
                       count(DISTINCT m) AS modules_count,
                       count(DISTINCT f) AS files_count,
                       count(DISTINCT d) + count(DISTINCT dr) AS docs_count
                ORDER BY r.name ASC
                """
                params = {"organization_id": organization_id.strip()}
            else:
                query = """
                MATCH (r:Repository)
                OPTIONAL MATCH (m:Module)-[:PART_OF]->(r)
                OPTIONAL MATCH (f:File)-[:PART_OF]->(m)
                OPTIONAL MATCH (d:Document)-[:DOCUMENTS]->(m)
                OPTIONAL MATCH (dr:Document)-[:DOCUMENTS]->(r)
                RETURN r.id AS id, r.name AS name, r.organization_id AS organization_id, 
                       r.url AS url, coalesce(r.default_branch, 'main') AS default_branch,
                       count(DISTINCT m) AS modules_count,
                       count(DISTINCT f) AS files_count,
                       count(DISTINCT d) + count(DISTINCT dr) AS docs_count
                ORDER BY r.name ASC
                """
                params = {}

            result = session.run(query, params)
            records = [dict(row) for row in result]

        repos_list = [
            RepositorySummary(
                id=rec.get("id") or "",
                name=rec.get("name") or "",
                organization_id=rec.get("organization_id") or "",
                url=rec.get("url") or "",
                default_branch=rec.get("default_branch") or "main",
                modules_count=rec.get("modules_count") or 0,
                files_count=rec.get("files_count") or 0,
                docs_count=rec.get("docs_count") or 0,
            )
            for rec in records
            if rec.get("id")
        ]

        return RepositoryListResponse(
            repositories=repos_list,
            total=len(repos_list),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to query repositories from knowledge graph: {exc}",
        )


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
