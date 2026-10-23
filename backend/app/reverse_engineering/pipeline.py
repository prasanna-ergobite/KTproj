"""Background orchestration for repository intake and inventory."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import UUID

from app.core.config import settings
from app.db.postgres_client import SessionLocal
from app.models.db_models import Task
from app.reverse_engineering.acceptance import acceptance_failure_report, build_acceptance_report
from app.reverse_engineering.analysis import analyze_repository
from app.reverse_engineering.artifacts import build_download_bundle
from app.reverse_engineering.documents import build_shared_analysis, generate_documents
from app.reverse_engineering.lifecycle import cleanup_run_checkout
from app.reverse_engineering.repository import build_source_inventory, clone_repository, sanitize_repository_url, write_json_artifact
from app.reverse_engineering.verification import verify_and_correct_documents

logger = logging.getLogger("autokt.reverse_engineering")


def _set_task(task_id: str, status: str, result: dict) -> None:
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == UUID(task_id)).first()
        if task:
            task.status = status
            task.result = result
            task.updated_at = datetime.now(timezone.utc)
            db.commit()


def run_reverse_engineering_task(task_id: str, repo_url: str, organization_id: str, ref: str | None = None) -> None:
    """Clone one immutable source snapshot and produce inventory and structural evidence."""
    safe_url = sanitize_repository_url(repo_url)
    work_root = Path(settings.REVERSE_ENGINEERING_WORK_ROOT).resolve()
    artifact_root = Path(settings.REVERSE_ENGINEERING_ARTIFACT_ROOT).resolve()
    checkout = work_root / task_id / "repository"
    run_artifacts = artifact_root / task_id
    base_result = {
        "run_id": task_id,
        "organization_id": organization_id,
        "repo_url": safe_url,
        "requested_ref": ref or None,
        "stage": "cloning",
    }
    current_result = base_result
    stage_started = perf_counter()
    stage_durations_ms: dict[str, float] = {}

    def finish_stage(stage: str) -> None:
        nonlocal stage_started
        now = perf_counter()
        stage_durations_ms[stage] = round((now - stage_started) * 1000, 2)
        stage_started = now

    try:
        _set_task(task_id, "running", current_result)
        revision = clone_repository(repo_url, checkout, ref)
        finish_stage("clone")
        analyzing = {**base_result, **revision, "stage": "inventory"}
        current_result = analyzing
        _set_task(task_id, "running", analyzing)
        inventory = build_source_inventory(
            checkout,
            max_files=settings.REVERSE_ENGINEERING_MAX_FILES,
            max_file_bytes=settings.REVERSE_ENGINEERING_MAX_FILE_BYTES,
            max_included_bytes=settings.REVERSE_ENGINEERING_MAX_SOURCE_BYTES,
        )
        write_json_artifact(run_artifacts / "inventory.json", inventory)
        finish_stage("inventory")
        structural_stage = {
            **analyzing,
            "stage": "structural_analysis",
            "inventory": {key: value for key, value in inventory.items() if key not in {"files", "exclusions"}},
        }
        current_result = structural_stage
        _set_task(task_id, "running", structural_stage)
        extracted_facts, repository_analysis, evidence = analyze_repository(checkout, inventory, revision["commit_sha"])
        write_json_artifact(run_artifacts / "extracted_facts.json", extracted_facts)
        write_json_artifact(run_artifacts / "structural_analysis.json", repository_analysis)
        write_json_artifact(run_artifacts / "evidence.json", evidence)
        finish_stage("structural_analysis")
        compact_analysis = {
            "summary": repository_analysis["summary"],
            "coverage": repository_analysis["coverage"],
            "architecture": {
                "style": repository_analysis["architecture"]["style"],
                "components": [
                    {key: value for key, value in component.items() if key != "files"}
                    for component in repository_analysis["architecture"]["components"]
                ],
                "interactions": repository_analysis["architecture"]["interactions"],
                "entry_points": repository_analysis["architecture"]["entry_points"][:25],
            },
            "uncertainties": repository_analysis["uncertainties"][:100],
        }
        repository_name = Path(repo_url.rstrip("/")).stem.removesuffix(".git")
        shared_stage = {**structural_stage, "stage": "shared_analysis", "analysis": compact_analysis}
        current_result = shared_stage
        _set_task(task_id, "running", shared_stage)
        shared_analysis = build_shared_analysis(
            checkout,
            inventory,
            extracted_facts,
            repository_analysis,
            repository_name,
            allow_llm=settings.REVERSE_ENGINEERING_LLM_ENRICHMENT,
        )
        write_json_artifact(run_artifacts / "repository_analysis.json", shared_analysis)
        finish_stage("shared_analysis")
        generation_stage = {
            **shared_stage,
            "stage": "document_generation",
        }
        current_result = generation_stage
        _set_task(task_id, "running", generation_stage)
        documents, claims = generate_documents(run_artifacts, shared_analysis, evidence)
        finish_stage("document_generation")
        verification_stage = {
            **generation_stage,
            "stage": "verification",
            "documents": documents,
            "claims_count": len(claims["claims"]),
        }
        current_result = verification_stage
        _set_task(task_id, "running", verification_stage)
        try:
            documents, claims, findings, verification_report = verify_and_correct_documents(
                checkout,
                run_artifacts,
                shared_analysis,
                extracted_facts,
                evidence,
                documents,
                claims,
                max_correction_rounds=settings.REVERSE_ENGINEERING_MAX_CORRECTION_ROUNDS,
            )
        except Exception as verification_error:
            logger.exception("Document verification failed for task %s", task_id)
            completed_at = datetime.now(timezone.utc).isoformat()
            for document in documents:
                document["verification_status"] = "verification_incomplete"
            findings = {
                "schema_version": 1,
                "commit_sha": revision["commit_sha"],
                "findings": [{
                    "id": "finding-00001",
                    "code": "verification_execution_failed",
                    "severity": "critical",
                    "document_id": None,
                    "message": str(verification_error)[:1000],
                    "evidence_ids": [],
                    "fixable": False,
                    "details": {},
                    "status": "open",
                }],
            }
            verification_report = {
                "schema_version": 1,
                "commit_sha": revision["commit_sha"],
                "status": "verification_incomplete",
                "scope": "deterministic source, fact, exact rendered contract, completeness, consistency, and Markdown safety checks",
                "completed_at": completed_at,
                "correction_rounds": 0,
                "correction_history": [],
                "corrected_documents": [],
                "max_correction_rounds": settings.REVERSE_ENGINEERING_MAX_CORRECTION_ROUNDS,
                "documents": documents,
                "claim_verdicts": {"unverified": len(claims["claims"])},
                "coverage": {},
                "contract_checks": {},
                "findings_by_severity": {"critical": 1},
                "open_findings": 1,
                "limitations": [
                    "Verification did not complete; generated documents remain available as unverified drafts.",
                    "Tests and repository commands were not executed.",
                ],
            }
        write_json_artifact(run_artifacts / "verification" / "claims.json", claims)
        write_json_artifact(run_artifacts / "verification" / "findings.json", findings)
        write_json_artifact(run_artifacts / "verification" / "report.json", verification_report)
        finish_stage("verification_and_correction")
        try:
            acceptance_report = build_acceptance_report(
                commit_sha=revision["commit_sha"],
                inventory=inventory,
                structural_analysis=repository_analysis,
                documents=documents,
                claims=claims,
                findings=findings,
                verification=verification_report,
                generation=shared_analysis["generation"],
                stage_durations_ms=stage_durations_ms,
            )
        except Exception as acceptance_error:
            logger.exception("Acceptance evaluation failed for task %s", task_id)
            acceptance_report = acceptance_failure_report(
                revision["commit_sha"], stage_durations_ms, acceptance_error
            )
        write_json_artifact(run_artifacts / "acceptance" / "report.json", acceptance_report)
        artifact_refs = {
            "inventory": "inventory.json",
            "extracted_facts": "extracted_facts.json",
            "structural_analysis": "structural_analysis.json",
            "repository_analysis": "repository_analysis.json",
            "evidence": "evidence.json",
            "claims": "verification/claims.json",
            "findings": "verification/findings.json",
            "verification_report": "verification/report.json",
            "acceptance_report": "acceptance/report.json",
            "manifest": "manifest.json",
            "bundle": "bundle.zip",
        }
        verification_summary = {
            key: verification_report[key]
            for key in (
                "status", "scope", "completed_at", "correction_rounds", "correction_history", "corrected_documents",
                "claim_verdicts", "coverage", "contract_checks", "findings_by_severity", "open_findings", "limitations",
            )
        }
        result = {
            **analyzing,
            "stage": "completed",
            "repository_name": repository_name,
            "inventory": {key: value for key, value in inventory.items() if key not in {"files", "exclusions"}},
            "analysis": compact_analysis,
            "files_preview": inventory["files"][:30],
            "exclusions_preview": inventory["exclusions"][:20],
            "routes_preview": extracted_facts["routes"][:20],
            "models_preview": extracted_facts["models"][:20],
            "dependencies_preview": extracted_facts["dependencies"][:30],
            "documents": documents,
            "generation": shared_analysis["generation"],
            "claims_count": len(claims["claims"]),
            "verification": verification_summary,
            "acceptance": acceptance_report,
            "findings_preview": findings["findings"][:20],
            "artifact_refs": artifact_refs,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest = {
            "schema_version": 1,
            "run_id": task_id,
            "repository": {"url": safe_url, **revision, "requested_ref": ref},
            "organization_id": organization_id,
            "status": "completed",
            "coverage": repository_analysis["coverage"],
            "artifacts": artifact_refs,
            "documents": documents,
            "document_status": "completed" if all(item["generation_status"] == "completed" for item in documents) else "partial",
            "generation": shared_analysis["generation"],
            "verification": verification_summary,
            "acceptance": acceptance_report,
            "bundle_status": "available",
            "updated_at": result["completed_at"],
        }
        write_json_artifact(run_artifacts / "manifest.json", manifest)
        try:
            bundle = build_download_bundle(
                run_artifacts,
                artifact_refs,
                documents,
                max_file_bytes=settings.REVERSE_ENGINEERING_MAX_BUNDLE_BYTES,
                max_total_bytes=settings.REVERSE_ENGINEERING_MAX_BUNDLE_BYTES,
            )
            result["bundle"] = bundle
        except Exception as bundle_error:
            logger.exception("Artifact bundle generation failed for task %s", task_id)
            artifact_refs.pop("bundle", None)
            result["bundle"] = {"artifact_ref": None, "error": str(bundle_error)[:1000]}
            manifest["artifacts"] = artifact_refs
            manifest["bundle_status"] = "failed"
            manifest["bundle_error"] = str(bundle_error)[:1000]
            write_json_artifact(run_artifacts / "manifest.json", manifest)
        _set_task(task_id, "completed", result)
    except Exception as exc:
        logger.exception("Reverse-engineering analysis failed for task %s", task_id)
        _set_task(task_id, "failed", {**current_result, "stage": "failed", "error": str(exc)[:2000]})
    finally:
        if not settings.REVERSE_ENGINEERING_KEEP_CHECKOUTS:
            try:
                cleanup_run_checkout(work_root, task_id)
            except (OSError, ValueError):
                logger.exception("Failed to clean reverse-engineering checkout for task %s", task_id)

