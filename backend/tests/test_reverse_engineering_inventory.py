import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.reverse_engineering.repository import build_source_inventory, validate_git_ref, validate_remote_repository_url, write_json_artifact
from app.reverse_engineering.analysis import analyze_repository
from app.reverse_engineering.artifacts import build_download_bundle
from app.reverse_engineering.documents import build_shared_analysis, generate_documents
from app.reverse_engineering.lifecycle import cleanup_run_checkout, interrupted_result
from app.reverse_engineering.source_access import lookup_symbol, read_source_range, resolve_source_path, search_source
from app.reverse_engineering.verification import verify_and_correct_documents


def test_remote_url_validation_rejects_credentials_and_unapproved_hosts():
    with pytest.raises(ValueError, match="credentials"):
        validate_remote_repository_url("https://token@github.com/acme/demo.git", ["github.com"])
    with pytest.raises(ValueError, match="not enabled"):
        validate_remote_repository_url("https://example.com/acme/demo.git", ["github.com"])
    assert validate_remote_repository_url("https://github.com/acme/demo.git", ["github.com"]) == "https://github.com/acme/demo.git"


def test_git_ref_validation_rejects_option_like_and_malformed_values():
    assert validate_git_ref("release/1.2.0") == "release/1.2.0"
    assert validate_git_ref("  ") is None
    for value in ("--upload-pack=bad", "feature..branch", "branch.lock", "space branch"):
        with pytest.raises(ValueError, match="valid branch"):
            validate_git_ref(value)


def test_inventory_classifies_source_and_excludes_secrets_and_vendor(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"18"}}', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi==0.110.0\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=do-not-read\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("vendor", encoding="utf-8")

    inventory = build_source_inventory(tmp_path)

    assert inventory["included_files"] == 4
    assert inventory["languages"] == {"Python": 2}
    assert inventory["categories"]["test"] == 1
    assert inventory["frameworks"] == ["FastAPI", "React"]
    assert {item["path"] for item in inventory["files"]} == {
        "package.json", "requirements.txt", "src/main.py", "tests/test_main.py",
    }
    reasons = {(item["path"], item["reason"]) for item in inventory["exclusions"]}
    assert (".env", "sensitive_file") in reasons
    assert ("node_modules/", "generated_or_vendor_directory") in reasons


def test_json_artifact_is_complete(tmp_path: Path):
    destination = tmp_path / "run" / "inventory.json"
    write_json_artifact(destination, {"included_files": 2})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"included_files": 2}
    assert not destination.with_suffix(".json.tmp").exists()


def test_download_bundle_contains_only_explicit_artifacts(tmp_path: Path):
    (tmp_path / "documents").mkdir()
    (tmp_path / "verification").mkdir()
    (tmp_path / "acceptance").mkdir()
    (tmp_path / "documents" / "system_overview.md").write_text("# Overview\n", encoding="utf-8")
    (tmp_path / "verification" / "report.json").write_text('{"status":"checks_passed"}', encoding="utf-8")
    (tmp_path / "acceptance" / "report.json").write_text('{"status":"ready"}', encoding="utf-8")
    (tmp_path / "repository.py").write_text("secret source must not be bundled", encoding="utf-8")

    bundle = build_download_bundle(
        tmp_path,
        {
            "verification_report": "verification/report.json",
            "acceptance_report": "acceptance/report.json",
            "bundle": "bundle.zip",
        },
        [{
            "id": "system_overview",
            "generation_status": "completed",
            "artifact_ref": "documents/system_overview.md",
        }],
    )

    assert bundle["size_bytes"] > 0
    assert len(bundle["sha256"]) == 64
    with zipfile.ZipFile(tmp_path / "bundle.zip") as archive:
        assert archive.namelist() == [
            "acceptance/report.json", "documents/system_overview.md", "verification/report.json",
        ]
        assert "repository.py" not in archive.namelist()
    with pytest.raises(ValueError, match="safe run-relative"):
        build_download_bundle(tmp_path, {"bad": "../repository.py"}, [])


