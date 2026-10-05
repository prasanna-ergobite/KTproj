"""
Production-Grade Neo4j Graph Database Client for AutoKT backend.
Provides multi-tenant connection pooling, managed transaction retries,
typed exception handling, composite/tenant_key constraints, and logging.
"""

import logging
import time
from typing import Any, Dict, List, Optional
from neo4j import GraphDatabase, Driver, Session, ManagedTransaction
from neo4j.exceptions import (
    Neo4jError,
    ConstraintError,
    ServiceUnavailable,
    SessionExpired,
    DriverError,
)

from app.core.config import settings

logger = logging.getLogger("autokt.neo4j")

# Node labels managed in the AutoKT code knowledge graph
NODE_LABELS = [
    "Repository",
    "Module",
    "File",
    "Person",
    "Document",
    "KTSession",
    "Decision",
    "Risk",
    "Dependency",
    "Topic",
    "Function",
    "Class",
    "Package",
]


class Neo4jClientError(Exception):
    """Base exception for all Neo4j client errors."""
    pass


class Neo4jConnectionError(Neo4jClientError):
    """Raised when connection to Neo4j database fails or times out."""
    pass


class Neo4jQueryError(Neo4jClientError):
    """Raised when a Cypher query fails execution."""
    pass


class Neo4jConstraintError(Neo4jQueryError):
    """Raised when a Neo4j database constraint or uniqueness check is violated."""
    pass


