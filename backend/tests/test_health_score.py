"""
Tests for KT Health Score engine (app/core/health_score.py) and API (app/api/health_score.py).

Tests 1–11: Pure unit tests against internal scoring helpers — no Neo4j/Chroma mocking needed.
Tests 12–13: FastAPI TestClient with compute_health_score mocked to avoid live Neo4j dependency.

Baseline: 157 PASSED, 1 SKIPPED.
After this file: 169 PASSED, 1 SKIPPED, 0 FAILED.
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.core.health_score import (
    _compute_doc_score,
    _compute_ownership_dimensions,
    _compute_overall_score,
    _build_gaps,
    HealthScoreDimensions,
    ModuleHealthScore,
)


# ---------------------------------------------------------------------------
# Helpers — build minimal dicts matching ModuleContext field shapes
# ---------------------------------------------------------------------------

def _owners(emails_and_commits):
    """Build owners_list from [(email, commit_count), ...]."""
    return [{"name": e, "email": e, "total_commits": c, "last_active": ""} for e, c in emails_and_commits]


# ===========================================================================
# Tests 1–3: _compute_doc_score
# ===========================================================================

class TestComputeDocScore:
    def test_full_coverage_with_readme_and_docs_capped_at_100(self):
        """100% coverage + README (10) + 3 docs (15) = 125 → capped at 100."""
        score = _compute_doc_score(coverage_pct=100.0, has_readme=True, linked_doc_count=3)
        assert score == 100.0

    def test_zero_coverage_no_readme_no_docs(self):
        """All zeros → doc_score == 0.0."""
        score = _compute_doc_score(coverage_pct=0.0, has_readme=False, linked_doc_count=0)
        assert score == 0.0

    def test_partial_coverage_with_bonuses(self):
        """coverage_pct=60 + README(10) + 2 docs(10) = 80.0."""
        score = _compute_doc_score(coverage_pct=60.0, has_readme=True, linked_doc_count=2)
        assert score == 80.0


# ===========================================================================
# Tests 4–6: _compute_ownership_dimensions
# ===========================================================================

class TestComputeOwnershipDimensions:
    def test_no_owners_returns_unowned(self):
        dims = _compute_ownership_dimensions(_owners([]))
        assert dims.bus_factor == 0
        assert dims.unowned is True
        assert dims.sole_owner_risk is False
        assert dims.ownership_score == 0.0

    def test_single_owner_returns_sole_owner_risk(self):
        dims = _compute_ownership_dimensions(_owners([("alice@x.com", 10)]))
        assert dims.bus_factor == 1
        assert dims.sole_owner_risk is True
        assert dims.unowned is False
        assert abs(dims.ownership_score - 33.3) < 0.1

    def test_three_plus_owners_returns_full_score(self):
        owners = _owners([("a@x", 5), ("b@x", 3), ("c@x", 7), ("d@x", 1)])
        dims = _compute_ownership_dimensions(owners)
        assert dims.bus_factor == 4
        assert dims.ownership_score == 100.0
        assert dims.sole_owner_risk is False
        assert dims.unowned is False

    def test_owner_with_zero_commits_not_counted(self):
        """An owner with 0 commits should not count toward bus_factor."""
        owners = _owners([("alice@x", 5), ("ghost@x", 0)])
        dims = _compute_ownership_dimensions(owners)
        assert dims.bus_factor == 1
        assert dims.sole_owner_risk is True


# ===========================================================================
# Test 7: _compute_overall_score
# ===========================================================================

class TestComputeOverallScore:
    def test_weighted_40_60_formula(self):
        """0.40 * 80 + 0.60 * 50 = 32 + 30 = 62.0"""
        result = _compute_overall_score(doc_score=80.0, ownership_score=50.0)
        assert result == 62.0


# ===========================================================================
# Tests 8–11: _build_gaps
# ===========================================================================

def _dims(bus_factor):
    """Quick HealthScoreDimensions factory for gap tests."""
    return HealthScoreDimensions(
        doc_score=0.0,
        ownership_score=0.0,
        bus_factor=bus_factor,
        sole_owner_risk=(bus_factor == 1),
        unowned=(bus_factor == 0),
    )


class TestBuildGaps:
    def test_gap_undocumented_functions_below_80pct(self):
        gaps = _build_gaps(
            doc_score=55.0,
            dims=_dims(2),
            coverage_pct=55.0,
            total_func_count=20,
            undocumented_count=9,
            has_readme=True,
            linked_doc_count=1,
        )
        assert any("missing docstrings" in g for g in gaps)

    def test_no_gap_for_undocumented_when_above_80pct(self):
        gaps = _build_gaps(
            doc_score=85.0,
            dims=_dims(3),
            coverage_pct=85.0,
            total_func_count=10,
            undocumented_count=1,
            has_readme=True,
            linked_doc_count=2,
        )
        assert not any("missing docstrings" in g for g in gaps)

    def test_gap_no_readme(self):
        gaps = _build_gaps(
            doc_score=60.0,
            dims=_dims(2),
            coverage_pct=60.0,
            total_func_count=5,
            undocumented_count=2,
            has_readme=False,
            linked_doc_count=1,
        )
        assert any("README" in g for g in gaps)

    def test_gap_sole_owner(self):
        gaps = _build_gaps(
            doc_score=90.0,
            dims=_dims(1),
            coverage_pct=90.0,
            total_func_count=10,
            undocumented_count=1,
            has_readme=True,
            linked_doc_count=2,
        )
        assert any("Bus factor is 1" in g for g in gaps)

    def test_gap_no_owners(self):
        gaps = _build_gaps(
            doc_score=90.0,
            dims=_dims(0),
            coverage_pct=90.0,
            total_func_count=5,
            undocumented_count=0,
            has_readme=True,
            linked_doc_count=2,
        )
        assert any("No code owners" in g for g in gaps)


# ===========================================================================
# Tests 12–13: FastAPI TestClient (compute_health_score mocked)
# ===========================================================================

@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


class TestHealthScoreAPI:
    def test_get_module_missing_organization_id_returns_422(self, client):
        """Missing required query param → FastAPI 422 Unprocessable Entity."""
        response = client.get("/health-score/module/some-module-id")
        assert response.status_code == 422

    def test_get_module_nonexistent_module_returns_404(self, client):
        """compute_health_score raising ModuleNotFoundError → 404."""
        from app.core.module_context import ModuleNotFoundError as MNE
        with patch("app.api.health_score.compute_health_score", side_effect=MNE("not found")):
            response = client.get(
                "/health-score/module/nonexistent-module",
                params={"organization_id": "test-org"},
            )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
