from copy import deepcopy

from app.reverse_engineering.acceptance import acceptance_failure_report, build_acceptance_report
from app.reverse_engineering.documents import DOCUMENT_SPECS


def _inputs():
    return {
        "commit_sha": "a" * 40,
        "inventory": {"included_files": 8, "included_bytes": 4096, "excluded_files": 2},
        "structural_analysis": {
            "coverage": {
                "status": "complete",
                "coverage_percent": 100.0,
                "eligible_files": 8,
                "parsed_files": 8,
                "unsupported_files": 0,
                "failed_files": 0,
            }
        },
        "documents": [
            {"id": document_id, "generation_status": "completed"}
            for document_id in DOCUMENT_SPECS
        ],
        "claims": {"claims": [{"verdict": "supported"}, {"verdict": "supported"}]},
        "findings": {"findings": []},
        "verification": {
            "status": "checks_passed",
            "correction_rounds": 1,
            "contract_checks": {
                "api_endpoints": {"status": "complete"},
                "models": {"status": "complete"},
            },
            "limitations": ["Source-only verification does not prove runtime behavior."],
        },
        "generation": {"method": "deterministic", "provider": "disabled", "model": None, "warnings": []},
        "stage_durations_ms": {"clone": 120.5, "inventory": 10.25},
    }


def test_acceptance_report_marks_complete_run_ready():
    report = build_acceptance_report(**_inputs())

    assert report["status"] == "ready"
    assert report["summary"] == {
        "total_checks": 10,
        "passed": 10,
        "review": 0,
        "failed": 0,
        "score_percent": 100.0,
    }
    assert report["measured_stage_durations_ms"] == {"clone": 120.5, "inventory": 10.25}
    assert report["recommendations"] == []


def test_acceptance_report_marks_partial_supported_scope_for_review():
    inputs = deepcopy(_inputs())
    inputs["structural_analysis"]["coverage"].update({
        "status": "partial",
        "coverage_percent": 87.5,
        "parsed_files": 7,
        "unsupported_files": 1,
    })
    inputs["verification"]["status"] = "needs_review"
    inputs["findings"]["findings"] = [{"code": "structural_coverage_partial", "severity": "medium"}]

    report = build_acceptance_report(**inputs)

    assert report["status"] == "needs_review"
    assert report["summary"]["review"] == 2
    assert report["summary"]["failed"] == 0
    assert {item["id"] for item in report["checks"] if item["status"] == "review"} == {
        "structural_coverage", "document_verification",
    }


def test_acceptance_report_blocks_incomplete_or_unsupported_output():
    inputs = deepcopy(_inputs())
    inputs["documents"] = inputs["documents"][:-1]
    inputs["claims"]["claims"][0]["verdict"] = "contradicted"
    inputs["verification"].update({"status": "verification_incomplete", "contract_checks": {}})
    inputs["findings"]["findings"] = [{
        "code": "invalid_source_citation",
        "severity": "high",
    }]

    report = build_acceptance_report(**inputs)

    assert report["status"] == "not_ready"
    failed_ids = {item["id"] for item in report["checks"] if item["status"] == "fail"}
    assert {
        "five_documents",
        "document_verification",
        "claim_grounding",
        "exact_document_contracts",
        "source_citations",
        "blocking_findings",
    }.issubset(failed_ids)
    assert report["recommendations"]


def test_acceptance_failure_report_preserves_measured_progress():
    report = acceptance_failure_report("f" * 40, {"clone": 25.0}, RuntimeError("fixture failure"))

    assert report["status"] == "not_ready"
    assert report["summary"]["failed"] == 1
    assert report["checks"][0]["id"] == "acceptance_execution"
    assert report["measured_stage_durations_ms"] == {"clone": 25.0}
    assert "fixture failure" in report["checks"][0]["message"]
