import sys
import uuid
import time
import chromadb
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.main import app
from app.core.config import settings
from app.db.neo4j_client import neo4j_client
from app.db.postgres_client import SessionLocal
from app.models.db_models import Task, User
from app.core.constants import SYSTEM_USER_ID, ensure_system_user

TEST_ORG_ID = f"test-org-ingest-{uuid.uuid4().hex[:8]}"
TEST_REPO_PATH = str(backend_dir.resolve()) # autokt/backend directory

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def setup_environment_and_db():
    """Ensure database connection, system user sentinel, and config environment."""
    # Enable local path testing
    settings.REPO_LOCAL_ALLOWED_ROOT = str(Path(TEST_REPO_PATH).parent.resolve())

    try:
        neo4j_client.connect()
        neo4j_client.create_constraints()
    except Exception:
        pass

    with SessionLocal() as db:
        ensure_system_user(db)

    yield

    # Teardown: Clean up Neo4j test nodes for TEST_ORG_ID
    try:
        neo4j_client.run_write_query(
            "MATCH (n) WHERE n.organization_id = $org_id DETACH DELETE n",
            {"org_id": TEST_ORG_ID},
            organization_id=TEST_ORG_ID,
        )
    except Exception:
        pass

    # Teardown: Clean up task rows in Postgres
    with SessionLocal() as db:
        try:
            db.query(Task).filter(Task.payload["organization_id"].astext == TEST_ORG_ID).delete(synchronize_session=False)
            db.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_post_repos_invalid_scheme_raises_400():
    """POST /repos with invalid scheme or forbidden file:// path raises HTTP 400."""
    response = client.post(
        "/repos",
        json={
            "repo_url": "file:///etc/passwd",
            "organization_id": TEST_ORG_ID,
            "branch": "main",
        },
    )
    assert response.status_code == 400
    assert "Invalid URL scheme" in response.json()["detail"] or "URI scheme" in response.json()["detail"] or "disabled" in response.json()["detail"]


def test_sanitize_url_removes_embedded_credentials_from_git_error_text():
    """sanitize_url() must strip embedded tokens from realistic GitPython exception strings."""
    from app.graphs.repo_ingestion import sanitize_url

    # Realistic GitPython exception message embedding token in URL
    git_error_text = (
        "Cmd('git') failed due to: exit code(128)\n"
        "  cmdline: git clone -v -- https://ghp_secret_token_abcdef123456@github.com/my-org/private-repo.git\n"
        "  stderr: 'fatal: unable to access https://ghp_secret_token_abcdef123456@github.com/my-org/private-repo.git: 403 Forbidden'"
    )

    cleaned = sanitize_url(git_error_text)
    assert "ghp_secret_token_abcdef123456" not in cleaned
    assert "https://github.com/my-org/private-repo.git" in cleaned


def test_extract_repo_name_scp_and_urls():
    """extract_repo_name() correctly extracts repository name from SCP SSH and HTTPS URLs."""
    from app.graphs.repo_ingestion import extract_repo_name

    assert extract_repo_name("git@github.com:my-org/my-cool-repo.git") == "my-cool-repo"
    assert extract_repo_name("https://github.com/my-org/another-repo.git") == "another-repo"
    assert extract_repo_name("c:/Users/admin/Desktop/hackathon/autokt") == "autokt"


def test_validate_repo_url_accepts_scp_ssh_syntax():
    """validate_repo_url() accepts standard SCP-like SSH clone URLs (git@github.com:org/repo.git)."""
    from app.graphs.repo_ingestion import validate_repo_url
    assert validate_repo_url("git@github.com:my-org/my-repo.git") is True
    assert validate_repo_url("https://github.com/my-org/my-repo.git") is True


from app.db.chroma_client import chroma_client

