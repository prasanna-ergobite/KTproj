"""
Hybrid Search API Router (Vector + Keyword BM25 + Knowledge Graph).
Combines ChromaDB vector similarity search, BM25 keyword search, Reciprocal Rank Fusion (RRF),
and Neo4j graph context expansion.
"""

import time
import logging
import re
import hashlib
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from app.core.config import settings
from app.core.embedder import embed_query
from app.core.reranker import rerank, get_reranker
from app.db.chroma_client import chroma_client
from app.db.neo4j_client import neo4j_client
from app.core.query_router import classify_query_intent, QueryIntent, IntentClassificationResult
from app.core.graph_traversal import execute_structural_traversal

logger = logging.getLogger("autokt.search")

router = APIRouter(prefix="/search", tags=["Search"])


# ---------------------------------------------------------------------------
# Pydantic Response Models
# ---------------------------------------------------------------------------

class RepositoryRef(BaseModel):
    id: str = Field(..., description="Repository node ID")
    name: str = Field(..., description="Repository name")
    url: Optional[str] = Field("", description="Remote repository git URL")


class ModuleRef(BaseModel):
    id: str = Field(..., description="Module node ID")
    name: str = Field(..., description="Module name")
    path: Optional[str] = Field("", description="Module file-system path")


class OwnerRef(BaseModel):
    name: str = Field(..., description="Author display name")
    email: str = Field(..., description="Author email address")
    commit_count: int = Field(0, description="Number of commits touching this file")


class RelatedDocRef(BaseModel):
    doc_id: str = Field(..., description="Document node ID")
    filename: str = Field(..., description="Document filename")
    relative_path: str = Field(..., description="Document relative path")


class RelatedCodeFileRef(BaseModel):
    file_id: str = Field(..., description="Code File node ID")
    filename: str = Field(..., description="Source code filename")
    relative_path: str = Field(..., description="Source code relative path")


class SearchGraphContext(BaseModel):
    repository: Optional[RepositoryRef] = None
    module: Optional[ModuleRef] = None
    owners: List[OwnerRef] = []
    related_documents: List[RelatedDocRef] = []
    related_code_files: List[RelatedCodeFileRef] = []


class SearchTelemetry(BaseModel):
    embedding_ms: float = Field(..., description="Query embedding duration in milliseconds")
    vector_search_ms: float = Field(..., description="ChromaDB vector search duration in milliseconds")
    keyword_search_ms: float = Field(0.0, description="BM25 keyword search duration in milliseconds")
    fusion_ms: float = Field(0.0, description="RRF candidate fusion duration in milliseconds")
    reranking_ms: float = Field(0.0, description="Cross-encoder reranking duration in milliseconds (0.0 if disabled or degraded)")
    graph_expansion_ms: float = Field(..., description="Neo4j graph expansion duration in milliseconds")
    graph_traversal_ms: float = Field(0.0, description="Deterministic Neo4j graph traversal duration in milliseconds (non-zero for structural queries)")
    total_ms: float = Field(..., description="Total endpoint execution time in milliseconds")


class SearchResultItem(BaseModel):
    chunk_id: str = Field(..., description="Unscoped chunk ID")
    result_type: str = Field(..., description="Result type: 'code' or 'doc'")
    similarity_score: float = Field(0.0, description="Cosine similarity score (0.0 for BM25-only hits)")
    rrf_score: float = Field(0.0, description="Reciprocal Rank Fusion score (informational when reranking is active)")
    rerank_score: Optional[float] = Field(
        None,
        description=(
            "Cross-encoder rerank score (primary sorting key when reranking is active). "
            "Raw logit value — NOT normalized to 0-1 range. Higher is more relevant. "
            "None when reranking is disabled or degraded."
        ),
    )
    vector_rank: Optional[int] = Field(None, description="1-based rank in vector search results (None if absent)")
    keyword_rank: Optional[int] = Field(None, description="1-based rank in BM25 keyword results (None if absent)")
    text: str = Field(..., description="Chunk content text")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Curated metadata fields")
    graph_context: Optional[SearchGraphContext] = None


class HybridSearchResponse(BaseModel):
    query: str = Field(..., description="Original search query string")
    organization_id: str = Field(..., description="Tenant organization ID")
    total_results: int = Field(
        ...,
        description=(
            "Size of the reranked candidate pool before pagination. "
            "As of the cross-encoder reranking milestone, this is capped at RERANKER_TOP_K (default 30), "
            "not the full underlying RRF match count. See Known Limitation #10."
        ),
    )
    limit: int = Field(10, description="Page limit requested")
    offset: int = Field(0, description="Page offset requested")
    telemetry: SearchTelemetry = Field(..., description="Execution duration metrics")
    results: List[SearchResultItem] = Field(default_factory=list, description="Ranked search result items")


# ---------------------------------------------------------------------------
# Formatting Helper for Structural Query Results
# ---------------------------------------------------------------------------

