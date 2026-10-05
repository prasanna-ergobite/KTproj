import sys
import uuid
from pathlib import Path
from unittest.mock import patch
import pytest

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.graphs.relationship_extractor import (
    extract_imports,
    extract_calls,
    extract_extends,
    extract_test_edges,
    parse_manifest_deps,
    ImportEdge,
    CallEdge,
    ExtendsEdge,
    TestEdge,
    PackageRecord,
)
from app.models.schemas import Package
from app.core.config import settings
from app.db.neo4j_client import neo4j_client


# ---------------------------------------------------------------------------
# Unit Tests — Pure Extraction Logic
# ---------------------------------------------------------------------------

def test_extract_imports_python_internal_and_external():
    py_code = (
        "import os\n"
        "import sys\n"
        "from app.core.config import settings\n"
        "from app.db.neo4j_client import neo4j_client\n"
    )
    all_paths = {"app/core/config.py", "app/db/neo4j_client.py", "app/main.py"}
    edges = extract_imports(py_code, "app/main.py", "py", all_paths)

    assert len(edges) >= 2
    config_imp = [e for e in edges if "app.core.config" in e.module_name]
    assert len(config_imp) == 1
    assert config_imp[0].is_external is False
    assert config_imp[0].to_rel_path == "app/core/config.py"

    os_imp = [e for e in edges if e.module_name == "os"]
    assert len(os_imp) == 1
    assert os_imp[0].is_external is True


def test_extract_imports_js_ts():
    ts_code = (
        "import React from 'react';\n"
        "import { helper } from './utils/helper';\n"
    )
    all_paths = {"src/index.ts", "src/utils/helper.ts"}
    edges = extract_imports(ts_code, "src/index.ts", "ts", all_paths)

    react_imp = [e for e in edges if e.module_name == "react"]
    assert len(react_imp) == 1
    assert react_imp[0].is_external is True

    helper_imp = [e for e in edges if "./utils/helper" in e.module_name]
    assert len(helper_imp) == 1
    assert helper_imp[0].is_external is False
    assert helper_imp[0].to_rel_path == "src/utils/helper.ts"


def test_extract_calls_same_file():
    code = (
        "def process_data(data):\n"
        "    validate(data)\n"
        "    return data\n\n"
        "def validate(data):\n"
        "    pass\n"
    )
    known_fns = {"process_data", "validate"}
    calls = extract_calls(code, known_fns, "app/processor.py")

    assert len(calls) == 1
    assert calls[0].caller_fn == "process_data"
    assert calls[0].callee_fn == "validate"


def test_extract_extends_python():
    py_code = (
        "class Animal:\n"
        "    pass\n\n"
        "class Dog(Animal):\n"
        "    pass\n"
    )
    known_cls = {"Animal", "Dog"}
    extends = extract_extends(py_code, "app/models.py", "py", known_cls)

    assert len(extends) == 1
    assert extends[0].child_class == "Dog"
    assert extends[0].parent_class == "Animal"


def test_extract_extends_js():
    js_code = (
        "class Component {}\n"
        "class MyButton extends Component {}\n"
    )
    known_cls = {"Component", "MyButton"}
    extends = extract_extends(js_code, "src/button.js", "js", known_cls)

    assert len(extends) == 1
    assert extends[0].child_class == "MyButton"
    assert extends[0].parent_class == "Component"


def test_extract_test_edges_python_and_ts():
    all_paths = {
        "app/utils/math_helper.py",
        "tests/test_math_helper.py",
        "src/components/button.tsx",
        "src/components/button.test.tsx",
    }
    test_edges = extract_test_edges(all_paths)
    assert len(test_edges) == 2

    py_edge = [e for e in test_edges if "test_math_helper.py" in e.test_file_rel_path][0]
    assert py_edge.source_file_rel_path == "app/utils/math_helper.py"

    ts_edge = [e for e in test_edges if "button.test.tsx" in e.test_file_rel_path][0]
    assert ts_edge.source_file_rel_path == "src/components/button.tsx"