def test_repo_ingestion_end_to_end_and_cleanup():
    """
    POST /repos enqueues task -> Background runner parses repo ->
    Neo4j nodes created via MERGE -> GET /repos/tasks/{task_id} returns status.
    Confirms disk cleanup removed temp directory post-task.
    """
    with patch.object(settings, "EMBEDDING_PROVIDER", ""):
        # 1. Trigger ingestion
        response = client.post(
            "/repos",
            json={
                "repo_url": TEST_REPO_PATH,
                "organization_id": TEST_ORG_ID,
                "branch": "main",
            },
        )
        assert response.status_code == 202
        data = response.json()
    assert "task_id" in data
    assert data["status"] == "pending"

    task_id = data["task_id"]

    # 2. Poll status until background task completes (up to 30s)
    completed = False
    status_data = {}
    for _ in range(30):
        time.sleep(1)
        poll_resp = client.get(f"/repos/tasks/{task_id}")
        assert poll_resp.status_code == 200
        status_data = poll_resp.json()
        if status_data["status"] in ("completed", "completed_without_embeddings", "failed"):
            completed = True
            break

    assert completed, f"Task did not complete within timeout. Last status: {status_data}"
    # Accept both success variants: 'completed' (embeddings written) or
    # 'completed_without_embeddings' (model unconfigured / unavailable).
    assert status_data["status"] in ("completed", "completed_without_embeddings"), \
        f"Expected successful completion, got: {status_data['status']}"
    result = status_data["result"]
    assert result is not None
    assert result["modules_count"] > 0
    assert result["files_count"] > 0
    # chunks_count > 0 when embedding model is active; 0 when provider is unconfigured
    assert result["chunks_count"] >= 0

    # 3. Verify Neo4j nodes exist
    records = neo4j_client.run_read_query(
        "MATCH (r:Repository {organization_id: $org_id}) RETURN r",
        {"org_id": TEST_ORG_ID},
        organization_id=TEST_ORG_ID,
    )
    assert len(records) == 1
    assert records[0]["r"]["organization_id"] == TEST_ORG_ID

    # 4. Verify clone_dir was cleaned up post-task
    tmp_clone_dir = Path("tmp_repos") / TEST_ORG_ID / "backend"
    assert not tmp_clone_dir.exists(), f"Temporary clone directory '{tmp_clone_dir}' was not cleaned up!"