def _format_structural_results(records: List[Dict[str, Any]], intent_res: IntentClassificationResult) -> List[SearchResultItem]:
    items: List[SearchResultItem] = []
    intent = intent_res.intent

    if intent in (QueryIntent.SAME_MODULE_FUNCTIONS, QueryIntent.SAME_FILE_FUNCTIONS):
        for rec in records:
            repo_ref = RepositoryRef(**rec["repository"]) if rec.get("repository") else None
            mod_ref = ModuleRef(**rec["module"]) if rec.get("module") else None
            owners = [
                OwnerRef(name=o.get("name", ""), email=o.get("email", ""), commit_count=o.get("commit_count", 0))
                for o in rec.get("owners", []) if o.get("email")
            ]
            graph_ctx = SearchGraphContext(repository=repo_ref, module=mod_ref, owners=owners)
            fn_name = rec.get("function_name", "")
            f_path = rec.get("file_path", "")
            mod_name = rec.get("module", {}).get("name", "") if rec.get("module") else ""
            text_summary = f"# Function: {fn_name}\n# File: {f_path} (lines {rec.get('start_line', 0)}-{rec.get('end_line', 0)})\n# Module: {mod_name}"
            items.append(
                SearchResultItem(
                    chunk_id=rec.get("function_id") or rec.get("file_id") or f"fn:{fn_name}",
                    result_type="code",
                    similarity_score=1.0,
                    rrf_score=1.0,
                    rerank_score=None,
                    vector_rank=1,
                    keyword_rank=1,
                    text=text_summary,
                    metadata={
                        "file_path": f_path,
                        "function_name": fn_name,
                        "start_line": rec.get("start_line", 0),
                        "end_line": rec.get("end_line", 0),
                        "chunk_type": "function_definition",
                    },
                    graph_context=graph_ctx,
                )
            )

    elif intent == QueryIntent.FILE_SYMBOLS:
        for rec in records:
            repo_ref = RepositoryRef(**rec["repository"]) if rec.get("repository") else None
            mod_ref = ModuleRef(**rec["module"]) if rec.get("module") else None
            owners = [
                OwnerRef(name=o.get("name", ""), email=o.get("email", ""), commit_count=o.get("commit_count", 0))
                for o in rec.get("owners", []) if o.get("email")
            ]
            graph_ctx = SearchGraphContext(repository=repo_ref, module=mod_ref, owners=owners)
            f_path = rec.get("file_path", "")
            funcs = rec.get("functions", [])
            classes = rec.get("classes", [])
            fn_names = [f.get("name") for f in funcs if f.get("name")]
            cls_names = [c.get("name") for c in classes if c.get("name")]
            text_summary = f"# File: {f_path}\n# Classes: {', '.join(cls_names) if cls_names else 'None'}\n# Functions: {', '.join(fn_names) if fn_names else 'None'}"
            items.append(
                SearchResultItem(
                    chunk_id=rec.get("file_id") or f"file:{f_path}",
                    result_type="code",
                    similarity_score=1.0,
                    rrf_score=1.0,
                    rerank_score=None,
                    vector_rank=1,
                    keyword_rank=1,
                    text=text_summary,
                    metadata={
                        "file_path": f_path,
                        "functions": fn_names,
                        "classes": cls_names,
                    },
                    graph_context=graph_ctx,
                )
            )

    elif intent in (QueryIntent.CONTAINING_FILE, QueryIntent.CONTAINING_MODULE):
        for rec in records:
            repo_ref = RepositoryRef(**rec["repository"]) if rec.get("repository") else None
            mod_ref = ModuleRef(**rec["module"]) if rec.get("module") else None
            owners = [
                OwnerRef(name=o.get("name", ""), email=o.get("email", ""), commit_count=o.get("commit_count", 0))
                for o in rec.get("owners", []) if o.get("email")
            ]
            graph_ctx = SearchGraphContext(repository=repo_ref, module=mod_ref, owners=owners)
            sym_name = rec.get("symbol_name") or rec.get("file_path", "")
            sym_type = rec.get("symbol_type") or "File"
            f_path = rec.get("file_path", "")
            mod_name = rec.get("module", {}).get("name", "") if rec.get("module") else ""
            text_summary = f"# Symbol: {sym_name} ({sym_type})\n# File: {f_path}\n# Module: {mod_name}"
            items.append(
                SearchResultItem(
                    chunk_id=rec.get("file_id") or f"sym:{sym_name}",
                    result_type="code",
                    similarity_score=1.0,
                    rrf_score=1.0,
                    rerank_score=None,
                    vector_rank=1,
                    keyword_rank=1,
                    text=text_summary,
                    metadata={
                        "symbol_name": sym_name,
                        "file_path": f_path,
                        "module_name": mod_name,
                    },
                    graph_context=graph_ctx,
                )
            )

    return items


# ---------------------------------------------------------------------------
# Whitelist Helpers for Clean Metadata Output
# ---------------------------------------------------------------------------

