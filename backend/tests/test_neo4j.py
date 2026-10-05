import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
import pytest

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.db.neo4j_client import (
    neo4j_client,
    Neo4jConstraintError,
)
from app.models.schemas import (
    Repository,
    Module,
    File,
    Person,
    Topic,
)

TEST_ORG_ID = f"test-org-{uuid.uuid4().hex[:8]}"
OTHER_ORG_ID = f"other-org-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def setup_neo4j_schema():
    """Ensure Neo4j client is connected and constraints are created before running tests."""
    neo4j_client.connect()
    neo4j_client.create_constraints()
    yield
    # Final cleanup after module tests complete
    try:
        cleanup_query = "MATCH (n) WHERE n.organization_id = $organization_id DETACH DELETE n"
        neo4j_client.run_write_query(cleanup_query, organization_id=TEST_ORG_ID)
        neo4j_client.run_write_query(cleanup_query, organization_id=OTHER_ORG_ID)
    except Exception:
        pass


def test_neo4j_graph_crud_and_relationships():
    """
    Test node creation, relationship tagging (structural vs semantic),
    and property assertion.
    """
    repo = Repository(
        id="repo-1",
        organization_id=TEST_ORG_ID,
        name="autokt-core",
        url="https://github.com/org/autokt-core",
        default_branch="main",
        last_synced_at=datetime.now(timezone.utc),
    )
    module = Module(
        id="mod-1",
        organization_id=TEST_ORG_ID,
        repository_id=repo.id,
        name="graph_ingestion",
        path="backend/app/graphs",
        description="Graph ingestion module",
    )
    file_node = File(
        id="file-1",
        organization_id=TEST_ORG_ID,
        module_id=module.id,
        path="backend/app/graphs/builder.py",
        language="python",
        last_modified_at=datetime.now(timezone.utc),
    )
    person = Person(
        id="person-1",
        organization_id=TEST_ORG_ID,
        name="Alice Developer",
        email="alice@example.com",
        github_handle="alicedev",
    )
    topic = Topic(
        id="topic-1",
        organization_id=TEST_ORG_ID,
        name="AST Parsing",
        description="Abstract Syntax Tree extraction concepts",
        keywords=["ast", "parser", "python"],
    )

    try:
        # 1. Create Nodes
        create_repo_cypher = """
        CREATE (r:Repository $repo_params)
        RETURN r
        """
        neo4j_client.run_write_query(
            create_repo_cypher,
            {"repo_params": repo.to_node_params()},
            organization_id=TEST_ORG_ID,
        )

        create_mod_cypher = """
        CREATE (m:Module $mod_params)
        RETURN m
        """
        neo4j_client.run_write_query(
            create_mod_cypher,
            {"mod_params": module.to_node_params()},
            organization_id=TEST_ORG_ID,
        )

        create_file_cypher = """
        CREATE (f:File $file_params)
        RETURN f
        """
        neo4j_client.run_write_query(
            create_file_cypher,
            {"file_params": file_node.to_node_params()},
            organization_id=TEST_ORG_ID,
        )

        create_person_cypher = """
        CREATE (p:Person $person_params)
        RETURN p
        """
        neo4j_client.run_write_query(
            create_person_cypher,
            {"person_params": person.to_node_params()},
            organization_id=TEST_ORG_ID,
        )

        create_topic_cypher = """
        CREATE (t:Topic $topic_params)
        RETURN t
        """
        neo4j_client.run_write_query(
            create_topic_cypher,
            {"topic_params": topic.to_node_params()},
            organization_id=TEST_ORG_ID,
        )

        # 2. Create Structural Relationships
        part_of_cypher = """
        MATCH (m:Module {tenant_key: $mod_key}), (r:Repository {tenant_key: $repo_key})
        CREATE (m)-[:PART_OF]->(r)
        """
        neo4j_client.run_write_query(
            part_of_cypher,
            {"mod_key": module.tenant_key, "repo_key": repo.tenant_key},
            organization_id=TEST_ORG_ID,
        )

        file_part_of_cypher = """
        MATCH (f:File {tenant_key: $file_key}), (m:Module {tenant_key: $mod_key})
        CREATE (f)-[:PART_OF]->(m)
        """
        neo4j_client.run_write_query(
            file_part_of_cypher,
            {"file_key": file_node.tenant_key, "mod_key": module.tenant_key},
            organization_id=TEST_ORG_ID,
        )

        owns_cypher = """
        MATCH (p:Person {tenant_key: $person_key}), (m:Module {tenant_key: $mod_key})
        CREATE (p)-[:OWNS {commit_count: $commit_count, share: $share}]->(m)
        """
        neo4j_client.run_write_query(
            owns_cypher,
            {
                "person_key": person.tenant_key,
                "mod_key": module.tenant_key,
                "commit_count": 15,
                "share": 0.75,
            },
            organization_id=TEST_ORG_ID,
        )

        # 3. Create Semantic Relationship (with source & confidence)
        semantic_cypher = """
        MATCH (m:Module {tenant_key: $mod_key}), (t:Topic {tenant_key: $topic_key})
        CREATE (m)-[:REQUIRES_KNOWLEDGE_OF {source: $source, confidence: $confidence}]->(t)
        """
        neo4j_client.run_write_query(
            semantic_cypher,
            {
                "mod_key": module.tenant_key,
                "topic_key": topic.tenant_key,
                "source": "llm_inferred",
                "confidence": 0.8,
            },
            organization_id=TEST_ORG_ID,
        )

        # 4. Read back & verify properties
        read_cypher = """
        MATCH (p:Person {tenant_key: $person_key})-[o:OWNS]->(m:Module {tenant_key: $mod_key})-[:PART_OF]->(r:Repository {tenant_key: $repo_key})
        MATCH (m)-[rk:REQUIRES_KNOWLEDGE_OF]->(t:Topic {tenant_key: $topic_key})
        RETURN p.name AS person_name, o.commit_count AS commit_count, o.share AS share,
               m.name AS module_name, rk.source AS sem_source, rk.confidence AS sem_confidence,
               t.name AS topic_name
        """
        results = neo4j_client.run_read_query(
            read_cypher,
            {
                "person_key": person.tenant_key,
                "mod_key": module.tenant_key,
                "repo_key": repo.tenant_key,
                "topic_key": topic.tenant_key,
            },
            organization_id=TEST_ORG_ID,
        )

        assert len(results) == 1, "Graph query should return 1 matching path"
        row = results[0]
        assert row["person_name"] == "Alice Developer"
        assert row["commit_count"] == 15
        assert row["share"] == 0.75
        assert row["module_name"] == "graph_ingestion"
        assert row["sem_source"] == "llm_inferred"
        assert abs(row["sem_confidence"] - 0.8) < 1e-5
        assert row["topic_name"] == "AST Parsing"

    finally:
        # Cleanup test organization nodes
        cleanup_cypher = "MATCH (n) WHERE n.organization_id = $organization_id DETACH DELETE n"
        neo4j_client.run_write_query(cleanup_cypher, organization_id=TEST_ORG_ID)


