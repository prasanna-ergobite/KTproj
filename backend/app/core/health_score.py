"""
KT Health Score Engine for AutoKT.

Computes per-module health scores across two dimensions:
  - doc_score: documentation coverage (docstrings, README, linked business docs)
  - ownership_score: bus factor (unique committers, concentration risk)

overall_score = 0.40 * doc_score + 0.60 * ownership_score

Staleness score deferred to Milestone 22.5 pending real git timestamp ingestion fix.
(See repo_ingestion.py:L688 — last_commit_at currently stores ingest datetime, not commit datetime.)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.core.module_context import ModuleContext, ModuleNotFoundError, assemble_module_context
from app.db.neo4j_client import neo4j_client

logger = logging.getLogger("autokt.health_score")

# Weighting: ownership weighted higher because bus-factor-1 is immediate and unmitigable;
# documentation gaps are survivable if multiple people share code knowledge.
DOC_WEIGHT = 0.40
OWNERSHIP_WEIGHT = 0.60

# Gap thresholds
DOC_COVERAGE_WARN_THRESHOLD = 80.0  # % documented functions below which a gap is flagged
BUS_FACTOR_WARN_THRESHOLD = 3       # bus_factor below this triggers gap warnings

# Maximum modules returned in list endpoint (prevents N+1 overload)
MAX_MODULES_PER_REPO = 20


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class HealthScoreDimensions:
    doc_score: float
    ownership_score: float
    bus_factor: int
    sole_owner_risk: bool
    unowned: bool


@dataclass
class ModuleHealthScore:
    module_id: str
    module_name: str
    overall_score: float
    dimensions: HealthScoreDimensions
    gaps: List[str]
    computed_at: str  # ISO 8601


# ---------------------------------------------------------------------------
# Internal Scoring Helpers (accept plain dicts for testability)
# ---------------------------------------------------------------------------

def _compute_doc_score(
    coverage_pct: float,
    has_readme: bool,
    linked_doc_count: int,
) -> float:
    """
    Compute documentation coverage score (0–100).

    coverage_pct + README bonus (10) + linked doc bonus (5 per doc, capped at 15).
    Result capped at 100.0.
    """
    raw = coverage_pct
    raw += 10.0 if has_readme else 0.0
    raw += min(linked_doc_count * 5.0, 15.0)
    return min(raw, 100.0)


def _compute_ownership_dimensions(owners_list: List[Dict[str, Any]]) -> HealthScoreDimensions:
    """
    Compute ownership score and bus factor risk signals.

    bus_factor = count of unique owners with at least 1 commit.
    ownership_score = min(bus_factor / 3.0, 1.0) * 100.
      bus_factor 0 → 0.0 (unowned), 1 → 33.3 (sole owner), 2 → 66.7, 3+ → 100.0
    """
    bus_factor = sum(1 for o in owners_list if int(o.get("total_commits", 0)) > 0)
    ownership_score = round(min(bus_factor / 3.0, 1.0) * 100.0, 1)
    sole_owner_risk = (bus_factor == 1)
    unowned = (bus_factor == 0)

    return HealthScoreDimensions(
        doc_score=0.0,  # filled by caller
        ownership_score=ownership_score,
        bus_factor=bus_factor,
        sole_owner_risk=sole_owner_risk,
        unowned=unowned,
    )


def _compute_overall_score(doc_score: float, ownership_score: float) -> float:
    """overall_score = 0.40 * doc_score + 0.60 * ownership_score, rounded to 1 dp."""
    return round(DOC_WEIGHT * doc_score + OWNERSHIP_WEIGHT * ownership_score, 1)


def _build_gaps(
    doc_score: float,
    dims: HealthScoreDimensions,
    coverage_pct: float,
    total_func_count: int,
    undocumented_count: int,
    has_readme: bool,
    linked_doc_count: int,
) -> List[str]:
    """
    Build a human-readable list of KT risk gap strings.
    All thresholds are explicit and testable.
    """
    gaps: List[str] = []

    if coverage_pct < DOC_COVERAGE_WARN_THRESHOLD and total_func_count > 0:
        gaps.append(
            f"{undocumented_count} of {total_func_count} functions missing docstrings "
            f"({coverage_pct:.1f}% coverage)"
        )

    if not has_readme:
        gaps.append("No README or contributing guide found in this module")

    if linked_doc_count == 0:
        gaps.append("No linked business documents (PRDs, BRDs) found for this module")

    if dims.unowned:
        gaps.append("No code owners found — module has no git authorship data")
    elif dims.sole_owner_risk:
        gaps.append("Bus factor is 1 — only 1 contributor owns this module (high KT risk)")
    elif dims.bus_factor == 2:
        gaps.append("Bus factor is 2 — only 2 contributors own this module")

    return gaps


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_health_score(module_id: str, organization_id: str) -> ModuleHealthScore:
    """
    Compute the KT Health Score for a single module.

    Calls assemble_module_context() then runs scoring functions.

    Raises:
        ModuleNotFoundError: if the module is not in Neo4j.
        Neo4jClientError: on fatal Neo4j query failure.
    """
    ctx: ModuleContext = assemble_module_context(module_id, organization_id)

    doc_score = _compute_doc_score(
        coverage_pct=ctx.coverage_pct,
        has_readme=ctx.has_readme,
        linked_doc_count=len(ctx.existing_docs_list),
    )

    dims = _compute_ownership_dimensions(ctx.owners_list)
    dims.doc_score = doc_score  # fill in after computing

    overall = _compute_overall_score(doc_score, dims.ownership_score)

    gaps = _build_gaps(
        doc_score=doc_score,
        dims=dims,
        coverage_pct=ctx.coverage_pct,
        total_func_count=ctx.total_func_count,
        undocumented_count=ctx.undocumented_count,
        has_readme=ctx.has_readme,
        linked_doc_count=len(ctx.existing_docs_list),
    )

    return ModuleHealthScore(
        module_id=ctx.module_id,
        module_name=ctx.module_name,
        overall_score=overall,
        dimensions=dims,
        gaps=gaps,
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


def list_module_health_scores(repository_id: str, organization_id: str) -> List[ModuleHealthScore]:
    """
    Compute KT Health Scores for all modules in a repository.

    Returns modules sorted by overall_score ascending (worst first).
    Capped at MAX_MODULES_PER_REPO (20) to prevent N+1 overload.

    Raises:
        Neo4jClientError: on fatal Neo4j query failure.
    """
    cypher_modules = (
        "MATCH (m:Module) "
        "WHERE m.repository_id = $repository_id "
        "  AND m.organization_id = $organization_id "
        "RETURN m.id AS module_id "
        "ORDER BY m.name "
        "LIMIT $limit"
    )

    records = neo4j_client.run_read_query(
        cypher_modules,
        parameters={
            "repository_id": repository_id,
            "organization_id": organization_id,
            "limit": MAX_MODULES_PER_REPO,
        },
        organization_id=organization_id,
    )

    results: List[ModuleHealthScore] = []
    for rec in records:
        mod_id = rec.get("module_id")
        if not mod_id:
            continue
        try:
            score = compute_health_score(mod_id, organization_id)
            results.append(score)
        except ModuleNotFoundError:
            logger.warning("Module '%s' not found during health score list — skipping.", mod_id)
        except Exception as exc:
            logger.warning("Error computing health score for module '%s': %s", mod_id, exc)

    results.sort(key=lambda s: s.overall_score)
    return results