def _whitelist_code_metadata(raw_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file_path": raw_meta.get("file_path", ""),
        "function_name": raw_meta.get("function_name", ""),
        "class_name": raw_meta.get("class_name", ""),
        "parent_class": raw_meta.get("parent_class", ""),
        "start_line": raw_meta.get("start_line", 0),
        "end_line": raw_meta.get("end_line", 0),
        "chunk_type": raw_meta.get("chunk_type", "unknown"),
        "language": raw_meta.get("language", ""),
        "token_count": raw_meta.get("token_count", 0),
        "is_documented": raw_meta.get("is_documented", False),
    }


def _whitelist_doc_metadata(raw_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "relative_path": raw_meta.get("relative_path", ""),
        "heading_path": raw_meta.get("heading_path", ""),
        "section_level": raw_meta.get("section_level", 0),
        "chunking_method": raw_meta.get("chunking_method", ""),
        "chunk_type": raw_meta.get("chunk_type", "unknown"),
        "chunk_total": raw_meta.get("chunk_total", 0),
        "file_format": raw_meta.get("file_format", ""),
        "version": raw_meta.get("version", 1),
        "token_count": raw_meta.get("token_count", 0),
        "has_code_fence": raw_meta.get("has_code_fence", False),
        "doc_type": raw_meta.get("doc_type", ""),
        "repository_id": raw_meta.get("repository_id", ""),
    }


def _extract_file_id(meta: Dict[str, Any]) -> str:
    if meta.get("file_id"):
        return meta["file_id"]
    file_path = meta.get("file_path", "")
    module_id = meta.get("module_id", "")
    if module_id and ":module:" in module_id:
        repo_node_id = module_id.split(":module:")[0]
        return f"{repo_node_id}:file:{file_path}"
    return f"file:{file_path}"


def _extract_doc_id(meta: Dict[str, Any]) -> Optional[str]:
    if meta.get("doc_id"):
        return meta["doc_id"]
    rel_path = meta.get("relative_path")
    if rel_path:
        return f"doc:{rel_path}"
    return None


def _tokenize(text: str) -> List[str]:
    """
    Tokenize query or document text using regex word extraction.
    
    Decision on header tokens (# File:, # Function:, # Section:, # Chunk: N/M):
    Accepted as noise for v1 because BM25's IDF naturally discounts terms that
    appear across nearly every document in the corpus (IDF -> 0).
    """
    return re.findall(r"\w+", text.lower())


# ---------------------------------------------------------------------------
# Hybrid Search Endpoint
# ---------------------------------------------------------------------------