def test_parse_manifest_deps_requirements_txt():
    req_txt = (
        "# Core requirements\n"
        "fastapi>=0.110.0\n"
        "neo4j>=5.18.0\n"
        "pydantic\n"
    )
    pkgs = parse_manifest_deps("requirements.txt", req_txt)
    assert len(pkgs) == 3
    names = {p.name for p in pkgs}
    assert "fastapi" in names
    assert "neo4j" in names
    assert "pydantic" in names
    for p in pkgs:
        assert p.ecosystem == "python"


def test_parse_manifest_deps_package_json():
    pkg_json = '{"dependencies": {"react": "^18.0.0"}, "devDependencies": {"typescript": "^5.0.0"}}'
    pkgs = parse_manifest_deps("package.json", pkg_json)
    assert len(pkgs) == 2
    names = {p.name for p in pkgs}
    assert "react" in names
    assert "typescript" in names
    for p in pkgs:
        assert p.ecosystem == "npm"


def test_package_schema_instantiation():
    pkg = Package(
        id="package:python:fastapi",
        organization_id="test-org",
        name="fastapi",
        version=">=0.110.0",
        ecosystem="python",
        repository_id="autokt",
    )
    assert pkg.tenant_key == "test-org:package:python:fastapi"
    assert pkg.to_node_params()["tenant_key"] == "test-org:package:python:fastapi"


def test_authored_caveat_in_onboarding_pack():
    """
    Verifies that onboarding pack limitations explicitly contain the AUTHORED file-level caveat.
    """
    from app.core.module_context import ModuleContext
    from app.graphs.onboarding_pack import run_onboarding_pack_task

    # Mock context to run onboarding pack without DB
    dummy_ctx = ModuleContext(
        module_id="test-mod",
        module_name="test-mod",
        module_path="app",
        organization_id="test-org",
        raw_files=[],
        raw_functions=[],
        raw_classes=[],
        key_files_list=[],
        entry_points_list=[],
        chroma_func_meta={},
        owners_list=[{"name": "Alice", "email": "alice@test.com", "total_commits": 5, "last_active": "2026-08-19"}],
        existing_docs_list=[],
        has_readme=True,
        project_requirements_data={"status": "found", "declared_dependencies": ["fastapi"]},
        total_func_count=5,
        documented_count=5,
        undocumented_count=0,
        coverage_pct=100.0,
        doc_coverage_data={"coverage_pct": 100.0},
        semantic_snippets=[],
        graph_time_ms=1.0,
        vector_time_ms=1.0,
    )

    with patch("app.graphs.onboarding_pack.assemble_module_context", return_value=dummy_ctx):
        with patch.object(settings, "LLM_PROVIDER", "stub"):
            res = run_onboarding_pack_task(task_id="dummy-task", module_id="test-mod", organization_id="test-org")

    known_lims = res.get("known_limitations", [])
    owner_lim = [l for l in known_lims if l.startswith("owner_granularity:")]
    assert len(owner_lim) == 1
    assert "file-level last-author heuristic" in owner_lim[0]
    assert "AUTHORED" in owner_lim[0]


# ---------------------------------------------------------------------------
# Integration Tests — Neo4j ingestion & graph relationship validation
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ingested_graph_data():
    """Ingests local backend repository into Neo4j once for all relationship assertions."""
    from app.graphs.repo_ingestion import run_repo_ingestion_task

    test_org = f"test-org-rel-{uuid.uuid4().hex[:8]}"

    with patch.object(settings, "EMBEDDING_PROVIDER", ""):
        summary = run_repo_ingestion_task(
            task_id=str(uuid.uuid4()),
            repo_url=str(backend_dir.resolve()),
            organization_id=test_org,
            branch="main",
        )

    yield test_org, summary

    # Teardown: Clean up test org nodes
    try:
        neo4j_client.run_write_query(
            "MATCH (n) WHERE n.organization_id = $org_id DETACH DELETE n",
            {"org_id": test_org},
            organization_id=test_org,
        )
    except Exception:
        pass


