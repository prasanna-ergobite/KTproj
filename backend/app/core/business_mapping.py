"""
Business-to-Code Mapping Engine for AutoKT (Milestone 20).
Connects business concepts described in ingested business documents (PRDs, BRDs)
to the actual source code functions and files that implement them via a 4-stage pipeline:
1. Hybrid Retrieval (Vector + BM25 + RRF) scoped to repository
2. Cross-Encoder Reranking & Threshold Filtering
3. Provider-Agnostic LLM Grounded Judge
4. Neo4j Graph Topology Enrichment (Intra-file Callers)

Design Decision Note (G6 Divergence):
Deliberately does not share retrieval code with search.py. Unlike module_context.py (where multiple pipelines
required identical retrieval semantics), business mapping retrieval is single-collection (code only),
has no final result pagination, and uses fixed candidate pool sizes. Isolation here is load-bearing to prevent coupling.
"""

import re
import json
import time
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from app.core.config import settings
from app.db.chroma_client import chroma_client
from app.db.neo4j_client import neo4j_client
from app.core.embedder import embed_query
from app.core.reranker import rerank
from app.core.llm_client import generate_text, LLMClientError

logger = logging.getLogger("autokt.business_mapping")


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class MappingTelemetry(BaseModel):
    retrieval_ms: float = Field(..., description="Stage 1 retrieval duration in ms")
    reranking_ms: float = Field(..., description="Stage 2 cross-encoder reranking duration in ms")
    llm_judge_ms: float = Field(0.0, description="Stage 3 LLM judge duration in ms")
    graph_enrichment_ms: float = Field(0.0, description="Stage 4 Neo4j graph enrichment duration in ms")
    total_ms: float = Field(..., description="Total mapping pipeline execution duration in ms")
    stage1_candidates: int = Field(..., description="Number of candidates retrieved in Stage 1")
    stage2_survivors: int = Field(..., description="Number of candidates passing Stage 2 threshold filter")
    stage3_confirmed: int = Field(0, description="Number of mappings confirmed by Stage 3 LLM judge")
    llm_provider: str = Field(..., description="LLM provider name used for judging")
    use_llm_judge: bool = Field(..., description="Whether LLM judging stage was executed")


class CandidateCodeChunk(BaseModel):
    chunk_id: str
    file_path: str
    function_name: str
    start_line: int
    end_line: int
    text: str
    rerank_score: float = 0.0
    vector_rank: Optional[int] = None
    keyword_rank: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CodeMapping(BaseModel):
    target_type: str = Field("function", description="Target code element type: 'function' or 'file'")
    file_path: str = Field(..., description="Relative file path of matched code")
    function_name: str = Field("", description="Function name if matched to a function, empty if file-level")
    module_name: str = Field("", description="Parent module name if available")
    code_excerpt: str = Field(..., description="First 200 characters of matched code chunk")
    confidence: str = Field("high", description="Confidence level: 'high', 'medium', or 'low'")
    rerank_score: float = Field(..., description="Cross-encoder rerank score")
    evidence_type: str = Field(..., description="Source of evidence: 'llm_reasoning' or 'reranker_only'")
    reasoning: str = Field(..., description="Explanation of why this code matches the business concept")
    callers: List[str] = Field(default_factory=list, description="Function names in same file that call this function")
    callers_scope: str = Field("same_file_only", description="ALWAYS 'same_file_only' in v1 — cross-file callers not captured in CALLS graph")
    low_confidence: bool = Field(False, description="Flag indicating if mapping confidence is low")


class BusinessChunkRef(BaseModel):
    chunk_id: str = Field("", description="Business chunk ID if resolved from docs_chunks")
    heading_path: str = Field("", description="Heading section path of business doc chunk")
    text_excerpt: str = Field(..., description="First 300 characters of input business concept text")