@router.get("", response_model=HybridSearchResponse)
async def search_knowledge(
    q: str = Query(..., min_length=1, max_length=1000, description="Search query string"),
    organization_id: str = Query(..., min_length=1, description="Tenant organization ID"),
    source_type: str = Query("all", pattern="^(all|code|doc)$", description="Filter source type ('all', 'code', 'doc')"),
    module_id: Optional[str] = Query(None, description="Optional module ID filter"),
    doc_type: Optional[str] = Query(None, description="Optional document category filter (e.g. 'business', 'prd', 'technical')"),
    min_score: float = Query(0.0, ge=0.0, le=1.0, description="Minimum similarity score threshold (applies to vector search only)"),
    limit: int = Query(10, ge=1, le=100, description="Maximum results per page"),
    offset: int = Query(0, ge=0, description="Page offset"),
):
    """
    Execute hybrid search over ChromaDB vector embeddings, BM25 keyword search, Reciprocal Rank Fusion (RRF),
    Cross-Encoder Reranking, and Neo4j Knowledge Graph.

    Flow:
    1. Embed query string using Nomic search_query prefix.
    2. Perform vector similarity search across code_chunks and docs_chunks in ChromaDB.
    3. Perform full-corpus BM25 keyword search over tenant document chunks.
    4. Fuse vector search and BM25 results using Reciprocal Rank Fusion (RRF, k=60).
    4.5 Perform Cross-Encoder Reranking over top-K RRF candidates (K = max(RERANKER_TOP_K, limit+offset)).
    5. Perform single-pass batched Cypher expansion in Neo4j for post-rerank top candidates.
    6. Paginate and return results sorted by rerank_score DESC (falling back to rrf_score DESC if degraded).
    """
    t_start = time.perf_counter()

    # Step 0: Check Query Intent Router for Structural Queries
    intent_res = classify_query_intent(q)
    if intent_res.intent != QueryIntent.SEMANTIC:
        t_graph_start = time.perf_counter()
        raw_records = execute_structural_traversal(intent_res, organization_id)
        t_graph_ms = (time.perf_counter() - t_graph_start) * 1000.0

        structural_items = _format_structural_results(raw_records, intent_res)
        total_count = len(structural_items)
        paginated = structural_items[offset : offset + limit]

        return HybridSearchResponse(
            query=q,
            organization_id=organization_id,
            total_results=total_count,
            limit=limit,
            offset=offset,
            telemetry=SearchTelemetry(
                embedding_ms=0.0,
                vector_search_ms=0.0,
                keyword_search_ms=0.0,
                fusion_ms=0.0,
                reranking_ms=0.0,
                graph_expansion_ms=0.0,
                graph_traversal_ms=t_graph_ms,
                total_ms=(time.perf_counter() - t_start) * 1000.0,
            ),
            results=paginated,
        )

    # Step 1: Generate Query Vector
    t_embed_start = time.perf_counter()
    query_vector = embed_query(q)
    if not query_vector:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query string produced an empty embedding vector."
        )
    t_embed_ms = (time.perf_counter() - t_embed_start) * 1000.0

    # Step 2: Query ChromaDB Vector Collections
    t_vector_start = time.perf_counter()
    extra_filter = {"module_id": {"$eq": module_id}} if module_id else None
    
    # Construct doc_extra_filter for docs_chunks (supports module_id and/or doc_type)
    doc_filter_clauses = []
    if module_id:
        doc_filter_clauses.append({"module_id": {"$eq": module_id}})
    if doc_type and doc_type.strip():
        doc_filter_clauses.append({"doc_type": {"$eq": doc_type.strip()}})

    if len(doc_filter_clauses) == 1:
        doc_extra_filter = doc_filter_clauses[0]
    elif len(doc_filter_clauses) > 1:
        doc_extra_filter = {"$and": doc_filter_clauses}
    else:
        doc_extra_filter = None

    candidate_fetch_count = max(limit + offset, 50)

    # Dictionary: cid -> (score, text, metadata, result_type)
    vector_hits_map: Dict[str, Any] = {}
    vector_ordered_ids: List[str] = []

    # Fetch code_chunks
    if source_type in ("all", "code"):
        try:
            res_code = chroma_client.query(
                collection_name="code_chunks",
                query_embeddings=[query_vector],
                n_results=candidate_fetch_count,
                organization_id=organization_id,
                extra_filter=extra_filter,
            )
            if res_code and res_code.get("ids") and res_code["ids"][0]:
                for i in range(len(res_code["ids"][0])):
                    cid = res_code["ids"][0][i]
                    text = res_code["documents"][0][i]
                    meta = res_code["metadatas"][0][i]
                    dist = res_code["distances"][0][i]
                    score = round(max(0.0, 1.0 - float(dist)), 4)
                    if score >= min_score:
                        vector_hits_map[cid] = (score, text, meta, "code")
                        vector_ordered_ids.append(cid)
        except Exception as exc:
            logger.warning("Error querying ChromaDB collection 'code_chunks': %s", exc)

    # Fetch docs_chunks
    if source_type in ("all", "doc"):
        try:
            res_doc = chroma_client.query(
                collection_name="docs_chunks",
                query_embeddings=[query_vector],
                n_results=candidate_fetch_count,
                organization_id=organization_id,
                extra_filter=doc_extra_filter,
            )
            if res_doc and res_doc.get("ids") and res_doc["ids"][0]:
                for i in range(len(res_doc["ids"][0])):
                    cid = res_doc["ids"][0][i]
                    text = res_doc["documents"][0][i]
                    meta = res_doc["metadatas"][0][i]
                    dist = res_doc["distances"][0][i]
                    score = round(max(0.0, 1.0 - float(dist)), 4)
                    if score >= min_score:
                        if cid not in vector_hits_map:
                            vector_hits_map[cid] = (score, text, meta, "doc")
                            vector_ordered_ids.append(cid)
        except Exception as exc:
            logger.warning("Error querying ChromaDB collection 'docs_chunks': %s", exc)

    # Sort vector hits strictly by score DESC to assign 1-based vector_rank
    vector_ordered_ids.sort(key=lambda cid: vector_hits_map[cid][0], reverse=True)
    vector_rank_map = {cid: rank + 1 for rank, cid in enumerate(vector_ordered_ids)}

    t_vector_ms = (time.perf_counter() - t_vector_start) * 1000.0

    # Step 3: Full-Corpus BM25 Keyword Search
    t_keyword_start = time.perf_counter()
    bm25_hits_map: Dict[str, Any] = {}
    bm25_rank_map: Dict[str, int] = {}

    try:
        bm25_corpus_ids: List[str] = []
        bm25_corpus_texts: List[str] = []
        bm25_corpus_metas: List[Dict[str, Any]] = []
        bm25_corpus_types: List[str] = []

        if source_type in ("all", "code"):
            raw_code_docs = chroma_client.get_documents(
                collection_name="code_chunks",
                organization_id=organization_id,
                extra_filter=extra_filter,
            )
            if raw_code_docs and raw_code_docs.get("ids") and raw_code_docs.get("documents"):
                c_ids = raw_code_docs["ids"]
                c_docs = raw_code_docs["documents"]
                c_metas = raw_code_docs.get("metadatas") or [{}] * len(c_ids)
                for i in range(len(c_ids)):
                    bm25_corpus_ids.append(c_ids[i])
                    bm25_corpus_texts.append(c_docs[i] or "")
                    bm25_corpus_metas.append(c_metas[i] or {})
                    bm25_corpus_types.append("code")

        if source_type in ("all", "doc"):
            raw_doc_docs = chroma_client.get_documents(
                collection_name="docs_chunks",
                organization_id=organization_id,
                extra_filter=doc_extra_filter,
            )
            if raw_doc_docs and raw_doc_docs.get("ids") and raw_doc_docs.get("documents"):
                d_ids = raw_doc_docs["ids"]
                d_docs = raw_doc_docs["documents"]
                d_metas = raw_doc_docs.get("metadatas") or [{}] * len(d_ids)
                for i in range(len(d_ids)):
                    bm25_corpus_ids.append(d_ids[i])
                    bm25_corpus_texts.append(d_docs[i] or "")
                    bm25_corpus_metas.append(d_metas[i] or {})
                    bm25_corpus_types.append("doc")

        if bm25_corpus_texts:
            tokenized_corpus = [_tokenize(t) for t in bm25_corpus_texts]
            tokenized_query = _tokenize(q)

            if tokenized_query:
                bm25_engine = BM25Okapi(tokenized_corpus)
                bm25_scores = bm25_engine.get_scores(tokenized_query)

                # Filter and rank non-zero BM25 hits, capping top candidates to prevent RRF candidate inflation
                scored_indices = [
                    (idx, float(score)) for idx, score in enumerate(bm25_scores) if score > 0.0
                ]
                scored_indices.sort(key=lambda item: item[1], reverse=True)
                top_k_bm25 = max(candidate_fetch_count, 100)
                scored_indices = scored_indices[:top_k_bm25]

                for rank_idx, (corpus_idx, b_score) in enumerate(scored_indices):
                    cid = bm25_corpus_ids[corpus_idx]
                    bm25_rank_map[cid] = rank_idx + 1
                    bm25_hits_map[cid] = (
                        b_score,
                        bm25_corpus_texts[corpus_idx],
                        bm25_corpus_metas[corpus_idx],
                        bm25_corpus_types[corpus_idx],
                    )
    except Exception as bm25_exc:
        logger.warning("BM25 keyword search failed; degrading to vector-only search: %s", bm25_exc)

    t_keyword_ms = (time.perf_counter() - t_keyword_start) * 1000.0

    # Step 4: Reciprocal Rank Fusion (RRF, k=60)
    t_fusion_start = time.perf_counter()
    k_rrf = 60.0
    all_candidate_ids = set(vector_hits_map.keys()).union(set(bm25_hits_map.keys()))

    rrf_candidates = []
    for cid in all_candidate_ids:
        vec_info = vector_hits_map.get(cid)
        bm25_info = bm25_hits_map.get(cid)

        vec_rank = vector_rank_map.get(cid)
        k_rank = bm25_rank_map.get(cid)

        rrf_vec_score = 1.0 / (k_rrf + vec_rank) if vec_rank is not None else 0.0
        rrf_bm25_score = 1.0 / (k_rrf + k_rank) if k_rank is not None else 0.0
        rrf_score = round(rrf_vec_score + rrf_bm25_score, 6)

        sim_score = vec_info[0] if vec_info else 0.0
        text = vec_info[1] if vec_info else bm25_info[1]
        meta = vec_info[2] if vec_info else bm25_info[2]
        res_type = vec_info[3] if vec_info else bm25_info[3]

        rrf_candidates.append({
            "chunk_id": cid,
            "result_type": res_type,
            "similarity_score": sim_score,
            "rrf_score": rrf_score,
            "vector_rank": vec_rank,
            "keyword_rank": k_rank,
            "text": text,
            "metadata": meta,
        })

    # Sort candidates strictly by rrf_score DESC
    rrf_candidates.sort(key=lambda item: item["rrf_score"], reverse=True)
    t_fusion_ms = (time.perf_counter() - t_fusion_start) * 1000.0

    # Step 4.5: Cross-Encoder Reranking (Top-K RRF Candidates)
    if settings.RERANKER_PROVIDER == "cross-encoder":
        get_reranker(settings.RERANKER_MODEL_NAME)

    t_rerank_start = time.perf_counter()
    rerank_k = max(settings.RERANKER_TOP_K, limit + offset)
    rrf_top_k = rrf_candidates[:rerank_k]

    try:
        if settings.RERANKER_PROVIDER != "disabled" and rrf_top_k:
            texts_to_rerank = [item["text"] for item in rrf_top_k]
            scores = rerank(
                query=q,
                texts=texts_to_rerank,
                provider=settings.RERANKER_PROVIDER,
                model_name=settings.RERANKER_MODEL_NAME,
                head_chars=settings.RERANKER_HEAD_CHARS,
                tail_chars=settings.RERANKER_TAIL_CHARS,
            )
            if len(scores) == len(rrf_top_k):
                for item, score in zip(rrf_top_k, scores):
                    item["rerank_score"] = float(score)
                # Resort strictly by rerank_score DESC
                rrf_top_k.sort(key=lambda item: item["rerank_score"], reverse=True)
            else:
                logger.warning("Reranker returned mismatched score count; falling back to RRF ordering")
                for item in rrf_top_k:
                    item["rerank_score"] = None
        else:
            for item in rrf_top_k:
                item["rerank_score"] = None
    except Exception as rerank_exc:
        logger.warning("Cross-encoder reranking failed; degrading to RRF ordering: %s", rerank_exc)
        for item in rrf_top_k:
            item["rerank_score"] = None

    t_rerank_ms = (time.perf_counter() - t_rerank_start) * 1000.0

    # Step 5: Single-Pass Graph Expansion in Neo4j (Sliced top limit+offset candidates post-rerank)
    t_graph_start = time.perf_counter()
    file_context_map: Dict[str, Dict[str, Any]] = {}
    doc_context_map: Dict[str, Dict[str, Any]] = {}

    graph_candidates = rrf_top_k[: limit + offset]

    file_tenant_keys = []
    file_id_to_key = {}
    doc_tenant_keys = []
    doc_id_to_key = {}

    for cand in graph_candidates:
        meta = cand["metadata"]
        if cand["result_type"] == "code":
            fid = _extract_file_id(meta)
            if fid and fid not in file_id_to_key:
                tk = f"{organization_id}:{fid}"
                file_tenant_keys.append(tk)
                file_id_to_key[fid] = tk
        elif cand["result_type"] == "doc":
            did = _extract_doc_id(meta)
            if did and did not in doc_id_to_key:
                tk = f"{organization_id}:{did}"
                doc_tenant_keys.append(tk)
                doc_id_to_key[did] = tk

    try:
        if file_tenant_keys:
            cypher_file_expand = (
                "UNWIND $file_tenant_keys AS tk "
                "MATCH (f:File {tenant_key: tk, organization_id: $organization_id}) "
                "OPTIONAL MATCH (f)-[:PART_OF]->(m:Module {organization_id: $organization_id}) "
                "OPTIONAL MATCH (m)-[:PART_OF]->(r:Repository {organization_id: $organization_id}) "
                "OPTIONAL MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(f) "
                "OPTIONAL MATCH (target_mod:Module {organization_id: $organization_id}) "
                "WHERE target_mod = m OR (r IS NOT NULL AND target_mod.repository_id = r.id AND target_mod.name = '_root') "
                "OPTIONAL MATCH (d:Document {organization_id: $organization_id})-[:DOCUMENTS]->(target_mod) "
                "OPTIONAL MATCH (doc_f:File {organization_id: $organization_id})-[:PART_OF]->(target_mod) "
                "WHERE doc_f.path ENDS WITH '.md' OR doc_f.path ENDS WITH '.rst' OR doc_f.path ENDS WITH '.txt' OR doc_f.path CONTAINS 'README' "
                "WITH f, r, m, p, o, d, doc_f "
                "RETURN f.id AS file_id, "
                "       r { .id, .name, .url } AS repository, "
                "       m { .id, .name, .path } AS module, "
                "       collect(DISTINCT p { .name, .email, commit_count: o.commit_count }) AS owners, "
                "       [item IN collect(DISTINCT CASE "
                "           WHEN d IS NOT NULL THEN { doc_id: d.id, filename: d.filename, relative_path: d.relative_path } "
                "           WHEN doc_f IS NOT NULL AND doc_f.id <> f.id THEN { doc_id: doc_f.id, filename: coalesce(doc_f.filename, doc_f.path, ''), relative_path: doc_f.path } "
                "           ELSE NULL END) WHERE item IS NOT NULL] AS related_documents"
            )
            records = neo4j_client.run_read_query(
                cypher_file_expand,
                parameters={
                    "file_tenant_keys": file_tenant_keys,
                    "organization_id": organization_id,
                },
                organization_id=organization_id,
            )
            for rec in records:
                file_context_map[rec["file_id"]] = rec

        if doc_tenant_keys:
            cypher_doc_expand = (
                "UNWIND $doc_tenant_keys AS tk "
                "OPTIONAL MATCH (d_sub:Document {tenant_key: tk, organization_id: $organization_id}) "
                "OPTIONAL MATCH (f_sub:File {tenant_key: tk, organization_id: $organization_id}) "
                "WITH coalesce(d_sub, f_sub) AS doc_node, tk "
                "WHERE doc_node IS NOT NULL "
                "OPTIONAL MATCH (doc_node)-[:PART_OF|DOCUMENTS]->(m:Module {organization_id: $organization_id}) "
                "OPTIONAL MATCH (m)-[:PART_OF]->(r:Repository {organization_id: $organization_id}) "
                "OPTIONAL MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(doc_node) "
                "OPTIONAL MATCH (target_mod:Module {organization_id: $organization_id}) "
                "WHERE (r IS NOT NULL AND target_mod.repository_id = r.id) OR target_mod = m "
                "OPTIONAL MATCH (f:File {organization_id: $organization_id})-[:PART_OF]->(target_mod) "
                "WHERE f.id <> doc_node.id "
                "RETURN doc_node.id AS doc_id, "
                "       r { .id, .name, .url } AS repository, "
                "       m { .id, .name, .path } AS module, "
                "       collect(DISTINCT p { .name, .email, commit_count: o.commit_count }) AS owners, "
                "       collect(DISTINCT f { file_id: f.id, filename: coalesce(f.filename, f.path, ''), relative_path: coalesce(f.relative_path, f.path, '') }) AS related_code_files"
            )
            records = neo4j_client.run_read_query(
                cypher_doc_expand,
                parameters={
                    "doc_tenant_keys": doc_tenant_keys,
                    "organization_id": organization_id,
                },
                organization_id=organization_id,
            )
            for rec in records:
                doc_context_map[rec["doc_id"]] = rec

    except Exception as graph_exc:
        logger.warning("Graph expansion in Neo4j failed or unavailable; degrading to vector-only context: %s", graph_exc)

    t_graph_ms = (time.perf_counter() - t_graph_start) * 1000.0

    # Step 6: Assemble Final Response Objects & Paginate
    final_items: List[SearchResultItem] = []

    for cand in rrf_top_k:
        cid = cand["chunk_id"]
        res_type = cand["result_type"]
        raw_meta = cand["metadata"]

        if res_type == "code":
            fid = _extract_file_id(raw_meta)
            gdata = file_context_map.get(fid, {})

            repo_ref = RepositoryRef(**gdata["repository"]) if gdata.get("repository") else None
            mod_ref = ModuleRef(**gdata["module"]) if gdata.get("module") else None
            owners = [
                OwnerRef(name=o.get("name", ""), email=o.get("email", ""), commit_count=o.get("commit_count", 0))
                for o in gdata.get("owners", []) if o.get("email")
            ]
            rel_docs = [
                RelatedDocRef(
                    doc_id=d.get("doc_id") or d.get("id", ""),
                    filename=d.get("filename", ""),
                    relative_path=d.get("relative_path", ""),
                )
                for d in gdata.get("related_documents", [])
                if d.get("doc_id") or d.get("id")
            ]

            graph_ctx = SearchGraphContext(
                repository=repo_ref,
                module=mod_ref,
                owners=owners,
                related_documents=rel_docs,
                related_code_files=[],
            )

            final_items.append(
                SearchResultItem(
                    chunk_id=cid,
                    result_type="code",
                    similarity_score=cand["similarity_score"],
                    rrf_score=cand["rrf_score"],
                    rerank_score=cand.get("rerank_score"),
                    vector_rank=cand["vector_rank"],
                    keyword_rank=cand["keyword_rank"],
                    text=cand["text"],
                    metadata=_whitelist_code_metadata(raw_meta),
                    graph_context=graph_ctx,
                )
            )

        elif res_type == "doc":
            did = _extract_doc_id(raw_meta)
            gdata = doc_context_map.get(did, {}) if did else {}

            repo_ref = RepositoryRef(**gdata["repository"]) if gdata.get("repository") else None
            mod_ref = ModuleRef(**gdata["module"]) if gdata.get("module") else None
            owners = [
                OwnerRef(name=o.get("name", ""), email=o.get("email", ""), commit_count=o.get("commit_count", 0))
                for o in gdata.get("owners", []) if o.get("email")
            ]
            rel_code = [
                RelatedCodeFileRef(
                    file_id=f.get("file_id") or f.get("id", ""),
                    filename=f.get("filename", ""),
                    relative_path=f.get("relative_path", ""),
                )
                for f in gdata.get("related_code_files", [])
                if f.get("file_id") or f.get("id")
            ]

            graph_ctx = SearchGraphContext(
                repository=repo_ref,
                module=mod_ref,
                owners=owners,
                related_documents=[],
                related_code_files=rel_code,
            )

            final_items.append(
                SearchResultItem(
                    chunk_id=cid,
                    result_type="doc",
                    similarity_score=cand["similarity_score"],
                    rrf_score=cand["rrf_score"],
                    rerank_score=cand.get("rerank_score"),
                    vector_rank=cand["vector_rank"],
                    keyword_rank=cand["keyword_rank"],
                    text=cand["text"],
                    metadata=_whitelist_doc_metadata(raw_meta),
                    graph_context=graph_ctx,
                )
            )

    paginated_results = final_items[offset : offset + limit]
    t_total_ms = (time.perf_counter() - t_start) * 1000.0

    telemetry = SearchTelemetry(
        embedding_ms=round(t_embed_ms, 2),
        vector_search_ms=round(t_vector_ms, 2),
        keyword_search_ms=round(t_keyword_ms, 2),
        fusion_ms=round(t_fusion_ms, 2),
        reranking_ms=round(t_rerank_ms, 2),
        graph_expansion_ms=round(t_graph_ms, 2),
        total_ms=round(t_total_ms, 2),
    )

    return HybridSearchResponse(
        query=q,
        organization_id=organization_id,
        total_results=len(rrf_top_k),
        limit=limit,
        offset=offset,
        telemetry=telemetry,
        results=paginated_results,
    )


