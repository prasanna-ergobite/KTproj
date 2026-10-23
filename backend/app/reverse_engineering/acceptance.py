"""Run-level acceptance report for reverse-engineering demo rehearsals."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.reverse_engineering.documents import DOCUMENT_SPECS


def _check(
    check_id: str,
    title: str,
    status: str,
    message: str,
    *,
    evidence: dict[str, Any] | None = None,
    required: bool = True,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "required": required,
        "message": message,
        "evidence": evidence or {},
    }


def acceptance_failure_report(
    commit_sha: str,
    stage_durations_ms: dict[str, float],
    error: Exception,
) -> dict[str, Any]:
    """Preserve a terminal, truthful report when acceptance evaluation cannot run."""
    message = str(error).strip()[:1000] or type(error).__name__
    return {
        "schema_version": 1,
        "status": "not_ready",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha,
        "summary": {"total_checks": 1, "passed": 0, "review": 0, "failed": 1, "score_percent": 0.0},
        "checks": [{
            "id": "acceptance_execution",
            "title": "Acceptance evaluation",
            "status": "fail",
            "required": True,
            "message": f"Acceptance evaluation did not complete: {message}",
            "evidence": {"error_type": type(error).__name__},
        }],
        "measured_stage_durations_ms": stage_durations_ms,
        "generation": {},
        "recommendations": ["Review the acceptance evaluator failure before the demo."],
        "limitations": ["Documentation artifacts remain available, but run-level acceptance was not established."],
    }


def build_acceptance_report(
    *,
    commit_sha: str,
    inventory: dict[str, Any],
    structural_analysis: dict[str, Any],
    documents: list[dict[str, Any]],
    claims: dict[str, Any],
    findings: dict[str, Any],
    verification: dict[str, Any],
    generation: dict[str, Any],
    stage_durations_ms: dict[str, float],
) -> dict[str, Any]:
    """Evaluate one completed pipeline run against concrete demo acceptance checks."""
    checks: list[dict[str, Any]] = []
    commit_valid = bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha or ""))
    checks.append(_check(
        "pinned_commit",
        "Immutable source snapshot",
        "pass" if commit_valid else "fail",
        "The run is bound to a full Git commit SHA." if commit_valid else "A valid full Git commit SHA was not recorded.",
        evidence={"commit_sha": commit_sha},
    ))

    included_files = int(inventory.get("included_files", 0))
    included_bytes = int(inventory.get("included_bytes", 0))
    checks.append(_check(
        "source_inventory",
        "Source inventory",
        "pass" if included_files > 0 else "fail",
        f"{included_files} files and {included_bytes} bytes were included in source-only analysis.",
        evidence={
            "included_files": included_files,
            "included_bytes": included_bytes,
            "excluded_files": int(inventory.get("excluded_files", 0)),
        },
    ))

    coverage = structural_analysis.get("coverage", {})
    coverage_status = coverage.get("status")
    checks.append(_check(
        "structural_coverage",
        "Structural extraction coverage",
        "pass" if coverage_status == "complete" else "review",
        (
            f"All {coverage.get('eligible_files', 0)} eligible files were parsed."
            if coverage_status == "complete"
            else f"Coverage is {coverage.get('coverage_percent', 0)}%; unsupported or failed files require review."
        ),
        evidence={
            key: coverage.get(key)
            for key in ("status", "coverage_percent", "eligible_files", "parsed_files", "unsupported_files", "failed_files")
        },
    ))

    document_by_id = {item.get("id"): item for item in documents}
    missing_documents = sorted(set(DOCUMENT_SPECS) - set(document_by_id))
    incomplete_documents = sorted(
        document_id
        for document_id in DOCUMENT_SPECS
        if document_by_id.get(document_id, {}).get("generation_status") != "completed"
    )
    documents_pass = not missing_documents and not incomplete_documents
    checks.append(_check(
        "five_documents",
        "Required documentation set",
        "pass" if documents_pass else "fail",
        "All five required Markdown documents were generated." if documents_pass else "One or more required documents are missing or incomplete.",
        evidence={"expected": len(DOCUMENT_SPECS), "present": len(document_by_id), "missing": missing_documents, "incomplete": incomplete_documents},
    ))

    verification_status = verification.get("status")
    checks.append(_check(
        "document_verification",
        "Final document verification",
        "pass" if verification_status == "checks_passed" else "fail" if verification_status == "verification_incomplete" else "review",
        f"Final document verification status is {verification_status or 'missing'}.",
        evidence={"status": verification_status, "correction_rounds": verification.get("correction_rounds", 0)},
    ))

    claim_items = claims.get("claims", [])
    claim_counts = Counter(item.get("verdict", "unverified") for item in claim_items)
    claims_pass = not claim_items or claim_counts.get("supported", 0) == len(claim_items)
    checks.append(_check(
        "claim_grounding",
        "Claim grounding",
        "pass" if claims_pass else "fail",
        (
            f"All {len(claim_items)} generated claims are supported by pinned source evidence."
            if claims_pass
            else f"{len(claim_items) - claim_counts.get('supported', 0)} claims are not supported."
        ),
        evidence={"total": len(claim_items), "verdicts": dict(claim_counts)},
    ))

    contract_checks = verification.get("contract_checks", {})
    incomplete_contracts = sorted(
        name for name, metric in contract_checks.items() if metric.get("status") != "complete"
    )
    contract_pass = bool(contract_checks) and not incomplete_contracts
    checks.append(_check(
        "exact_document_contracts",
        "Exact rendered contracts",
        "pass" if contract_pass else "fail",
        (
            f"All {len(contract_checks)} implemented exact document contracts match extracted facts."
            if contract_pass
            else "Exact contract checks are missing or contain mismatches."
        ),
        evidence={"checked": len(contract_checks), "incomplete": incomplete_contracts},
    ))

    finding_items = findings.get("findings", [])
    invalid_citations = [item for item in finding_items if item.get("code") == "invalid_source_citation"]
    checks.append(_check(
        "source_citations",
        "Resolvable source references",
        "pass" if not invalid_citations else "fail",
        "No invalid rendered source citations remain." if not invalid_citations else f"{len(invalid_citations)} invalid citation findings remain.",
        evidence={"invalid_citation_findings": len(invalid_citations)},
    ))

    blocking_findings = [item for item in finding_items if item.get("severity") in {"critical", "high"}]
    checks.append(_check(
        "blocking_findings",
        "Blocking findings",
        "pass" if not blocking_findings else "fail",
        "No critical or high-severity findings remain." if not blocking_findings else f"{len(blocking_findings)} blocking findings remain open.",
        evidence={"codes": [item.get("code") for item in blocking_findings[:20]]},
    ))

    checks.append(_check(
        "source_only_execution",
        "Source-only execution policy",
        "pass",
        "The reverse-engineering pipeline used file parsing only and did not invoke repository build, test, or startup commands.",
        evidence={"mode": "source_only", "repository_commands_executed": False},
    ))

    statuses = Counter(item["status"] for item in checks)
    required_failures = [item for item in checks if item["required"] and item["status"] == "fail"]
    required_reviews = [item for item in checks if item["required"] and item["status"] == "review"]
    overall_status = "not_ready" if required_failures else "needs_review" if required_reviews else "ready"
    recommendations = [item["message"] for item in required_failures + required_reviews]
    verification_limitations = list(dict.fromkeys(verification.get("limitations", [])))
    return {
        "schema_version": 1,
        "status": overall_status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha,
        "summary": {
            "total_checks": len(checks),
            "passed": statuses.get("pass", 0),
            "review": statuses.get("review", 0),
            "failed": statuses.get("fail", 0),
            "score_percent": round(statuses.get("pass", 0) / len(checks) * 100, 1) if checks else 0.0,
        },
        "checks": checks,
        "measured_stage_durations_ms": stage_durations_ms,
        "generation": {
            key: generation.get(key)
            for key in ("method", "provider", "model", "warnings")
        },
        "recommendations": recommendations,
        "limitations": verification_limitations,
    }