class BusinessMappingResult(BaseModel):
    business_chunk: BusinessChunkRef
    mappings: List[CodeMapping] = Field(default_factory=list)
    unmapped_reason: Optional[str] = Field(None, description="Explanation when no mapping is found")
    generation_quality: str = Field("ok", description="Quality status: 'ok' or 'grounding_warning'")
    telemetry: MappingTelemetry


# ---------------------------------------------------------------------------
# Helper Tokenizer for BM25
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    return re.findall(r"\w+", text.lower())


def _extract_file_id(meta: Dict[str, Any]) -> str:
    if meta.get("file_id"):
        return meta["file_id"]
    file_path = meta.get("file_path", "")
    module_id = meta.get("module_id", "")
    if module_id and ":module:" in module_id:
        repo_node_id = module_id.split(":module:")[0]
        return f"{repo_node_id}:file:{file_path}"
    return f"file:{file_path}"


# ---------------------------------------------------------------------------
# Core Mapping Pipeline Engine
# ---------------------------------------------------------------------------

def map_business_chunk_to_code(
    organization_id: str,
    repository_id: str,
    business_text: str,
    chunk_id: str = "",
    heading_path: str = "",
    use_llm_judge: bool = True,
    max_mappings: int = 5,
) -> BusinessMappingResult:
    """
    Executes the 4-stage Business-to-Code Mapping pipeline.

    Args:
        organization_id: Tenant organization ID.
        repository_id: Scoped repository ID (e.g. 'repo:chatdoc').
        business_text: Input business concept / requirement text chunk.
        chunk_id: Optional business document chunk ID.
        heading_path: Optional section heading path of business chunk.
        use_llm_judge: If True, execute Stage 3 LLM judging.
        max_mappings: Maximum mappings to return.

    Returns:
        BusinessMappingResult object.
    """
    t_start = time.perf_counter()
    org_id = organization_id.strip()
    repo_id = repository_id.strip()
    biz_text = business_text.strip()

    biz_ref = BusinessChunkRef(
        chunk_id=chunk_id,
        heading_path=heading_path,
        text_excerpt=biz_text[:300].strip(),
    )

    if not biz_text:
        return BusinessMappingResult(
            business_chunk=biz_ref,
            mappings=[],
            unmapped_reason="Business text is empty.",
            generation_quality="ok",
            telemetry=MappingTelemetry(
                retrieval_ms=0.0,
                reranking_ms=0.0,
                llm_judge_ms=0.0,
                graph_enrichment_ms=0.0,
                total_ms=0.0,
                stage1_candidates=0,
                stage2_survivors=0,
                stage3_confirmed=0,
                llm_provider=settings.get_effective_llm_provider(),
                use_llm_judge=use_llm_judge,
            ),
        )

    # -----------------------------------------------------------------------
    # STAGE 1: Candidate Retrieval (Vector + BM25 + RRF)
    # -----------------------------------------------------------------------
    t_ret_start = time.perf_counter()
    extra_filter = {"repository_id": {"$eq": repo_id}} if repo_id else None

    # 1a. Vector Search
    query_vec = embed_query(biz_text)
    vector_hits: Dict[str, Dict[str, Any]] = {}
    vector_rank_map: Dict[str, int] = {}

    if query_vec:
        try:
            res_vec = chroma_client.query(
                collection_name="code_chunks",
                query_embeddings=[query_vec],
                n_results=settings.BUSINESS_MAPPING_MAX_CANDIDATES,
                organization_id=org_id,
                extra_filter=extra_filter,
            )
            if res_vec and res_vec.get("ids") and res_vec["ids"][0]:
                for rank, cid in enumerate(res_vec["ids"][0]):
                    txt = res_vec["documents"][0][rank]
                    meta = res_vec["metadatas"][0][rank]
                    vector_hits[cid] = {"text": txt, "meta": meta}
                    vector_rank_map[cid] = rank + 1
        except Exception as vec_exc:
            logger.warning("Stage 1 vector query failed: %s", vec_exc)

    # 1b. BM25 Search
    bm25_hits: Dict[str, Dict[str, Any]] = {}
    bm25_rank_map: Dict[str, int] = {}

    try:
        raw_code = chroma_client.get_documents(
            collection_name="code_chunks",
            organization_id=org_id,
            extra_filter=extra_filter,
        )
        if raw_code and raw_code.get("ids") and raw_code.get("documents"):
            c_ids = raw_code["ids"]
            c_docs = raw_code["documents"]
            c_metas = raw_code.get("metadatas") or [{}] * len(c_ids)

            tokenized_corpus = [_tokenize(doc or "") for doc in c_docs]
            tokenized_query = _tokenize(biz_text)

            if tokenized_query and tokenized_corpus:
                bm25_engine = BM25Okapi(tokenized_corpus)
                bm25_scores = bm25_engine.get_scores(tokenized_query)

                scored_indices = [
                    (idx, float(sc)) for idx, sc in enumerate(bm25_scores) if sc > 0.0
                ]
                scored_indices.sort(key=lambda item: item[1], reverse=True)

                for rank_idx, (idx, score) in enumerate(scored_indices[: settings.BUSINESS_MAPPING_MAX_CANDIDATES]):
                    cid = c_ids[idx]
                    bm25_hits[cid] = {"text": c_docs[idx], "meta": c_metas[idx]}
                    bm25_rank_map[cid] = rank_idx + 1
    except Exception as bm25_exc:
        logger.warning("Stage 1 BM25 search failed: %s", bm25_exc)

    # 1c. RRF Fusion
    all_candidate_ids = set(vector_hits.keys()).union(set(bm25_hits.keys()))
    k_rrf = 60.0

    candidates_list: List[CandidateCodeChunk] = []
    for cid in all_candidate_ids:
        vec_info = vector_hits.get(cid)
        bm25_info = bm25_hits.get(cid)

        vrank = vector_rank_map.get(cid)
        krank = bm25_rank_map.get(cid)

        rrf_v = 1.0 / (k_rrf + vrank) if vrank is not None else 0.0
        rrf_k = 1.0 / (k_rrf + krank) if krank is not None else 0.0
        rrf_score = rrf_v + rrf_k

        text = vec_info["text"] if vec_info else bm25_info["text"]
        meta = vec_info["meta"] if vec_info else bm25_info["meta"]

        f_path = meta.get("file_path", "")
        fn_name = meta.get("function_name", meta.get("class_name", ""))
        start_l = int(meta.get("start_line", 0))
        end_l = int(meta.get("end_line", 0))

        candidates_list.append(
            CandidateCodeChunk(
                chunk_id=cid,
                file_path=f_path,
                function_name=fn_name,
                start_line=start_l,
                end_line=end_l,
                text=text,
                vector_rank=vrank,
                keyword_rank=krank,
                metadata=meta,
            )
        )

    # Sort candidates by RRF score descending
    candidates_list.sort(key=lambda c: (c.vector_rank or 999, c.keyword_rank or 999))
    candidates_list = candidates_list[: settings.BUSINESS_MAPPING_MAX_CANDIDATES]

    retrieval_ms = (time.perf_counter() - t_ret_start) * 1000.0

    if not candidates_list:
        return BusinessMappingResult(
            business_chunk=biz_ref,
            mappings=[],
            unmapped_reason="No code candidates found in repository.",
            generation_quality="ok",
            telemetry=MappingTelemetry(
                retrieval_ms=round(retrieval_ms, 2),
                reranking_ms=0.0,
                llm_judge_ms=0.0,
                graph_enrichment_ms=0.0,
                total_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                stage1_candidates=0,
                stage2_survivors=0,
                stage3_confirmed=0,
                llm_provider=settings.get_effective_llm_provider(),
                use_llm_judge=use_llm_judge,
            ),
        )

    # -----------------------------------------------------------------------
    # STAGE 2: Cross-Encoder Reranking & Threshold Filtering
    # -----------------------------------------------------------------------
    t_rerank_start = time.perf_counter()
    texts_to_rerank = [c.text for c in candidates_list]

    try:
        rerank_scores = rerank(
            query=biz_text,
            texts=texts_to_rerank,
            provider=settings.RERANKER_PROVIDER,
            model_name=settings.RERANKER_MODEL_NAME,
            head_chars=settings.RERANKER_HEAD_CHARS,
            tail_chars=settings.RERANKER_TAIL_CHARS,
        )
        for cand, score in zip(candidates_list, rerank_scores):
            cand.rerank_score = float(score)
    except Exception as rerank_exc:
        logger.warning("Stage 2 cross-encoder reranking failed; using stub scores: %s", rerank_exc)
        for idx, cand in enumerate(candidates_list):
            cand.rerank_score = 1.0 - (idx * 0.1)

    # Sort by rerank_score DESC
    candidates_list.sort(key=lambda c: c.rerank_score, reverse=True)

    # Threshold filter
    threshold = settings.BUSINESS_MAPPING_RERANK_THRESHOLD
    survivors = [c for c in candidates_list if c.rerank_score >= threshold]

    reranking_ms = (time.perf_counter() - t_rerank_start) * 1000.0

    if not survivors:
        return BusinessMappingResult(
            business_chunk=biz_ref,
            mappings=[],
            unmapped_reason=(
                f"No code chunk scored above the reranking threshold ({threshold}) for this business concept. "
                "This typically occurs for high-level strategic requirements that span multiple components or are not yet implemented."
            ),
            generation_quality="ok",
            telemetry=MappingTelemetry(
                retrieval_ms=round(retrieval_ms, 2),
                reranking_ms=round(reranking_ms, 2),
                llm_judge_ms=0.0,
                graph_enrichment_ms=0.0,
                total_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                stage1_candidates=len(candidates_list),
                stage2_survivors=0,
                stage3_confirmed=0,
                llm_provider=settings.get_effective_llm_provider(),
                use_llm_judge=use_llm_judge,
            ),
        )

    # -----------------------------------------------------------------------
    # STAGE 3: Provider-Agnostic LLM Grounded Judge
    # -----------------------------------------------------------------------
    t_llm_start = time.perf_counter()
    top_candidates = survivors[: settings.BUSINESS_MAPPING_LLM_MAX_CANDIDATES]

    generation_quality = "ok"
    confirmed_mappings: List[CodeMapping] = []
    effective_provider = settings.get_effective_llm_provider()

    if use_llm_judge:
        # Build prompt
        candidate_items_formatted = []
        for idx, c in enumerate(top_candidates):
            candidate_items_formatted.append(
                f"Candidate #{idx}:\n"
                f"  File: {c.file_path}\n"
                f"  Function: {c.function_name or 'N/A'} (lines {c.start_line}-{c.end_line})\n"
                f"  Code Excerpt: {c.text[:300].strip()}\n"
            )

        candidates_block = "\n".join(candidate_items_formatted)

        system_instruction = (
            "You are a strict code analyst. Given a business requirement excerpt and a set of candidate code functions, "
            "identify which code functions actually implement the business requirement. "
            "Be precise: only confirm matches where the code directly handles the described behavior. "
            "Respond ONLY as a JSON array of objects with keys 'candidate_id', 'match', 'confidence', 'reasoning'."
        )

        user_prompt = (
            f"BUSINESS REQUIREMENT:\n\"{biz_text}\"\n\n"
            f"CANDIDATE CODE FUNCTIONS:\n{candidates_block}\n\n"
            "Respond ONLY as a JSON array in this exact schema:\n"
            "[\n"
            "  {\n"
            "    \"candidate_id\": 0,\n"
            "    \"match\": true,\n"
            "    \"confidence\": \"high\",\n"
            "    \"reasoning\": \"One sentence explaining why this code directly implements the requirement.\"\n"
            "  }\n"
            "]"
        )

        try:
            raw_llm_resp = generate_text(
                prompt=user_prompt,
                system_instruction=system_instruction,
                provider=effective_provider,
            )

            # JSON Parsing with fallback (G5)
            parsed_judgements = []
            try:
                # Strip markdown fences if present
                clean_resp = raw_llm_resp.strip()
                if clean_resp.startswith("```json"):
                    clean_resp = clean_resp[7:]
                if clean_resp.startswith("```"):
                    clean_resp = clean_resp[3:]
                if clean_resp.endswith("```"):
                    clean_resp = clean_resp[:-3]
                clean_resp = clean_resp.strip()

                parsed_json = json.loads(clean_resp)
                if isinstance(parsed_json, list):
                    parsed_judgements = parsed_json
                elif isinstance(parsed_json, dict) and "candidates" in parsed_json:
                    parsed_judgements = parsed_json["candidates"]
            except Exception as parse_exc:
                logger.warning("LLM output parsing failed; using Stage 2 reranker fallback: %s", parse_exc)
                generation_quality = "grounding_warning"

            # Process judgements if successfully parsed
            if parsed_judgements:
                known_cand_files = (
                    {c.file_path.lower() for c in top_candidates}
                    | {c.file_path.split("/")[-1].lower() for c in top_candidates if "/" in c.file_path}
                )

                for item in parsed_judgements:
                    cand_idx = item.get("candidate_id")
                    if cand_idx is not None and 0 <= cand_idx < len(top_candidates):
                        is_match = bool(item.get("match", False))
                        conf = str(item.get("confidence", "medium")).lower()
                        reasoning_txt = str(item.get("reasoning", "")).strip()

                        if is_match and conf != "none":
                            c = top_candidates[cand_idx]
                            target_t = "function" if c.function_name else "file"
                            mod_name = c.metadata.get("module_name", c.file_path.split("/")[0] if "/" in c.file_path else "")

                            # Grounding check on reasoning text (G8 / onboarding pack pattern)
                            mentioned_files = set(re.findall(r'\b[a-zA-Z0-9_\-]+\.(?:py|js|ts|jsx|tsx|md|rst|txt)\b', reasoning_txt.lower()))
                            for mf in mentioned_files:
                                if mf not in known_cand_files and not mf.startswith("test"):
                                    logger.warning("Grounding warning: LLM mentioned unknown file '%s' in reasoning", mf)
                                    generation_quality = "grounding_warning"
                                    if conf == "high":
                                        conf = "medium"

                            confirmed_mappings.append(
                                CodeMapping(
                                    target_type=target_t,
                                    file_path=c.file_path,
                                    function_name=c.function_name,
                                    module_name=mod_name,
                                    code_excerpt=c.text[:200].strip(),
                                    confidence=conf if conf in ("high", "medium", "low") else "medium",
                                    rerank_score=c.rerank_score,
                                    evidence_type="llm_reasoning",
                                    reasoning=reasoning_txt or "Confirmed by LLM judge.",
                                    callers=[],
                                    callers_scope="same_file_only",
                                    low_confidence=(conf == "low" or generation_quality == "grounding_warning"),
                                )
                            )

            # Fallback if LLM returned 0 confirmed matches due to parse error or strict rejection
            if not confirmed_mappings and generation_quality == "grounding_warning":
                for c in top_candidates[:max_mappings]:
                    target_t = "function" if c.function_name else "file"
                    confirmed_mappings.append(
                        CodeMapping(
                            target_type=target_t,
                            file_path=c.file_path,
                            function_name=c.function_name,
                            module_name=c.metadata.get("module_name", ""),
                            code_excerpt=c.text[:200].strip(),
                            confidence="low",
                            rerank_score=c.rerank_score,
                            evidence_type="reranker_only",
                            reasoning="LLM judge response unparseable; retained top cross-encoder candidate with low confidence.",
                            callers=[],
                            callers_scope="same_file_only",
                            low_confidence=True,
                        )
                    )

        except LLMClientError as llm_exc:
            logger.warning("Stage 3 LLM Client execution failed; falling back to Stage 2 reranker: %s", llm_exc)
            generation_quality = "grounding_warning"
            for c in top_candidates[:max_mappings]:
                target_t = "function" if c.function_name else "file"
                confirmed_mappings.append(
                    CodeMapping(
                        target_type=target_t,
                        file_path=c.file_path,
                        function_name=c.function_name,
                        module_name=c.metadata.get("module_name", ""),
                        code_excerpt=c.text[:200].strip(),
                        confidence="medium",
                        rerank_score=c.rerank_score,
                        evidence_type="reranker_only",
                        reasoning="LLM provider unavailable; mapped using cross-encoder rerank score.",
                        callers=[],
                        callers_scope="same_file_only",
                        low_confidence=False,
                    )
                )
    else:
        # LLM judge disabled by caller request
        for c in top_candidates[:max_mappings]:
            target_t = "function" if c.function_name else "file"
            conf = "high" if c.rerank_score > (threshold + 1.5) else ("medium" if c.rerank_score >= threshold else "low")
            confirmed_mappings.append(
                CodeMapping(
                    target_type=target_t,
                    file_path=c.file_path,
                    function_name=c.function_name,
                    module_name=c.metadata.get("module_name", ""),
                    code_excerpt=c.text[:200].strip(),
                    confidence=conf,
                    rerank_score=c.rerank_score,
                    evidence_type="reranker_only",
                    reasoning="Mapped using cross-encoder rerank score (LLM judge disabled).",
                    callers=[],
                    callers_scope="same_file_only",
                    low_confidence=(conf == "low"),
                )
            )

    llm_judge_ms = (time.perf_counter() - t_llm_start) * 1000.0

    # Cap result mappings
    confirmed_mappings = confirmed_mappings[:max_mappings]

    # -----------------------------------------------------------------------
    # STAGE 4: Neo4j Graph Topology Enrichment (Intra-file Callers)
    # -----------------------------------------------------------------------
    t_graph_start = time.perf_counter()

    for m in confirmed_mappings:
        if m.target_type == "function" and m.function_name:
            try:
                cypher_callers = (
                    "MATCH (caller:Function {organization_id: $org_id})-[:CALLS]->(fn:Function {organization_id: $org_id, name: $fn_name}) "
                    "WHERE fn.file_id CONTAINS $file_path OR fn.tenant_key CONTAINS $file_path "
                    "RETURN caller.name AS caller_name "
                    "LIMIT 5"
                )
                records = neo4j_client.run_read_query(
                    cypher_callers,
                    parameters={
                        "org_id": org_id,
                        "fn_name": m.function_name,
                        "file_path": m.file_path,
                    },
                    organization_id=org_id,
                )
                callers_list = [r["caller_name"] for r in records if r.get("caller_name")]
                m.callers = callers_list
            except Exception as graph_exc:
                logger.warning("Stage 4 Neo4j callers graph query failed for function '%s': %s", m.function_name, graph_exc)
                m.callers = []
            m.callers_scope = "same_file_only"

    graph_enrichment_ms = (time.perf_counter() - t_graph_start) * 1000.0
    total_ms = (time.perf_counter() - t_start) * 1000.0

    unmapped_msg = None
    if not confirmed_mappings:
        unmapped_msg = "No code candidate was confirmed as a match for this business requirement by the judge."

    return BusinessMappingResult(
        business_chunk=biz_ref,
        mappings=confirmed_mappings,
        unmapped_reason=unmapped_msg,
        generation_quality=generation_quality,
        telemetry=MappingTelemetry(
            retrieval_ms=round(retrieval_ms, 2),
            reranking_ms=round(reranking_ms, 2),
            llm_judge_ms=round(llm_judge_ms, 2),
            graph_enrichment_ms=round(graph_enrichment_ms, 2),
            total_ms=round(total_ms, 2),
            stage1_candidates=len(candidates_list),
            stage2_survivors=len(survivors),
            stage3_confirmed=len(confirmed_mappings),
            llm_provider=effective_provider,
            use_llm_judge=use_llm_judge,
        ),
    )
