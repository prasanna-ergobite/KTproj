"""Independent deterministic verification and bounded document correction."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.reverse_engineering.analysis import redact_source_excerpt
from app.reverse_engineering.documents import DOCUMENT_SPECS, _source_ref, _table_cell, generate_documents
from app.reverse_engineering.source_access import read_source_range, resolve_source_path

BLOCKING_SEVERITIES = {"critical", "high"}
REQUIRED_SECTIONS = {
    "system_overview": ["## Project purpose", "## Components", "## Entry points", "## External dependencies"],
    "business_logic": ["## Detected capabilities", "## Request workflows", "## Additional implementation behavior"],
    "database_api_reference": ["## Data models", "## API endpoints", "## Error handling evidence"],
    "developer_operations": ["## Detected prerequisites", "## Configuration", "## Startup and build commands", "## Tests"],
    "kt_handover": ["## Prerequisite knowledge", "## Recommended session sequence", "## Practical exercises", "## Handover checkpoints"],
}


def _finding(
    findings: list[dict[str, Any]],
    code: str,
    severity: str,
    message: str,
    *,
    document_id: str | None = None,
    evidence_ids: list[str] | None = None,
    fixable: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    findings.append({
        "id": f"finding-{len(findings) + 1:05d}", "code": code, "severity": severity,
        "document_id": document_id, "message": message, "evidence_ids": evidence_ids or [],
        "fixable": fixable, "details": details or {}, "status": "open",
    })


def _fact_index(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed = {}
    for collection in ("routes", "models", "behaviors"):
        for item in facts.get(collection, []):
            indexed[item["id"]] = item
    for item in facts.get("entry_points", []):
        if item.get("evidence_id"):
            indexed[item["evidence_id"]] = item
    return indexed


def _expected_matches(category: str, expected: dict[str, Any], fact: dict[str, Any]) -> bool:
    if category == "data_contract":
        return (
            fact.get("qualified_name") == expected.get("qualified_name")
            and fact.get("model_kind") == expected.get("model_kind")
            and len(fact.get("fields", [])) == expected.get("field_count")
            and fact.get("file") == expected.get("file")
            and fact.get("line") == expected.get("line")
        )
    if category == "derived_behavior":
        return (
            fact.get("qualified_name") == expected.get("qualified_name")
            and len(fact.get("conditions", [])) == expected.get("conditions")
            and len(fact.get("exceptions", [])) == expected.get("exceptions")
            and len(fact.get("state_changes", [])) == expected.get("state_changes")
            and len(fact.get("side_effects", [])) == expected.get("side_effects")
            and fact.get("file") == expected.get("file")
            and fact.get("start_line") == expected.get("start_line")
        )
    return all(fact.get(key) == value for key, value in expected.items())


def _verify_evidence(repository_root: Path, claim_evidence: dict[str, Any], evidence_item: dict[str, Any], commit_sha: str) -> tuple[bool, str]:
    if evidence_item.get("commit_sha") != commit_sha:
        return False, "Evidence commit does not match the run commit."
    for key in ("file_path", "start_line", "end_line", "content_hash"):
        expected_key = "file" if key == "file_path" else key
        if claim_evidence.get(key) != evidence_item.get(expected_key):
            return False, f"Claim evidence {key} does not match its evidence record."
    try:
        source = read_source_range(
            repository_root,
            evidence_item["file"],
            evidence_item["start_line"],
            evidence_item["end_line"],
            max_characters=1600,
        )
    except (OSError, ValueError) as exc:
        return False, f"Evidence source could not be read: {exc}"
    excerpt = redact_source_excerpt(source["text"])
    content_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    if excerpt != evidence_item.get("excerpt") or content_hash != evidence_item.get("content_hash"):
        return False, "Evidence excerpt or hash no longer matches the pinned source."
    return True, "Source path, range, excerpt, hash, and commit match."


def _coverage(detected: list[Any], present: list[bool]) -> dict[str, Any]:
    denominator = len(detected)
    numerator = sum(present)
    return {
        "detected": denominator,
        "documented": numerator,
        "ratio": round(numerator / denominator, 4) if denominator else None,
        "status": "not_applicable" if denominator == 0 else "complete" if numerator == denominator else "incomplete",
    }


def _read_documents(artifact_root: Path, documents: list[dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    content = {}
    failures = []
    for document in documents:
        if document.get("generation_status") != "completed" or not document.get("artifact_ref"):
            failures.append(document["id"])
            continue
        path = (artifact_root / document["artifact_ref"]).resolve()
        try:
            path.relative_to(artifact_root.resolve())
            if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                failures.append(document["id"])
                continue
            content[document["id"]] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            failures.append(document["id"])
    return content, failures


def _markdown_section(markdown: str, heading: str, *, stop_level: int = 2) -> str:
    """Return a heading body, stopping at the next heading of the same or higher level."""
    lines = markdown.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return ""
    stop_pattern = re.compile(rf"^#{{1,{stop_level}}}\s")
    end = next((index for index in range(start, len(lines)) if stop_pattern.match(lines[index])), len(lines))
    return "\n".join(lines[start:end])


def _table_rows(section: str, header: str) -> list[str]:
    """Return data rows from the table identified by its exact header."""
    lines = section.splitlines()
    try:
        start = lines.index(header) + 2
    except ValueError:
        return []
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            if rows or line.strip():
                break
            continue
        rows.append(line)
    return rows


def _counter_difference(expected: list[str], actual: list[str]) -> tuple[list[str], list[str]]:
    expected_counter = Counter(expected)
    actual_counter = Counter(actual)
    return list((expected_counter - actual_counter).elements()), list((actual_counter - expected_counter).elements())


def _contract_metric(expected: list[str], actual: list[str]) -> dict[str, Any]:
    missing, unexpected = _counter_difference(expected, actual)
    matched = sum((Counter(expected) & Counter(actual)).values())
    return {
        "expected": len(expected),
        "matched": matched,
        "unexpected": len(unexpected),
        "status": "complete" if not missing and not unexpected else "mismatch",
        "missing_examples": missing[:10],
        "unexpected_examples": unexpected[:10],
    }


def _endpoint_rows(shared: dict[str, Any]) -> list[str]:
    return [
        f"| **{_table_cell(route.get('method'))}** | `{_table_cell(route.get('path'))}` | "
        f"`{_table_cell(route.get('handler'))}` | {_table_cell(route.get('framework'))} | "
        f"{_table_cell(route.get('confidence'))} | {_source_ref(route)} |"
        for route in shared["routes"]
    ]


def _model_field_rows(model: dict[str, Any]) -> list[str]:
    rows = []
    for field in model.get("fields", []):
        required = "yes" if field.get("required") is True else "no" if field.get("required") is False else "unknown"
        rows.append(
            f"| `{_table_cell(field['name'])}` | `{_table_cell(field.get('type'))}` | "
            f"{required} | `{_table_cell(field.get('default'))}` |"
        )
    return rows or ["| — | — | — | No fields extracted |"]


def _error_rows(shared: dict[str, Any]) -> list[str]:
    rows = [
        f"| `{_table_cell(behavior['qualified_name'])}` | `{_table_cell(exception['expression'])}` | "
        f"`{behavior['file']}:L{exception['line']}` |"
        for behavior in shared["behaviors"]
        for exception in behavior.get("exceptions", [])
    ]
    return rows or ["| — | No supported explicit exception paths detected | — |"]


def _workflow_contract_lines(workflow: dict[str, Any]) -> list[str]:
    route = workflow["trigger"]
    lines = [
        f"Handler: `{workflow.get('handler') or 'inline/unknown'}`  ",
        f"Evidence: {_source_ref(route)}",
    ]
    if workflow["conditions"]:
        lines.append("**Conditions and validations**")
        lines.extend(
            f"- `L{item['line']}`: `{_table_cell(item['expression'])}`"
            + (" — contains an exception path" if item.get("validation") else "")
            for item in workflow["conditions"]
        )
    if workflow["exceptions"]:
        lines.append("**Error paths**")
        lines.extend(f"- `L{item['line']}`: `{_table_cell(item['expression'])}`" for item in workflow["exceptions"])
    if workflow["state_changes"]:
        lines.append("**State changes**")
        lines.extend(
            f"- `L{item['line']}`: `{_table_cell(item['target'])}` ({item['operation']})"
            for item in workflow["state_changes"]
        )
    if workflow["side_effects"]:
        lines.append("**Side-effect calls**")
        lines.extend(f"- `L{item['line']}`: `{_table_cell(item['target'])}`" for item in workflow["side_effects"])
    if not any(workflow[key] for key in ("conditions", "exceptions", "state_changes", "side_effects")):
        lines.append("No supported branch, exception, state-change, or side-effect facts were extracted for this handler.")
    return lines


def _business_contract_lines(section: str) -> list[str]:
    return [line for line in section.splitlines() if line]


def _additional_behavior_rows(shared: dict[str, Any]) -> list[str]:
    route_handlers = {item.get("handler") for item in shared["routes"]}
    additional = [item for item in shared["behaviors"] if item.get("qualified_name") not in route_handlers]
    return [
        f"| `{_table_cell(item['qualified_name'])}` | {len(item['conditions'])} | {len(item['exceptions'])} | "
        f"{len(item['state_changes'])} | {len(item['side_effects'])} | {_source_ref(item)} |"
        for item in additional[:500]
    ]


def _expected_mermaid_lines(shared: dict[str, Any]) -> list[str]:
    components = shared["architecture"]["components"]
    node_ids = {item["name"]: f"c{index}" for index, item in enumerate(components)}
    lines = ["flowchart LR"]
    for item in components:
        safe_label = re.sub(r"[^A-Za-z0-9_./ -]", "", item["name"])
        lines.append(f'{node_ids[item["name"]]}["{safe_label}"]')
    for edge in shared["architecture"].get("interactions", []):
        if edge["source"] in node_ids and edge["target"] in node_ids:
            lines.append(
                f"{node_ids[edge['source']]} -->|imports ×{edge['evidence_count']}| {node_ids[edge['target']]}"
            )
    return lines


def _component_rows(shared: dict[str, Any]) -> list[str]:
    return [
        f"| `{_table_cell(component['name'])}` | {component['file_count']} | "
        f"{_table_cell(', '.join(component['languages']) or 'Unknown')} | {component['symbol_count']} | "
        f"{component['route_count']} | {component['model_count']} |"
        for component in shared["architecture"]["components"]
    ]


def _entry_point_rows(shared: dict[str, Any]) -> list[str]:
    rows = [
        f"| {_table_cell(item.get('kind'))} | `{_table_cell(item.get('target'))}` | "
        f"{_table_cell(item.get('framework'))} | {_source_ref(item)} |"
        for item in shared["entry_points"]
    ]
    return rows or ["| — | No entry points detected | — | — |"]


def _dependency_rows(shared: dict[str, Any]) -> list[str]:
    rows = [
        f"| `{_table_cell(item['name'])}` | `{_table_cell(item.get('version'))}` | "
        f"{_table_cell(item.get('scope'))} | `{_table_cell(item.get('source'))}` |"
        for item in shared["dependencies"]
    ]
    return rows or ["| — | — | — | No supported dependency declarations detected |"]


def _verify_document_contracts(
    findings: list[dict[str, Any]],
    shared: dict[str, Any],
    document_content: dict[str, str],
) -> dict[str, Any]:
    """Compare final rendered facts with the shared, source-derived analysis."""
    metrics: dict[str, Any] = {}
    api_doc = document_content.get("database_api_reference", "")
    if api_doc:
        model_index_section = _markdown_section(api_doc, "## Data models")
        expected_model_headings = [f"### `{model['qualified_name']}`" for model in shared["models"]]
        actual_model_headings = [
            line for line in model_index_section.splitlines() if line.startswith("### ")
        ]
        missing_model_headings, unexpected_model_headings = _counter_difference(
            expected_model_headings, actual_model_headings
        )
        endpoint_section = _markdown_section(api_doc, "## API endpoints")
        endpoint_expected = _endpoint_rows(shared)
        endpoint_actual = _table_rows(
            endpoint_section,
            "| Method | Path | Handler | Framework | Confidence | Source |",
        )
        if not endpoint_expected:
            endpoint_expected = ["| — | — | No supported API endpoints detected | — | — | — |"]
        metrics["api_endpoints"] = _contract_metric(endpoint_expected, endpoint_actual)

        model_metrics = []
        for model in shared["models"]:
            heading = f"### `{model['qualified_name']}`"
            section = _markdown_section(api_doc, heading, stop_level=3)
            expected_rows = _model_field_rows(model)
            actual_rows = _table_rows(section, "| Field | Type | Required | Default/declaration |")
            metric = _contract_metric(expected_rows, actual_rows)
            expected_metadata = f"Kind: `{model['model_kind']}`" + (
                f" · Table: `{model['table_name']}`" if model.get("table_name") else ""
            ) + f" · Source: {_source_ref(model)}"
            metric["model"] = model["qualified_name"]
            metric["metadata_matches"] = expected_metadata in section
            if not section or not metric["metadata_matches"]:
                metric["status"] = "mismatch"
            model_metrics.append(metric)
        metrics["models"] = {
            "expected": len(model_metrics),
            "matched": sum(item["status"] == "complete" for item in model_metrics),
            "unexpected": len(unexpected_model_headings),
            "status": "complete" if (
                all(item["status"] == "complete" for item in model_metrics)
                and not missing_model_headings
                and not unexpected_model_headings
            ) else "mismatch",
            "mismatches": [item for item in model_metrics if item["status"] != "complete"][:10],
            "missing_examples": missing_model_headings[:10],
            "unexpected_examples": unexpected_model_headings[:10],
        }

        error_section = _markdown_section(api_doc, "## Error handling evidence")
        error_expected = _error_rows(shared)
        error_actual = _table_rows(error_section, "| Handler | Raised expression | Source line |")
        metrics["error_paths"] = _contract_metric(error_expected, error_actual)

        database_mismatches = [
            name for name in ("api_endpoints", "models", "error_paths")
            if metrics[name]["status"] != "complete"
        ]
        if database_mismatches:
            _finding(
                findings,
                "database_api_contract_mismatch",
                "high",
                "The final database/API document does not exactly match extracted endpoint, model, or error facts.",
                document_id="database_api_reference",
                fixable=True,
                details={"mismatched_contracts": database_mismatches},
            )

    business_doc = document_content.get("business_logic", "")
    if business_doc:
        workflow_index_section = _markdown_section(business_doc, "## Request workflows")
        expected_workflow_headings = [
            f"### {index}. `{workflow['name']}`"
            for index, workflow in enumerate(shared["workflows"], start=1)
        ]
        actual_workflow_headings = [
            line for line in workflow_index_section.splitlines() if line.startswith("### ")
        ]
        missing_workflow_headings, unexpected_workflow_headings = _counter_difference(
            expected_workflow_headings, actual_workflow_headings
        )
        workflow_metrics = []
        for index, workflow in enumerate(shared["workflows"], start=1):
            heading = f"### {index}. `{workflow['name']}`"
            section = _markdown_section(business_doc, heading, stop_level=3)
            metric = _contract_metric(_workflow_contract_lines(workflow), _business_contract_lines(section))
            metric["workflow"] = workflow["name"]
            workflow_metrics.append(metric)
        additional_section = _markdown_section(business_doc, "## Additional implementation behavior")
        additional_metric = _contract_metric(
            _additional_behavior_rows(shared),
            _table_rows(
                additional_section,
                "| Function or method | Conditions | Errors | State changes | Side effects | Source |",
            ),
        )
        metrics["business_workflows"] = {
            "expected": len(workflow_metrics),
            "matched": sum(item["status"] == "complete" for item in workflow_metrics),
            "unexpected": len(unexpected_workflow_headings),
            "status": "complete" if (
                all(item["status"] == "complete" for item in workflow_metrics)
                and not missing_workflow_headings
                and not unexpected_workflow_headings
            ) else "mismatch",
            "mismatches": [item for item in workflow_metrics if item["status"] != "complete"][:10],
            "missing_examples": missing_workflow_headings[:10],
            "unexpected_examples": unexpected_workflow_headings[:10],
        }
        metrics["additional_behaviors"] = additional_metric
        if metrics["business_workflows"]["status"] != "complete" or additional_metric["status"] != "complete":
            _finding(
                findings,
                "business_logic_contract_mismatch",
                "high",
                "The final business-logic document does not exactly match extracted workflow and behavior facts.",
                document_id="business_logic",
                fixable=True,
            )

    architecture_doc = document_content.get("system_overview", "")
    if architecture_doc:
        component_actual = _table_rows(
            _markdown_section(architecture_doc, "## Components"),
            "| Component | Files | Languages | Symbols | Routes | Models |",
        )
        metrics["architecture_components"] = _contract_metric(_component_rows(shared), component_actual)
        entry_actual = _table_rows(
            _markdown_section(architecture_doc, "## Entry points"),
            "| Kind | Target | Framework | Source |",
        )
        metrics["entry_points"] = _contract_metric(_entry_point_rows(shared), entry_actual)
        dependency_actual = _table_rows(
            _markdown_section(architecture_doc, "## External dependencies"),
            "| Package | Version | Scope | Manifest |",
        )
        metrics["dependencies"] = _contract_metric(_dependency_rows(shared), dependency_actual)
        match = re.search(r"```mermaid\s*\n(.*?)\n```", architecture_doc, flags=re.DOTALL)
        actual_mermaid = [line.strip() for line in match.group(1).splitlines() if line.strip()] if match else []
        metrics["architecture_diagram"] = _contract_metric(_expected_mermaid_lines(shared), actual_mermaid)
        if metrics["architecture_diagram"]["status"] != "complete":
            _finding(
                findings,
                "architecture_diagram_mismatch",
                "high",
                "The Mermaid component diagram contains missing, altered, or invented nodes or edges.",
                document_id="system_overview",
                fixable=True,
                details=metrics["architecture_diagram"],
            )
        architecture_tables = [
            name for name in ("architecture_components", "entry_points", "dependencies")
            if metrics[name]["status"] != "complete"
        ]
        if architecture_tables:
            _finding(
                findings,
                "architecture_contract_mismatch",
                "high",
                "The final architecture document does not exactly match extracted component, entry-point, or dependency facts.",
                document_id="system_overview",
                fixable=True,
                details={"mismatched_contracts": architecture_tables},
            )
    return metrics


def _verify_once(
    repository_root: Path,
    artifact_root: Path,
    shared: dict[str, Any],
    facts: dict[str, Any],
    evidence_artifact: dict[str, Any],
    documents: list[dict[str, Any]],
    claims_artifact: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    commit_sha = shared["commit_sha"]
    evidence_by_id = {item["id"]: item for item in evidence_artifact.get("items", [])}
    facts_by_id = _fact_index(facts)
    document_content, failed_documents = _read_documents(artifact_root, documents)
    for document_id in failed_documents:
        _finding(findings, "document_missing", "critical", "Generated document artifact is missing or unreadable.", document_id=document_id, fixable=True)

    claims = claims_artifact.get("claims", [])
    for claim in claims:
        claim["checked_by"] = []
        claim["verdict"] = "unverified"
        claim["reason"] = "Awaiting verification"
        if claim.get("commit_sha") != commit_sha:
            claim.update({"verdict": "contradicted", "reason": "Claim commit does not match the run commit.", "checked_by": ["commit_binding"]})
            _finding(findings, "claim_commit_mismatch", "critical", claim["reason"], document_id=claim.get("document_type"))
            continue
        fact = facts_by_id.get(claim.get("fact_id"))
        if not fact:
            claim.update({"verdict": "insufficient_evidence", "reason": "Referenced structured fact is missing.", "checked_by": ["fact_identity"]})
            _finding(findings, "claim_fact_missing", "high", claim["reason"], document_id=claim.get("document_type"), evidence_ids=[item.get("evidence_id") for item in claim.get("evidence", []) if item.get("evidence_id")])
            continue
        if not _expected_matches(claim.get("category", ""), claim.get("expected", {}), fact):
            claim.update({"verdict": "contradicted", "reason": "Structured fact no longer matches the claim's expected values.", "checked_by": ["fact_identity", "structured_fact_match"]})
            _finding(findings, "claim_fact_mismatch", "high", claim["reason"], document_id=claim.get("document_type"), evidence_ids=[fact.get("evidence_id")] if fact.get("evidence_id") else [])
            continue
        evidence_results = []
        for reference in claim.get("evidence", []):
            evidence_item = evidence_by_id.get(reference.get("evidence_id"))
            if not evidence_item:
                evidence_results.append((False, "Referenced evidence record is missing."))
            else:
                evidence_results.append(_verify_evidence(repository_root, reference, evidence_item, commit_sha))
        if not evidence_results or not all(item[0] for item in evidence_results):
            reason = next((item[1] for item in evidence_results if not item[0]), "Claim has no evidence ranges.")
            claim.update({"verdict": "insufficient_evidence", "reason": reason, "checked_by": ["fact_identity", "source_evidence"]})
            _finding(findings, "claim_evidence_invalid", "high", reason, document_id=claim.get("document_type"), evidence_ids=[item.get("evidence_id") for item in claim.get("evidence", []) if item.get("evidence_id")])
            continue
        claim.update({"verdict": "supported", "reason": "Structured fact and pinned source evidence match.", "checked_by": ["commit_binding", "fact_identity", "structured_fact_match", "source_path", "source_hash"]})

    for document_id, required_sections in REQUIRED_SECTIONS.items():
        markdown = document_content.get(document_id, "")
        missing = [section for section in required_sections if section not in markdown]
        if missing:
            _finding(findings, "required_sections_missing", "high", f"Required sections are missing: {', '.join(missing)}", document_id=document_id, fixable=True, details={"missing": missing})
        if markdown and f"**Commit:** `{commit_sha}`" not in markdown:
            _finding(findings, "document_commit_mismatch", "critical", "Document does not declare the analyzed commit.", document_id=document_id, fixable=True)
        if markdown and f"**Repository:** `{shared['repository_name']}`" not in markdown:
            _finding(findings, "document_repository_mismatch", "critical", "Document does not declare the analyzed repository.", document_id=document_id, fixable=True)
        if re.search(r"(?i)<\s*(script|iframe|object|embed)\b|javascript\s*:", markdown):
            _finding(findings, "unsafe_markdown_content", "critical", "Document contains unsafe HTML or a JavaScript URL.", document_id=document_id, fixable=True)
        invalid_citations = []
        for citation in re.finditer(r"`([^`\r\n]+):L(\d+)(?:-L(\d+))?`", markdown):
            file_path, start_text, end_text = citation.groups()
            start_line = int(start_text)
            end_line = int(end_text or start_text)
            try:
                source_path = resolve_source_path(repository_root, file_path)
                line_count = len(source_path.read_text(encoding="utf-8", errors="replace").splitlines())
                if start_line < 1 or end_line < start_line or end_line > line_count:
                    invalid_citations.append(citation.group(0))
            except (OSError, ValueError):
                invalid_citations.append(citation.group(0))
        if invalid_citations:
            _finding(
                findings, "invalid_source_citation", "high",
                f"{len(invalid_citations)} rendered source citations do not resolve in the pinned checkout.",
                document_id=document_id, fixable=True,
                details={"citations": invalid_citations[:25]},
            )

    contract_checks = _verify_document_contracts(findings, shared, document_content)
    api_doc = document_content.get("database_api_reference", "")
    endpoint_section = _markdown_section(api_doc, "## API endpoints")
    actual_endpoint_rows = _table_rows(
        endpoint_section,
        "| Method | Path | Handler | Framework | Confidence | Source |",
    )
    route_presence = [row in actual_endpoint_rows for row in _endpoint_rows(shared)]
    model_presence = []
    for model in shared["models"]:
        section = _markdown_section(api_doc, f"### `{model['qualified_name']}`", stop_level=3)
        expected_rows = _model_field_rows(model)
        actual_rows = _table_rows(section, "| Field | Type | Required | Default/declaration |")
        expected_metadata = f"Kind: `{model['model_kind']}`" + (
            f" · Table: `{model['table_name']}`" if model.get("table_name") else ""
        ) + f" · Source: {_source_ref(model)}"
        model_presence.append(
            bool(section)
            and expected_metadata in section
            and not any(_counter_difference(expected_rows, actual_rows))
        )
    architecture_doc = document_content.get("system_overview", "")
    entry_presence = [str(item.get("target")) in architecture_doc for item in facts.get("entry_points", [])]
    dependency_presence = [str(item.get("name")) in architecture_doc for item in facts.get("dependencies", [])]
    operations_doc = document_content.get("developer_operations", "")
    config_keys = sorted({item.get("key") for item in facts.get("config_access", []) if item.get("key")})
    config_presence = [str(key) in operations_doc for key in config_keys]
    business_doc = document_content.get("business_logic", "")
    route_handlers = {item.get("handler") for item in shared["routes"]}
    additional_rows = _table_rows(
        _markdown_section(business_doc, "## Additional implementation behavior"),
        "| Function or method | Conditions | Errors | State changes | Side effects | Source |",
    )
    additional_expected = _additional_behavior_rows(shared)
    additional_by_name = {
        item.get("qualified_name"): row
        for item, row in zip(
            [item for item in shared["behaviors"] if item.get("qualified_name") not in route_handlers][:500],
            additional_expected,
        )
    }
    workflow_status_by_handler: dict[str, bool] = {}
    for index, workflow in enumerate(shared["workflows"], start=1):
        section = _markdown_section(business_doc, f"### {index}. `{workflow['name']}`", stop_level=3)
        missing, unexpected = _counter_difference(
            _workflow_contract_lines(workflow),
            _business_contract_lines(section),
        )
        workflow_status_by_handler[str(workflow.get("handler"))] = not missing and not unexpected
    behavior_presence = [
        workflow_status_by_handler.get(str(item.get("qualified_name")), False)
        if item.get("qualified_name") in route_handlers
        else additional_by_name.get(item.get("qualified_name")) in additional_rows
        for item in shared["behaviors"]
    ]
    coverage = {
        "endpoints": _coverage(facts.get("routes", []), route_presence),
        "models": _coverage(facts.get("models", []), model_presence),
        "entry_points": _coverage(facts.get("entry_points", []), entry_presence),
        "dependencies": _coverage(facts.get("dependencies", []), dependency_presence),
        "configuration": _coverage(config_keys, config_presence),
        "behavior_records": _coverage(facts.get("behaviors", []), behavior_presence),
    }
    coverage_documents = {
        "endpoints": "database_api_reference", "models": "database_api_reference",
        "entry_points": "system_overview", "dependencies": "system_overview",
        "configuration": "developer_operations", "behavior_records": "business_logic",
    }
    for name, metric in coverage.items():
        if metric["status"] == "incomplete":
            missing_count = metric["detected"] - metric["documented"]
            _finding(findings, f"{name}_coverage_incomplete", "high", f"{missing_count} of {metric['detected']} detected {name.replace('_', ' ')} are missing from the document.", document_id=coverage_documents[name], fixable=True, details=metric)

    if shared["coverage"].get("status") != "complete":
        _finding(findings, "structural_coverage_partial", "medium", "Structural extraction was partial; unsupported or failed files require review.", details=shared["coverage"])
    if shared.get("generation", {}).get("warnings"):
        _finding(findings, "generation_warning", "medium", "Document generation reported warnings.", details={"warnings": shared["generation"]["warnings"]})

    verdict_counts = Counter(claim.get("verdict", "unverified") for claim in claims)
    summary = {
        "claim_verdicts": dict(verdict_counts),
        "coverage": coverage,
        "contract_checks": contract_checks,
        "findings_by_severity": dict(Counter(item["severity"] for item in findings)),
        "open_findings": len(findings),
    }
    return findings, summary, claims


def verify_and_correct_documents(
    repository_root: Path,
    artifact_root: Path,
    shared: dict[str, Any],
    facts: dict[str, Any],
    evidence_artifact: dict[str, Any],
    documents: list[dict[str, Any]],
    claims_artifact: dict[str, Any],
    *,
    max_correction_rounds: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Verify final drafts and regenerate documents implicated by fixable findings."""
    current_documents = list(documents)
    corrected_documents: list[str] = []
    correction_history: list[dict[str, Any]] = []
    rounds = 0
    findings: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    verified_claims = claims_artifact.get("claims", [])
    while True:
        findings, summary, verified_claims = _verify_once(
            repository_root, artifact_root, shared, facts, evidence_artifact,
            current_documents, {**claims_artifact, "claims": verified_claims},
        )
        fixable_ids = {item["document_id"] for item in findings if item.get("fixable") and item.get("document_id") in DOCUMENT_SPECS}
        if not fixable_ids or rounds >= max_correction_rounds:
            break
        correction_history.append({
            "round": rounds + 1,
            "documents": sorted(fixable_ids),
            "triggering_findings": [
                {
                    "code": item["code"],
                    "severity": item["severity"],
                    "document_id": item["document_id"],
                    "message": item["message"],
                }
                for item in findings
                if item.get("fixable") and item.get("document_id") in fixable_ids
            ],
        })
        versions = {item["id"]: item.get("version", 1) + 1 for item in current_documents if item["id"] in fixable_ids}
        replacements, _ = generate_documents(
            artifact_root, shared, evidence_artifact,
            document_ids=fixable_ids, versions=versions,
        )
        replacement_by_id = {item["id"]: item for item in replacements}
        current_documents = [replacement_by_id.get(item["id"], item) for item in current_documents]
        corrected_documents.extend(sorted(fixable_ids))
        rounds += 1

    partial_scope = shared["coverage"].get("status") != "complete"
    for document in current_documents:
        document_findings = [item for item in findings if item.get("document_id") in {None, document["id"]}]
        has_blocker = any(item["severity"] in BLOCKING_SEVERITIES for item in document_findings)
        if document.get("generation_status") != "completed":
            document["verification_status"] = "verification_incomplete"
        elif has_blocker or partial_scope:
            document["verification_status"] = "needs_review"
        else:
            document["verification_status"] = "checks_passed"
    overall_status = "checks_passed" if all(item["verification_status"] == "checks_passed" for item in current_documents) else "needs_review"
    completed_at = datetime.now(timezone.utc).isoformat()
    claims_result = {**claims_artifact, "claims": verified_claims, "verified_at": completed_at}
    findings_result = {"schema_version": 1, "commit_sha": shared["commit_sha"], "findings": findings}
    report = {
        "schema_version": 1,
        "commit_sha": shared["commit_sha"],
        "status": overall_status,
        "scope": "deterministic source, fact, exact rendered contract, completeness, consistency, and Markdown safety checks",
        "completed_at": completed_at,
        "correction_rounds": rounds,
        "correction_history": correction_history,
        "corrected_documents": sorted(set(corrected_documents)),
        "max_correction_rounds": max_correction_rounds,
        "documents": current_documents,
        **summary,
        "limitations": [
            "Checks validate supported extraction and generated-document coverage; they do not prove runtime behavior.",
            "Tests and repository commands were not executed.",
            *shared.get("limitations", []),
        ],
    }
    return current_documents, claims_result, findings_result, report