def test_repo_ingestion_idempotency():
    """Re-ingesting the exact same repo runs MERGE queries cleanly without duplicate nodes."""
    with patch.object(settings, "EMBEDDING_PROVIDER", ""):
        # First query count of Repository nodes
        initial_records = neo4j_client.run_read_query(
            "MATCH (r:Repository {organization_id: $org_id}) RETURN count(r) as count",
            {"org_id": TEST_ORG_ID},
            organization_id=TEST_ORG_ID,
        )
        initial_count = initial_records[0]["count"]

        # Re-trigger ingestion
        response = client.post(
            "/repos",
            json={
                "repo_url": TEST_REPO_PATH,
                "organization_id": TEST_ORG_ID,
                "branch": "main",
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]

        # Poll completion
        for _ in range(30):
            time.sleep(1)
            poll_resp = client.get(f"/repos/tasks/{task_id}")
            if poll_resp.json()["status"] in ("completed", "completed_without_embeddings"):
                break

    # Verify count of Repository nodes remains identical (no duplicate)
    re_records = neo4j_client.run_read_query(
        "MATCH (r:Repository {organization_id: $org_id}) RETURN count(r) as count",
        {"org_id": TEST_ORG_ID},
        organization_id=TEST_ORG_ID,
    )
    assert re_records[0]["count"] == initial_count


def test_ast_chunker_python_basic():
    """Unit test for chunk_file() on a short Python snippet verifying AST chunk properties."""
    from app.core.ast_chunker import chunk_file

    snippet = (
        "def process_data(val: int) -> int:\n"
        "    \"\"\"Doubles input integer.\"\"\"\n"
        "    return val * 2\n"
    )
    chunks = chunk_file(snippet, "app/utils.py", "testrepo:file:app/utils.py", "py")
    assert len(chunks) >= 1
    ast_c = [c for c in chunks if c.chunking_method == "ast"]
    assert len(ast_c) == 1
    assert ast_c[0].function_name == "process_data"
    assert ast_c[0].start_line > 0
    assert ast_c[0].end_line >= ast_c[0].start_line
    assert ast_c[0].chunk_type == "function"
    assert ast_c[0].is_documented is True
    assert "# File: app/utils.py" in ast_c[0].text
    assert "# Function: process_data" in ast_c[0].text
    assert "Doubles input integer." in ast_c[0].text


def test_ast_chunker_fallback_on_syntax_error():
    """chunk_file() with invalid syntax does not crash and falls back to fixed-window chunking."""
    from app.core.ast_chunker import chunk_file

    invalid_code = "def broken_syntax(:\n    pass\n"
    chunks = chunk_file(invalid_code, "app/broken.py", "testrepo:file:app/broken.py", "py")
    assert len(chunks) >= 1
    for c in chunks:
        assert c.chunking_method == "fixed_window"


def test_ast_chunker_giant_function_subsplit():
    """Single Python function exceeding MAX_AST_CHUNK_CHARS is sub-split with chunking_method='ast'."""
    from app.core.ast_chunker import chunk_file, MAX_AST_CHUNK_CHARS

    body_lines = [f"    x_{i} = {i} * 100" for i in range(500)]
    giant_code = "def giant_function():\n" + "\n".join(body_lines) + "\n"

    chunks = chunk_file(giant_code, "app/giant.py", "testrepo:file:app/giant.py", "py")
    assert len(chunks) > 1
    for c in chunks:
        assert c.chunking_method == "ast"
        assert c.function_name == "giant_function"
        assert c.chunk_type == "function"
        assert c.is_documented is False


def test_repo_ingestion_function_nodes_in_neo4j():
    """Integration test: verifies Function and Class nodes created in Neo4j after repo ingestion."""
    from app.graphs.repo_ingestion import run_repo_ingestion_task

    records = neo4j_client.run_read_query(
        "MATCH (fn:Function {organization_id: $org_id}) RETURN fn",
        {"org_id": TEST_ORG_ID},
        organization_id=TEST_ORG_ID,
    )
    if len(records) == 0:
        run_repo_ingestion_task(
            task_id=str(uuid.uuid4()),
            repo_url=TEST_REPO_PATH,
            organization_id=TEST_ORG_ID,
            branch="main",
        )

    records = neo4j_client.run_read_query(
        "MATCH (fn:Function {organization_id: $org_id})-[:PART_OF]->(f:File) "
        "RETURN fn.name as name, fn.start_line as start_line, fn.end_line as end_line, f.path as path",
        {"org_id": TEST_ORG_ID},
        organization_id=TEST_ORG_ID,
    )
    assert len(records) > 0
    first_fn = records[0]
    assert first_fn["name"] != ""
    assert isinstance(first_fn["start_line"], int)
    assert isinstance(first_fn["end_line"], int)
    assert first_fn["start_line"] > 0
    assert first_fn["end_line"] >= first_fn["start_line"]


def test_chroma_code_chunk_metadata_schema():
    """
    Verifies all 21 code_chunks metadata fields are present and correctly typed
    after a real ingestion. Skips if embedding provider is not active.
    """
    from datetime import datetime
    from app.db.chroma_client import chroma_client

    if not settings.EMBEDDING_PROVIDER:
        pytest.skip("Skipping: EMBEDDING_PROVIDER is unconfigured")

    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
        result = chroma_client.get_documents("code_chunks", organization_id=TEST_ORG_ID)
    except Exception:
        chroma_client.client = chromadb.EphemeralClient()
        chroma_client.ensure_collections()
        try:
            result = chroma_client.get_documents("code_chunks", organization_id=TEST_ORG_ID)
        except Exception:
            pytest.skip("Chroma DB unavailable for test")
    if not result.get("ids"):
        pytest.skip("No code chunks found for TEST_ORG_ID in Chroma DB")

    meta = result["metadatas"][0]

    # Existing fields
    for field in ("chunking_method", "start_line", "end_line", "function_name",
                  "class_name", "parent_class", "file_path",
                  "organization_id", "module_id", "source_type"):
        assert field in meta, f"Missing existing field: {field}"

    # New string fields
    str_fields = ("file_id", "repository_id", "chunk_type", "language",
                  "tenant_key", "content_hash", "commit_sha",
                  "last_modified_date", "ingested_at",
                  "last_author_email", "last_author_name", "visibility")
    for field in str_fields:
        assert field in meta, f"Missing new string field: {field}"
        assert isinstance(meta[field], str), f"{field} must be str, got {type(meta[field])}"

    assert "commit_count" in meta and isinstance(meta["commit_count"], int)
    assert "token_count" in meta and isinstance(meta["token_count"], int)
    assert "is_documented" in meta and isinstance(meta["is_documented"], bool)

    # chunk_type must be one of 4 valid values
    assert meta["chunk_type"] in ("function", "method", "class_body", "fixed_window")
    # content_hash must be a 64-char hex sha256
    assert len(meta["content_hash"]) == 64
    # ingested_at must parse as ISO datetime
    datetime.fromisoformat(meta["ingested_at"])


def test_list_repositories_endpoint():
    """Verify GET /repos returns structured repository list from Neo4j."""
    response = client.get("/repos")
    assert response.status_code == 200
    data = response.json()
    assert "repositories" in data
    assert "total" in data
    assert isinstance(data["repositories"], list)
    assert data["total"] == len(data["repositories"])

    if data["total"] > 0:
        first = data["repositories"][0]
        assert "id" in first
        assert "name" in first
        assert "organization_id" in first
        assert "modules_count" in first
        assert "files_count" in first
        assert "docs_count" in first


def test_list_repositories_filter_by_org():
    """Verify GET /repos?organization_id=... filters to matching tenant repositories."""
    response = client.get("/repos?organization_id=RAG")
    assert response.status_code == 200
    data = response.json()
    assert "repositories" in data
    for repo in data["repositories"]:
        assert repo["organization_id"] == "RAG"


if __name__ == "__main__":
    pytest.main([__file__, "-s"])