def test_checkout_cleanup_is_bounded_to_uuid_run_directory(tmp_path: Path):
    task_id = "b4ad67aa-f2be-4e3c-a747-c806e60217ce"
    checkout = tmp_path / task_id / "repository"
    checkout.mkdir(parents=True)
    (checkout / "source.py").write_text("print('fixture')", encoding="utf-8")
    sibling = tmp_path / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")

    assert cleanup_run_checkout(tmp_path, task_id) is True
    assert not (tmp_path / task_id).exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert cleanup_run_checkout(tmp_path, task_id) is False
    with pytest.raises(ValueError):
        cleanup_run_checkout(tmp_path, "../outside")


def test_interrupted_result_preserves_progress_and_becomes_explicitly_terminal():
    detected_at = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
    result = interrupted_result({"stage": "structural_analysis", "commit_sha": "abc"}, detected_at)
    assert result["stage"] == "interrupted"
    assert result["commit_sha"] == "abc"
    assert "backend restarted" in result["error"]
    assert result["interrupted_at"] == "2026-10-14T08:30:00+00:00"


def test_structural_analysis_extracts_evidence_and_reports_unsupported_files(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "service.py").write_text("def load_user(user_id: int):\n    return {'id': user_id}\n", encoding="utf-8")
    (tmp_path / "app" / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "from app.service import load_user\n\n"
        "router = APIRouter(prefix='/users')\n\n"
        "class User(BaseModel):\n"
        "    id: int\n"
        "    name: str = 'unknown'\n\n"
        "@router.get('/{user_id}', response_model=User)\n"
        "def get_user(user_id: int):\n"
        "    return load_user(user_id)\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "main.tsx").write_text(
        "import React from 'react';\nimport { createRoot } from 'react-dom/client';\ncreateRoot(document.body);\n",
        encoding="utf-8",
    )
    (tmp_path / "worker.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react":"18.2.0"},"scripts":{"start":"vite"}}',
        encoding="utf-8",
    )

    inventory = build_source_inventory(tmp_path)
    facts, analysis, evidence = analyze_repository(tmp_path, inventory, "a" * 40)

    assert analysis["coverage"]["status"] == "partial"
    assert analysis["coverage"]["parsed_files"] == 3
    assert analysis["coverage"]["unsupported_files"] == 1
    assert analysis["summary"]["routes"] == 1
    assert facts["routes"][0]["path"] == "/users/{user_id}"
    assert facts["routes"][0]["evidence_id"]
    assert any(model["name"] == "User" and len(model["fields"]) == 2 for model in facts["models"])
    assert any(item["name"] == "react" for item in facts["dependencies"])
    assert any(item["target"] == "createRoot" for item in facts["entry_points"])
    assert evidence["items"]
    assert lookup_symbol(facts, "get_user")[0]["file"] == "app/api.py"

    shared = build_shared_analysis(tmp_path, inventory, facts, analysis, "fixture-project", allow_llm=False)
    documents, claims = generate_documents(tmp_path / "artifacts", shared, evidence)
    assert len(documents) == 5
    assert all(document["generation_status"] == "completed" for document in documents)
    assert all(document["verification_status"] == "pending" for document in documents)
    api_reference = (tmp_path / "artifacts" / "documents" / "database_api_reference.md").read_text(encoding="utf-8")
    assert "GET" in api_reference
    assert "/users/{user_id}" in api_reference
    assert "app/api.py:L12" in api_reference
    assert any(claim["category"] == "api_contract" and claim["evidence"] for claim in claims["claims"])

    verified_documents, verified_claims, findings, report = verify_and_correct_documents(
        tmp_path,
        tmp_path / "artifacts",
        shared,
        facts,
        evidence,
        documents,
        claims,
    )
    assert report["status"] == "needs_review"
    assert report["claim_verdicts"]["supported"] == len(claims["claims"])
    assert report["coverage"]["endpoints"]["status"] == "complete"
    assert any(item["code"] == "structural_coverage_partial" for item in findings["findings"])
    assert all(document["verification_status"] == "needs_review" for document in verified_documents)
    assert verified_claims["verified_at"]