# ---------------------------------------------------------------------------
# Answer Generation Models & Endpoint  (RAG: top-k context → LLM answer)
# ---------------------------------------------------------------------------

class AnswerRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="User question to answer")
    organization_id: str = Field(..., min_length=1, description="Tenant organization ID")
    context_items: List[SearchResultItem] = Field(
        default_factory=list,
        description="Top-K search results to use as grounding context (pass results from /search)",
    )
    top_k: int = Field(
        5,
        ge=1,
        le=20,
        description="How many context items to include in the LLM prompt (first N from context_items)",
    )


class CitedSource(BaseModel):
    chunk_id: str = Field(..., description="Chunk ID of the source item")
    result_type: str = Field(..., description="'code' or 'doc'")
    file_path: str = Field("", description="File or document path")
    snippet: str = Field("", description="First 300 chars of the source chunk text")
    rerank_score: Optional[float] = Field(None, description="Reranker score of this source")
    module_name: Optional[str] = Field(None, description="Module the source belongs to")
    owners: List[str] = Field(default_factory=list, description="Owner emails of the source")


class AnswerResponse(BaseModel):
    query: str
    organization_id: str
    answer: str = Field(..., description="LLM-generated grounded answer")
    sources: List[CitedSource] = Field(default_factory=list, description="Sources used to generate the answer")
    context_items_used: int = Field(..., description="Number of context items fed to the LLM")
    generation_ms: float = Field(..., description="LLM generation duration in milliseconds")


