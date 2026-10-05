"""
Module Health Score & Bus Factor API Router.

Endpoints:
  GET /health-score/repo/{repository_id}   — all modules in a repo, sorted worst-first
  GET /health-score/module/{module_id}     — single module health score

Both require ?organization_id= as a required query parameter (tenant isolation).
"""

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, status

from app.core.health_score import (
    ModuleHealthScore,
    compute_health_score,
    list_module_health_scores,
    MAX_MODULES_PER_REPO,
)
from app.core.module_context import ModuleNotFoundError
from app.db.neo4j_client import Neo4jClientError

router = APIRouter(tags=["Health Score"])


def _score_to_dict(score: ModuleHealthScore) -> Dict[str, Any]:
    """Serialize ModuleHealthScore to a plain dict for JSON response."""
    return {
        "module_id": score.module_id,
        "module_name": score.module_name,
        "overall_score": score.overall_score,
        "dimensions": {
            "doc_score": score.dimensions.doc_score,
            "ownership_score": score.dimensions.ownership_score,
            "bus_factor": score.dimensions.bus_factor,
            "sole_owner_risk": score.dimensions.sole_owner_risk,
            "unowned": score.dimensions.unowned,
            "staleness_note": (
                "not_available — last_commit_at reflects ingest timestamp, not git history "
                "(fix scheduled for Milestone 22.5)"
            ),
        },
        "gaps": score.gaps,
        "computed_at": score.computed_at,
    }


# ---------------------------------------------------------------------------
# /health-score/repo/{repository_id}  — declared FIRST to avoid route collision
# ---------------------------------------------------------------------------

@router.get("/health-score/repo/{repository_id}")
async def list_repo_health_scores(
    repository_id: str,
    organization_id: str = Query(..., min_length=1, description="Tenant organization identifier"),
):
    """
    Retrieve KT Health Scores for all modules in a repository.

    Returns modules sorted by overall_score ascending (worst-first).
    Capped at {MAX_MODULES_PER_REPO} modules per request.
    """
    try:
        scores = list_module_health_scores(repository_id, organization_id)
    except Neo4jClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Graph database unavailable: {exc}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error computing health scores: {exc}",
        )

    return {
        "repository_id": repository_id,
        "organization_id": organization_id,
        "total_modules": len(scores),
        "max_modules_per_request": MAX_MODULES_PER_REPO,
        "modules": [_score_to_dict(s) for s in scores],
    }


# ---------------------------------------------------------------------------
# /health-score/module/{module_id}  — declared SECOND ({module_id:path} captures slashes)
# ---------------------------------------------------------------------------

@router.get("/health-score/module/{module_id:path}")
async def get_module_health_score(
    module_id: str,
    organization_id: str = Query(..., min_length=1, description="Tenant organization identifier"),
):
    """
    Retrieve the KT Health Score for a single module.

    module_id may contain '/' and ':' characters (e.g., repo:chatdoc/backend:module:app).
    """
    try:
        score = compute_health_score(module_id, organization_id)
    except ModuleNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Module '{module_id}' not found in organization '{organization_id}'.",
        )
    except Neo4jClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Graph database unavailable: {exc}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error computing health score: {exc}",
        )

    return _score_to_dict(score)