def test_verification_repairs_a_fixable_document_failure(tmp_path: Path):
    (tmp_path / "service.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    inventory = build_source_inventory(tmp_path)
    facts, analysis, evidence = analyze_repository(tmp_path, inventory, "c" * 40)
    shared = build_shared_analysis(tmp_path, inventory, facts, analysis, "repair-fixture", allow_llm=False)
    artifact_root = tmp_path / "artifacts"
    documents, claims = generate_documents(artifact_root, shared, evidence)
    system_overview = artifact_root / "documents" / "system_overview.md"
    system_overview.write_text(
        system_overview.read_text(encoding="utf-8").replace("## Components", "## Removed section"),
        encoding="utf-8",
    )

    verified_documents, verified_claims, findings, report = verify_and_correct_documents(
        tmp_path, artifact_root, shared, facts, evidence, documents, claims,
    )

    repaired = next(item for item in verified_documents if item["id"] == "system_overview")
    assert repaired["version"] == 2
    assert report["correction_rounds"] == 1
    assert report["correction_history"][0]["triggering_findings"][0]["code"] == "required_sections_missing"
    assert report["corrected_documents"] == ["system_overview"]
    assert report["status"] == "checks_passed"
    assert findings["findings"] == []
    assert all(claim["verdict"] == "supported" for claim in verified_claims["claims"])
    assert "## Components" in system_overview.read_text(encoding="utf-8")


def test_verification_repairs_altered_exact_contracts(tmp_path: Path):
    (tmp_path / "routes").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "user.py").write_text(
        "def load_user(user_id: int):\n"
        "    if user_id < 1:\n"
        "        raise ValueError('invalid user id')\n"
        "    result = {'id': user_id}\n"
        "    return result\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "from pydantic import BaseModel\n"
        "from services.user import load_user\n\n"
        "app = FastAPI()\n\n"
        "class User(BaseModel):\n"
        "    id: int\n"
        "    name: str = 'unknown'\n\n"
        "@app.get('/users/{user_id}', response_model=User)\n"
        "def get_user(user_id: int):\n"
        "    if user_id < 1:\n"
        "        raise ValueError('invalid user id')\n"
        "    return load_user(user_id)\n",
        encoding="utf-8",
    )

    inventory = build_source_inventory(tmp_path)
    facts, analysis, evidence = analyze_repository(tmp_path, inventory, "d" * 40)
    shared = build_shared_analysis(tmp_path, inventory, facts, analysis, "contract-fixture", allow_llm=False)
    artifact_root = tmp_path / "artifacts"
    documents, claims = generate_documents(artifact_root, shared, evidence)

    database_doc = artifact_root / "documents" / "database_api_reference.md"
    database_doc.write_text(
        database_doc.read_text(encoding="utf-8")
        .replace("**GET**", "**POST**", 1)
        .replace("| `name` |", "| `invented_name` |", 1),
        encoding="utf-8",
    )
    business_doc = artifact_root / "documents" / "business_logic.md"
    business_markdown = business_doc.read_text(encoding="utf-8")
    behavior_row = next(
        line for line in business_markdown.splitlines()
        if line.startswith("|") and "load_user" in line
    )
    assert "user_id < 1" in business_markdown
    business_doc.write_text(
        business_markdown
        .replace(behavior_row, behavior_row.replace("| 1 |", "| 999 |", 1), 1)
        .replace("**Error paths**", "**Removed error paths**", 1)
        .replace("user_id < 1", "user_id > 1", 1),
        encoding="utf-8",
    )
    system_doc = artifact_root / "documents" / "system_overview.md"
    system_doc.write_text(
        system_doc.read_text(encoding="utf-8").replace(
            "flowchart LR",
            "flowchart LR\n  c0 -->|imports ×999| c0",
            1,
        ),
        encoding="utf-8",
    )

    verified_documents, _, findings, report = verify_and_correct_documents(
        tmp_path, artifact_root, shared, facts, evidence, documents, claims,
    )

    triggering_codes = {
        item["code"]
        for item in report["correction_history"][0]["triggering_findings"]
    }
    assert "database_api_contract_mismatch" in triggering_codes
    assert "business_logic_contract_mismatch" in triggering_codes
    assert "architecture_diagram_mismatch" in triggering_codes
    assert report["correction_rounds"] == 1
    assert report["corrected_documents"] == [
        "business_logic", "database_api_reference", "system_overview",
    ]
    assert report["status"] == "checks_passed"
    assert findings["findings"] == []
    assert all(
        item["version"] == 2
        for item in verified_documents
        if item["id"] in report["corrected_documents"]
    )
    assert report["contract_checks"]["api_endpoints"]["status"] == "complete"
    assert report["contract_checks"]["models"]["status"] == "complete"
    assert report["contract_checks"]["error_paths"]["status"] == "complete"
    assert report["contract_checks"]["business_workflows"]["status"] == "complete"
    assert report["contract_checks"]["architecture_diagram"]["status"] == "complete"


