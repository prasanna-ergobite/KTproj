"""
ChromaDB Vector Store Client for AutoKT backend.
Provides multi-tenant vector collection management with typed exceptions,
tenant-scoped IDs, validated metadata, and production-safe lifecycle hooks.

Backend: chromadb/chroma:0.5.0 (SQLite embedded storage, HttpClient mode).
API target: chromadb>=0.5.0,<1.0.0
"""

import logging
import time
from typing import Any, Dict, List, Optional

import chromadb
from chromadb import Collection
from chromadb.errors import ChromaError

from app.core.config import settings

logger = logging.getLogger("autokt.chroma")

# Collections managed by this client
COLLECTION_NAMES = [
    "docs_chunks",       # Chunked design docs, wikis, notes
    "transcript_chunks", # Chunked meeting transcripts or audio outputs
    "code_chunks",       # Chunked source code or docstrings
]

# Sentinel module_id value for documents not yet linked to a module
MODULE_ID_UNLINKED: Optional[str] = None


# ---------------------------------------------------------------------------
# Typed Exception Hierarchy
# ---------------------------------------------------------------------------

class ChromaClientError(Exception):
    """Base exception for all ChromaDB client errors."""
    pass


class ChromaConnectionError(ChromaClientError):
    """Raised when connection to ChromaDB server fails."""
    pass


class ChromaCollectionError(ChromaClientError):
    """Raised when a requested collection does not exist or cannot be accessed."""
    pass


class ChromaQueryError(ChromaClientError):
    """Raised when an add, query, or delete operation fails."""
    pass


class ChromaValidationError(ChromaClientError):
    """
    Raised when caller-supplied parameters fail pre-flight validation.
    E.g. empty/missing organization_id that would produce broken scoped IDs.
    """
    pass


# ---------------------------------------------------------------------------
# ChromaClient
# ---------------------------------------------------------------------------