def _build_rag_prompt(query: str, items: List[SearchResultItem]) -> str:
    """Build a structured RAG prompt from the top-k search result items."""
    lines = ["You are an expert software engineering assistant helping with codebase knowledge transfer."]
    lines.append("Answer the user's question using ONLY the context excerpts provided below.")
    lines.append("If the answer cannot be determined from the context, say so clearly.")
    lines.append("Be concise, technically precise, and cite the relevant source by its [Chunk N] label.\n")
    lines.append("=" * 60)
    lines.append("CONTEXT EXCERPTS")
    lines.append("=" * 60)

    for idx, item in enumerate(items, start=1):
        meta = item.metadata or {}
        file_path = meta.get("file_path") or meta.get("relative_path", "")
        chunk_type = meta.get("chunk_type", item.result_type)
        func_name = meta.get("function_name", "")
        heading = meta.get("heading_path", "")

        label_parts = [f"[Chunk {idx}]", f"type={chunk_type}"]
        if file_path:
            label_parts.append(f"file={file_path}")
        if func_name:
            label_parts.append(f"function={func_name}")
        if heading:
            label_parts.append(f"section={heading}")
        if item.graph_context and item.graph_context.module:
            label_parts.append(f"module={item.graph_context.module.name}")
        if item.rerank_score is not None:
            label_parts.append(f"score={item.rerank_score:.3f}")

        lines.append("  ".join(label_parts))
        lines.append(item.text.strip())
        lines.append("")

    lines.append("=" * 60)
    lines.append(f"USER QUESTION: {query}")
    lines.append("=" * 60)
    lines.append("\nANSWER (cite chunk numbers where relevant):")

    return "\n".join(lines)


