"""
AutoKT FastAPI Application Entry Point.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import logging

from app.api import repos, docs, business_docs, search, health_score, onboarding, kt_prep_questions, business_mapping, graph
from app.db.neo4j_client import neo4j_client
from app.db.chroma_client import chroma_client

_main_logger = logging.getLogger("autokt.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager managing startup & shutdown resources.
    """
    # --- Startup ---
    # Neo4j: connect + apply schema constraints
    try:
        neo4j_client.connect()
        neo4j_client.create_constraints()
    except Exception as exc:
        _main_logger.warning("Neo4j initialization warning: %s", exc)

    # ChromaDB: connect + ensure collections exist
    try:
        chroma_client.connect()
        chroma_client.ensure_collections()
    except Exception as exc:
        _main_logger.warning("ChromaDB initialization warning: %s", exc)

    # Postgres: seed system user sentinel
    try:
        from app.db.postgres_client import SessionLocal
        from app.core.constants import ensure_system_user
        with SessionLocal() as db:
            ensure_system_user(db)
    except Exception as exc:
        _main_logger.warning("Postgres system user seed warning: %s", exc)

    # Reranker: Preload model weights if configured
    try:
        from app.core.config import settings
        from app.core.reranker import get_reranker
        if settings.RERANKER_PROVIDER == "cross-encoder" and settings.RERANKER_PRELOAD:
            get_reranker(settings.RERANKER_MODEL_NAME)
    except Exception as exc:
        _main_logger.warning("Cross-encoder preload warning: %s", exc)

    yield

    # --- Shutdown ---
    neo4j_client.close()
    chroma_client.close()


app = FastAPI(
    title="AutoKT API",
    description="AI-Powered Knowledge-Transfer Platform API Service",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS Middleware Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Route Registration
app.include_router(repos.router)
app.include_router(docs.router)
app.include_router(business_docs.router)
app.include_router(search.router)
app.include_router(health_score.router)
app.include_router(onboarding.router)
app.include_router(kt_prep_questions.router)
app.include_router(business_mapping.router)
app.include_router(graph.router)


def custom_openapi():
    """Custom OpenAPI schema generator ensuring binary format for file arrays in Swagger UI."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    for schema in openapi_schema.get("components", {}).get("schemas", {}).values():
        if isinstance(schema, dict) and "properties" in schema:
            for prop in schema["properties"].values():
                if isinstance(prop, dict) and prop.get("type") == "array" and "items" in prop:
                    items = prop["items"]
                    if isinstance(items, dict) and items.get("type") == "string":
                        items["format"] = "binary"
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint returning system status.
    """
    return {"status": "ok"}