class ChromaClient:
    """
    Multi-tenant ChromaDB HTTP client for AutoKT backend.

    All document IDs are stored as scoped IDs (f"{organization_id}:{doc_id}")
    to prevent cross-tenant collisions at the collection level.
    All returned ID lists are unscoped before being handed to callers.

    Batch size note (future ingestion): SQLite backend enforces ~5,461 items
    per add() call. Chunk into <=5,000 items for production ingestion.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ):
        self.host = host or settings.CHROMA_HOST
        self.port = port or settings.CHROMA_PORT
        self.client: Optional[chromadb.HttpClient] = None
        self._collections: Dict[str, Collection] = {}

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def connect(self) -> None:
        """
        Connect to ChromaDB HTTP server and verify with a single heartbeat().
        No retry-with-backoff needed: Docker Compose guarantees readiness via
        'depends_on: chroma: condition: service_healthy' before backend starts.
        Uses chromadb>=1.0.0 HttpClient (v2 REST API). ssl=False for plain HTTP.
        Raises ChromaConnectionError on failure.
        """
        if self.client is not None:
            return
        try:
            logger.info("Connecting to ChromaDB at %s:%s...", self.host, self.port)
            self.client = chromadb.HttpClient(host=self.host, port=self.port, ssl=False)
            self.client.heartbeat()
            logger.info("ChromaDB connection established successfully.")
        except Exception as exc:
            self.client = None
            logger.error("Failed to connect to ChromaDB: %s", exc)
            raise ChromaConnectionError(
                f"Could not connect to ChromaDB at {self.host}:{self.port}: {exc}"
            ) from exc

    def ensure_collections(self) -> None:
        """
        Idempotently create all three collections if they don't exist.
        In chromadb>=1.0.0, embeddings are passed at add() time, so no
        embedding_function is registered on the collection — embeddings
        are always pre-computed by the caller, preventing any model download.
        """
        if self.client is None:
            self.connect()

        logger.info("Ensuring ChromaDB collections exist: %s", COLLECTION_NAMES)
        for name in COLLECTION_NAMES:
            try:
                col = self.client.get_or_create_collection(name=name)
                self._collections[name] = col
                logger.debug("Collection ready: '%s'", name)
            except Exception as exc:
                logger.error("Failed to initialize collection '%s': %s", name, exc)
                raise ChromaCollectionError(
                    f"Could not get or create collection '{name}': {exc}"
                ) from exc

        logger.info("All ChromaDB collections are ready.")

    def close(self) -> None:
        """
        ChromaDB HttpClient has no explicit close(). Reset internal state
        to allow reconnection if needed.
        """
        self.client = None
        self._collections.clear()
        logger.info("ChromaDB client connection released.")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _validate_organization_id(organization_id: Any) -> str:
        """
        Raise ChromaValidationError if organization_id is falsy or not a non-empty string.
        Must be called before any ID scoping or metadata injection.
        """
        if not organization_id or not str(organization_id).strip():
            raise ChromaValidationError(
                "organization_id must be a non-empty string for all ChromaDB operations. "
                "An empty value would produce broken scoped IDs (e.g. ':doc123') "
                "and silently break tenant isolation."
            )
        return str(organization_id).strip()

    @staticmethod
    def _scoped_id(organization_id: str, doc_id: str) -> str:
        """Namespace a document ID as 'organization_id:doc_id' for storage."""
        return f"{organization_id}:{doc_id}"

    @staticmethod
    def _unscoped_id(scoped_id: str) -> str:
        """Strip the organization_id prefix from a stored scoped ID."""
        return scoped_id.split(":", 1)[1] if ":" in scoped_id else scoped_id

    @staticmethod
    def _build_where(
        organization_id: str,
        extra_filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build a Chroma-compatible 'where' clause.
        Chroma 0.5.x does NOT implicitly AND multiple top-level keys.
        Two or more clauses must use explicit {"$and": [...]}.
        """
        base = {"organization_id": {"$eq": organization_id}}
        if not extra_filter:
            return base
        if "$and" in extra_filter and isinstance(extra_filter["$and"], list):
            return {"$and": [base] + extra_filter["$and"]}
        return {"$and": [base, extra_filter]}

    def _get_collection(self, collection_name: str) -> Collection:
        """Return an active Collection or raise ChromaCollectionError."""
        if self.client is None:
            self.connect()
        try:
            col = self.client.get_or_create_collection(name=collection_name)
            self._collections[collection_name] = col
            return col
        except Exception as exc:
            raise ChromaCollectionError(
                f"Collection '{collection_name}' could not be accessed: {exc}"
            ) from exc

    # -----------------------------------------------------------------------
    # Core Operations
    # -----------------------------------------------------------------------

    def add_documents(
        self,
        collection_name: str,
        documents: List[str],
        embeddings: List[List[float]],
        ids: List[str],
        organization_id: str,
        module_id: Optional[str] = None,
        source_type: str = "doc",
        project_id: str = "",
        extra_metadata: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        Add pre-computed embeddings to a named collection.

        - organization_id is validated non-empty before any processing (Gap 1).
        - All IDs are scoped as '{organization_id}:{doc_id}' before storage.
        - organization_id is injected into every metadata dict.
        - module_id is Optional[str]: None for documents not yet linked to a module
          (the "Unlinked" bucket).
        """
        organization_id = self._validate_organization_id(organization_id)

        if not (len(documents) == len(embeddings) == len(ids)):
            raise ChromaValidationError(
                "documents, embeddings, and ids must all have the same length."
            )

        collection = self._get_collection(collection_name)

        scoped_ids = [self._scoped_id(organization_id, doc_id) for doc_id in ids]

        metadatas = []
        for i, doc_id in enumerate(ids):
            meta: Dict[str, Any] = {
                "organization_id": organization_id,
                # ChromaDB v1 does not accept None metadata values (must be str/int/float/bool).
                # None module_id (Unlinked bucket) is serialized as "" and interpreted as unlinked.
                "module_id": module_id if module_id is not None else "",
                "source_type": source_type,
                "project_id": project_id,
            }
            if extra_metadata and i < len(extra_metadata):
                meta.update(extra_metadata[i])
            metadatas.append(meta)

        start = time.perf_counter()
        try:
            collection.add(
                documents=documents,
                embeddings=embeddings,
                ids=scoped_ids,
                metadatas=metadatas,
            )
            duration_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "add_documents: collection='%s', count=%d, org_id='%s', %.2fms",
                collection_name, len(ids), organization_id, duration_ms,
            )
        except ChromaError as exc:
            raise ChromaQueryError(
                f"Failed to add documents to '{collection_name}': {exc}"
            ) from exc

    def query(
        self,
        collection_name: str,
        query_embeddings: List[List[float]],
        n_results: int = 5,
        organization_id: str = "",
        extra_filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Query a collection by vector similarity, scoped to one tenant.

        Returned IDs are guaranteed to be unscoped (org prefix stripped)
        before being returned to callers. Scoped IDs never leak out. (Gap 2)
        """
        organization_id = self._validate_organization_id(organization_id)
        collection = self._get_collection(collection_name)
        where = self._build_where(organization_id, extra_filter)

        start = time.perf_counter()
        try:
            raw = collection.query(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where,
            )
            duration_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "query: collection='%s', n_results=%d, org_id='%s', %.2fms",
                collection_name, n_results, organization_id, duration_ms,
            )
        except ChromaError as exc:
            raise ChromaQueryError(
                f"Query failed on collection '{collection_name}': {exc}"
            ) from exc

        # Unscope all returned IDs before handing to callers (Gap 2)
        if raw.get("ids"):
            raw["ids"] = [
                [self._unscoped_id(sid) for sid in batch]
                for batch in raw["ids"]
            ]
        return raw

    def get_documents(
        self,
        collection_name: str,
        organization_id: str,
        extra_filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch documents by metadata filter (not by vector similarity).
        Use this for existence/count checks — not query(), which returns
        nearest-neighbors regardless of match count.

        Returned IDs are guaranteed to be unscoped before returning. (Gap 2)
        """
        organization_id = self._validate_organization_id(organization_id)
        collection = self._get_collection(collection_name)
        where = self._build_where(organization_id, extra_filter)

        try:
            raw = collection.get(where=where, include=["documents", "metadatas"])
        except ChromaError as exc:
            raise ChromaQueryError(
                f"get_documents failed on collection '{collection_name}': {exc}"
            ) from exc

        # Unscope returned IDs before handing to callers (Gap 2)
        if raw.get("ids"):
            raw["ids"] = [self._unscoped_id(sid) for sid in raw["ids"]]
        return raw

    def delete_by_metadata(
        self,
        collection_name: str,
        organization_id: str,
        extra_filter: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Delete documents by metadata filter.

        ChromaDB 0.5.0 with SQLite backend fully supports collection.delete(where=...)
        via DELETE /api/v1/collections/{name}/delete. No fallback to get()+delete(ids=...)
        is required. (Gap 3)
        """
        organization_id = self._validate_organization_id(organization_id)
        collection = self._get_collection(collection_name)
        where = self._build_where(organization_id, extra_filter)

        start = time.perf_counter()
        try:
            collection.delete(where=where)
            duration_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "delete_by_metadata: collection='%s', org_id='%s', %.2fms",
                collection_name, organization_id, duration_ms,
            )
        except ChromaError as exc:
            raise ChromaQueryError(
                f"delete_by_metadata failed on collection '{collection_name}': {exc}"
            ) from exc


# Global singleton
chroma_client = ChromaClient()