@router.post("/answer", response_model=AnswerResponse)
async def answer_question(request: AnswerRequest):
    """
    Generate a grounded answer for a question using top-K retrieved context chunks (RAG).

    Workflow:
    1. Slice request.context_items to top_k.
    2. Build a structured RAG prompt with labelled context excerpts.
    3. Call the provider-agnostic LLM client (stub / Gemini / Azure / Azure Foundry).
    4. Return the answer with cited sources.

    Usage pattern:
      - First call GET /search to retrieve ranked context items.
      - Pass the results directly into this endpoint as `context_items`.
    """
    from app.core.llm_client import generate_text, LLMClientError

    t_start = time.perf_counter()

    # Slice to top_k
    items = request.context_items[: request.top_k]

    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_items is empty. Run /search first and pass its results here.",
        )

    prompt = _build_rag_prompt(request.query, items)
    system_instruction = (
        "You are AutoKT, an AI assistant specialised in explaining codebases and software knowledge. "
        "Provide accurate, concise answers grounded strictly in the provided context. "
        "Always cite the [Chunk N] labels when referencing specific sources."
    )

    try:
        answer_text = generate_text(prompt=prompt, system_instruction=system_instruction)
    except LLMClientError as exc:
        logger.error("LLM answer generation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM generation failed: {exc}",
        )

    generation_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

    # Build cited sources list
    sources: List[CitedSource] = []
    for item in items:
        meta = item.metadata or {}
        file_path = meta.get("file_path") or meta.get("relative_path", "")
        module_name = None
        owners: List[str] = []
        if item.graph_context:
            if item.graph_context.module:
                module_name = item.graph_context.module.name
            owners = [o.email for o in item.graph_context.owners if o.email]
        sources.append(
            CitedSource(
                chunk_id=item.chunk_id,
                result_type=item.result_type,
                file_path=file_path,
                snippet=item.text[:300].strip(),
                rerank_score=item.rerank_score,
                module_name=module_name,
                owners=owners,
            )
        )

    return AnswerResponse(
        query=request.query,
        organization_id=request.organization_id,
        answer=answer_text,
        sources=sources,
        context_items_used=len(items),
        generation_ms=generation_ms,
    )