class Neo4jClient:
    """
    Neo4j Graph Database Client wrapping the official python driver.
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        max_connection_pool_size: int = 50,
        connection_timeout: float = 30.0,
        max_transaction_retry_time: float = 30.0,
    ):
        self.uri = uri or settings.NEO4J_URI
        self.user = user or settings.NEO4J_USER
        self.password = password or settings.NEO4J_PASSWORD
        self.max_connection_pool_size = max_connection_pool_size
        self.connection_timeout = connection_timeout
        self.max_transaction_retry_time = max_transaction_retry_time
        self.driver: Optional[Driver] = None

    def connect(self) -> None:
        """
        Instantiate Neo4j driver with connection pooling and verify connectivity.
        """
        if self.driver is not None:
            return

        try:
            logger.info("Connecting to Neo4j database at %s...", self.uri)
            self.driver = GraphDatabase.driver(
                self.uri,
                auth=(self.user, self.password),
                max_connection_pool_size=self.max_connection_pool_size,
                connection_timeout=self.connection_timeout,
                max_transaction_retry_time=self.max_transaction_retry_time,
            )
            self.driver.verify_connectivity()
            logger.info("Successfully connected to Neo4j database.")
        except (ServiceUnavailable, DriverError, Exception) as exc:
            logger.error("Failed to connect to Neo4j database: %s", exc)
            self.driver = None
            raise Neo4jConnectionError(f"Could not connect to Neo4j at {self.uri}: {exc}") from exc

    def close(self) -> None:
        """
        Safely close active Neo4j driver connection pool.
        """
        if self.driver is not None:
            logger.info("Closing Neo4j driver connection pool...")
            try:
                self.driver.close()
                logger.info("Neo4j driver connection pool closed.")
            except Exception as exc:
                logger.warning("Error closing Neo4j driver: %s", exc)
            finally:
                self.driver = None

    def create_constraints(self) -> None:
        """
        Idempotently create tenant_key uniqueness constraints and organization_id range indexes
        for all 10 domain node labels (Neo4j Community Edition compatible).
        """
        if self.driver is None:
            self.connect()

        logger.info("Initializing Neo4j schema constraints and indexes for %d node labels...", len(NODE_LABELS))
        
        with self.driver.session() as session:
            for label in NODE_LABELS:
                # 1. Uniqueness constraint on tenant_key
                constraint_query = (
                    f"CREATE CONSTRAINT constraint_{label.lower()}_tenant_key IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.tenant_key IS UNIQUE"
                )
                # 2. Range index on organization_id
                index_query = (
                    f"CREATE INDEX index_{label.lower()}_organization_id IF NOT EXISTS "
                    f"FOR (n:{label}) ON (n.organization_id)"
                )
                try:
                    session.execute_write(lambda tx: tx.run(constraint_query))
                    session.execute_write(lambda tx: tx.run(index_query))
                    logger.debug("Configured constraint & index for label ':%s'", label)
                except Exception as exc:
                    logger.error("Error creating constraint/index for label ':%s': %s", label, exc)
                    raise Neo4jQueryError(f"Failed to create schema constraint for {label}: {exc}") from exc

            # Composite index for Document identity lookup (organization_id + relative_path)
            composite_index_query = (
                "CREATE INDEX index_document_org_relpath IF NOT EXISTS "
                "FOR (d:Document) ON (d.organization_id, d.relative_path)"
            )
            try:
                session.execute_write(lambda tx: tx.run(composite_index_query))
                logger.debug("Configured composite index for Document (organization_id, relative_path)")
            except Exception as exc:
                logger.warning("Composite index creation notice: %s", exc)

            # Composite index for Document identity lookup (organization_id + id) for business docs
            doc_id_index_query = (
                "CREATE INDEX index_document_org_docid IF NOT EXISTS "
                "FOR (d:Document) ON (d.organization_id, d.id)"
            )
            try:
                session.execute_write(lambda tx: tx.run(doc_id_index_query))
                logger.debug("Configured composite index for Document (organization_id, id)")
            except Exception as exc:
                logger.warning("Document org_id composite index creation notice: %s", exc)

            # Composite index for Package lookup (name + ecosystem)
            package_index_query = (
                "CREATE INDEX index_package_name_ecosystem IF NOT EXISTS "
                "FOR (p:Package) ON (p.name, p.ecosystem)"
            )
            try:
                session.execute_write(lambda tx: tx.run(package_index_query))
                logger.debug("Configured composite index for Package (name, ecosystem)")
            except Exception as exc:
                logger.warning("Package index creation notice: %s", exc)

        logger.info("Neo4j schema constraints and indexes successfully initialized.")

    def run_read_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        organization_id: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Execute a read-only Cypher query inside a managed transaction with retries.
        Requires organization_id for multi-tenant isolation guarding.
        """
        if not organization_id or not organization_id.strip():
            raise ValueError("organization_id parameter is required for all Neo4j queries to enforce multi-tenancy.")

        if self.driver is None:
            self.connect()

        params = dict(parameters or {})
        params["organization_id"] = organization_id.strip()

        start_time = time.perf_counter()
        try:
            with self.driver.session() as session:
                records = session.execute_read(self._execute_tx, query, params)
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.debug(
                    "Read query executed in %.2fms (records=%d, org_id=%s)",
                    duration_ms,
                    len(records),
                    organization_id,
                )
                return records
        except ConstraintError as exc:
            raise Neo4jConstraintError(f"Neo4j constraint violation: {exc.message}") from exc
        except Neo4jError as exc:
            raise Neo4jQueryError(f"Cypher read query error: {exc.message}") from exc
        except (ServiceUnavailable, SessionExpired, DriverError) as exc:
            raise Neo4jConnectionError(f"Neo4j connection error during read query: {exc}") from exc

    def run_write_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        organization_id: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Execute a write Cypher query inside a managed transaction with retries.
        Requires organization_id for multi-tenant isolation guarding.
        """
        if not organization_id or not organization_id.strip():
            raise ValueError("organization_id parameter is required for all Neo4j queries to enforce multi-tenancy.")

        if self.driver is None:
            self.connect()

        params = dict(parameters or {})
        params["organization_id"] = organization_id.strip()

        start_time = time.perf_counter()
        try:
            with self.driver.session() as session:
                records = session.execute_write(self._execute_tx, query, params)
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.debug(
                    "Write query executed in %.2fms (records=%d, org_id=%s)",
                    duration_ms,
                    len(records),
                    organization_id,
                )
                return records
        except ConstraintError as exc:
            raise Neo4jConstraintError(f"Neo4j constraint violation: {exc.message}") from exc
        except Neo4jError as exc:
            raise Neo4jQueryError(f"Cypher write query error: {exc.message}") from exc
        except (ServiceUnavailable, SessionExpired, DriverError) as exc:
            raise Neo4jConnectionError(f"Neo4j connection error during write query: {exc}") from exc

    @staticmethod
    def _execute_tx(tx: ManagedTransaction, query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Transaction callback helper converting Neo4j Records into list of python dictionaries.
        """
        result = tx.run(query, params)
        return [record.data() for record in result]


# Global Neo4j client instance singleton
neo4j_client = Neo4jClient()