def test_bounded_source_access_rejects_traversal_and_limits_results(tmp_path: Path):
    (tmp_path / "source.py").write_text("alpha\nbeta alpha\ngamma\n", encoding="utf-8")
    assert read_source_range(tmp_path, "source.py", 2, 2)["text"] == "beta alpha"
    assert len(search_source(tmp_path, ["source.py"], "alpha", max_results=1)) == 1
    with pytest.raises(ValueError, match="repository-relative"):
        resolve_source_path(tmp_path, "../outside.py")


def test_java_and_typescript_framework_extractors_keep_qualified_routes(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "UserController.java").write_text(
        "package demo.api;\n"
        "@RestController\n@RequestMapping(\"/api/users\")\n"
        "public class UserController {\n"
        "  @GetMapping(\"/{id}\")\n"
        "  public User getUser(String id) { return null; }\n"
        "  public static void main(String[] args) {}\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "users.controller.ts").write_text(
        "@Controller('users')\nexport class UsersController {\n"
        "  @Get(':id')\n  getUser(id: string) { return id; }\n}\n"
        "export interface UserDto {\n  id: string;\n  name?: string;\n}\n",
        encoding="utf-8",
    )

    inventory = build_source_inventory(tmp_path)
    facts, analysis, _ = analyze_repository(tmp_path, inventory, "b" * 40)

    routes = {(item["framework"], item["method"], item["path"]) for item in facts["routes"]}
    assert ("spring", "GET", "/api/users/{id}") in routes
    assert ("nestjs", "GET", "/users/:id") in routes
    assert any(model["name"] == "UserDto" and len(model["fields"]) == 2 for model in facts["models"])
    assert analysis["coverage"]["coverage_percent"] == 100.0


def test_deployment_entry_points_support_compose_dockerfile_and_procfile(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text('CMD ["python", "app.py"]\n', encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services:\n  api:\n    command: python app.py\n", encoding="utf-8")
    (tmp_path / "Procfile").write_text("web: gunicorn app:app\n", encoding="utf-8")

    inventory = build_source_inventory(tmp_path)
    facts, _, _ = analyze_repository(tmp_path, inventory, "f" * 40)

    entries = {(item["kind"], item["target"], item["command"]) for item in facts["entry_points"]}
    assert ("container_command", "CMD", '["python", "app.py"]') in entries
    assert ("compose_command", "compose_command", "python app.py") in entries
    assert ("process_command", "web", "gunicorn app:app") in entries