def test_neo4j_tenant_isolation_failure_path():
    """
    Failure path test: querying with a different organization_id returns NO data.
    """
    repo = Repository(
        id="repo-iso-1",
        organization_id=TEST_ORG_ID,
        name="tenant-isolated-repo",
        url="https://github.com/org/isolated",
    )

    try:
        create_cypher = "CREATE (r:Repository $repo_params) RETURN r"
        neo4j_client.run_write_query(
            create_cypher,
            {"repo_params": repo.to_node_params()},
            organization_id=TEST_ORG_ID,
        )

        # Query using OTHER_ORG_ID
        query_cypher = "MATCH (r:Repository) WHERE r.organization_id = $organization_id RETURN r"
        other_results = neo4j_client.run_read_query(
            query_cypher,
            organization_id=OTHER_ORG_ID,
        )

        assert len(other_results) == 0, "Tenant isolation failure: query returned nodes from another organization!"

    finally:
        cleanup_cypher = "MATCH (n) WHERE n.organization_id = $organization_id DETACH DELETE n"
        neo4j_client.run_write_query(cleanup_cypher, organization_id=TEST_ORG_ID)


def test_neo4j_constraint_violation():
    """
    Constraint violation test: inserting a duplicate tenant_key raises Neo4jConstraintError.
    """
    repo = Repository(
        id="repo-dup-1",
        organization_id=TEST_ORG_ID,
        name="original-repo",
        url="https://github.com/org/original",
    )

    try:
        create_cypher = "CREATE (r:Repository $repo_params) RETURN r"
        neo4j_client.run_write_query(
            create_cypher,
            {"repo_params": repo.to_node_params()},
            organization_id=TEST_ORG_ID,
        )

        # Attempt to insert a second node with exact same tenant_key
        duplicate_repo = Repository(
            id="repo-dup-1",
            organization_id=TEST_ORG_ID,
            name="duplicate-repo",
            url="https://github.com/org/duplicate",
        )

        with pytest.raises(Neo4jConstraintError) as exc_info:
            neo4j_client.run_write_query(
                create_cypher,
                {"repo_params": duplicate_repo.to_node_params()},
                organization_id=TEST_ORG_ID,
            )

        assert "already exists" in str(exc_info.value).lower() or "constraint" in str(exc_info.value).lower()

    finally:
        cleanup_cypher = "MATCH (n) WHERE n.organization_id = $organization_id DETACH DELETE n"
        neo4j_client.run_write_query(cleanup_cypher, organization_id=TEST_ORG_ID)


if __name__ == "__main__":
    pytest.main([__file__, "-s"])