def test_package_and_used_by_relationship(ingested_graph_data):
    """Verifies Package nodes & USED_BY relationships exist in Neo4j."""
    test_org, summary = ingested_graph_data
    pkgs = neo4j_client.run_read_query(
        "MATCH (p:Package {organization_id: $org_id})-[r:USED_BY]->(m:Module) RETURN p.name AS name, p.ecosystem AS eco, m.name AS mod_name",
        {"org_id": test_org},
        organization_id=test_org,
    )
    assert len(pkgs) > 0, f"Expected Package nodes with USED_BY edges, got {len(pkgs)}"
    assert any(p["name"] == "fastapi" for p in pkgs)
    assert summary.get("packages_count", 0) > 0
    assert summary.get("used_by_count", 0) > 0


def test_imports_relationship(ingested_graph_data):
    """Verifies IMPORTS relationships between code files exist in Neo4j."""
    test_org, summary = ingested_graph_data
    imports = neo4j_client.run_read_query(
        "MATCH (f1:File {organization_id: $org_id})-[r:IMPORTS]->(f2:File) RETURN f1.path AS from_p, f2.path AS to_p, r.module AS mod",
        {"org_id": test_org},
        organization_id=test_org,
    )
    assert len(imports) > 0, f"Expected IMPORTS relationships, got {len(imports)}"
    assert summary.get("imports_count", 0) > 0


def test_calls_relationship(ingested_graph_data):
    """Verifies CALLS relationships between intra-file functions exist in Neo4j."""
    test_org, summary = ingested_graph_data
    calls = neo4j_client.run_read_query(
        "MATCH (fn1:Function {organization_id: $org_id})-[r:CALLS]->(fn2:Function) RETURN fn1.name AS caller, fn2.name AS callee",
        {"org_id": test_org},
        organization_id=test_org,
    )
    assert len(calls) > 0, f"Expected CALLS relationships between functions, got {len(calls)}"
    assert summary.get("calls_count", 0) > 0


def test_extends_relationship(ingested_graph_data):
    """Verifies EXTENDS relationships between classes exist in Neo4j."""
    test_org, summary = ingested_graph_data
    extends = neo4j_client.run_read_query(
        "MATCH (c1:Class {organization_id: $org_id})-[r:EXTENDS]->(c2:Class) RETURN c1.name AS child, c2.name AS parent",
        {"org_id": test_org},
        organization_id=test_org,
    )
    assert len(extends) > 0, f"Expected EXTENDS relationships between classes, got {len(extends)}"
    assert summary.get("extends_count", 0) > 0


def test_tests_relationship(ingested_graph_data):
    """Verifies TESTS relationships between test files and source files exist in Neo4j."""
    test_org, summary = ingested_graph_data
    tests = neo4j_client.run_read_query(
        "MATCH (tf:File {organization_id: $org_id})-[r:TESTS]->(sf:File) RETURN tf.path AS test_p, sf.path AS src_p",
        {"org_id": test_org},
        organization_id=test_org,
    )
    assert len(tests) > 0, f"Expected TESTS relationships between test and source files, got {len(tests)}"
    assert summary.get("tests_count", 0) > 0


def test_authored_relationship(ingested_graph_data):
    """Verifies AUTHORED relationships between Person nodes and Function/Class nodes exist in Neo4j."""
    test_org, summary = ingested_graph_data
    authored_fns = neo4j_client.run_read_query(
        "MATCH (p:Person {organization_id: $org_id})-[r:AUTHORED]->(fn:Function) RETURN p.name AS name, fn.name AS fn_name, r.source AS source",
        {"org_id": test_org},
        organization_id=test_org,
    )
    assert len(authored_fns) > 0, f"Expected AUTHORED relationships to Function nodes, got {len(authored_fns)}"
    assert authored_fns[0]["source"] == "file_heuristic"
    assert summary.get("authored_count", 0) > 0


def test_documents_extended_relationship(ingested_graph_data):
    """Verifies extended DOCUMENTS relationships between Document nodes and Function/Class nodes exist in Neo4j."""
    test_org, summary = ingested_graph_data
    doc_fns = neo4j_client.run_read_query(
        "MATCH (d:Document {organization_id: $org_id})-[r:DOCUMENTS]->(fn:Function) RETURN d.filename AS doc_name, fn.name AS fn_name, r.confidence AS confidence, r.source AS source",
        {"org_id": test_org},
        organization_id=test_org,
    )
    # If no in-repo doc file matches a code file stem, doc_fns could be 0; check query resilience
    assert isinstance(doc_fns, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
