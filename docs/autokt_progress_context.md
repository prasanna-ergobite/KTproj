# AutoKT — Project Context & Progress

Share this file at the start of the next chat to give full context on what's been built and what's next.

## What We're Building

AutoKT is an AI-powered platform that automates knowledge transfer (KT) and new-joiner induction. It reads a project's codebase and existing documents (notes, transcripts, wikis), builds a living knowledge graph + vector store, and uses that to generate onboarding packs, power semantic search, and score documentation health.

**Important scope note:** this is no longer being built as a hackathon demo — it's being built as a **solid, sellable SaaS product**. Decision made: get it working end-to-end first (functionally complete), then harden for enterprise-readiness (multi-tenancy is already being baked in now since it's expensive to retrofit; deeper hardening like full auth, audit logging, secrets management, clustering — comes after the core product works).

## Tech Stack (as actually being built — differs slightly from original plan)

| Layer | Choice |
|---|---|
| Backend framework | Python + FastAPI |
| Orchestration | LangChain + LangGraph |
| Knowledge graph | Neo4j **5 Community Edition** |
| Relational DB | **PostgreSQL** (added — holds users, sessions, tasks, chat history, KT generation records) |
| Vector store | ChromaDB |
| Code Parsing & Chunking | **Tree-sitter** (`tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript` AST parsing with fixed-window fallback) |
| LLM | **Not yet decided** — candidates: Azure OpenAI or Google Gemini (not Anthropic as originally drafted) |
| Git operations | GitPython |
| Frontend | **Plain React (Vite)** — not Next.js |
| Auth | JWT (placeholder only — hashing not implemented yet) |
| Deployment | Docker Compose — FastAPI + Neo4j + Chroma + Postgres |

## Done So Far

### 1. Folder structure & scaffolding
- Backend: FastAPI app under `backend/app` (`api/`, `graphs/`, `db/`, `models/`, `core/`).
- Frontend: plain React + Vite scaffold (Next.js scaffold was removed/replaced).
- Python venv created, all backend dependencies installed.

### 2. Docker Compose — verified working
- Services: `backend`, `neo4j`, `chroma`, `postgres` — **all confirmed Up & Healthy**.
- Named volumes added for all three data stores (data survives `docker-compose down`).
- LLM provider made generic (`LLM_PROVIDER` / `LLM_API_KEY` env vars) since provider isn't finalized.
- Known quirk: a tool called "Antigravity" reportedly couldn't parse `${VAR:-default}` shell-expansion syntax — worked around using static env values instead. Not a standard Docker Compose issue; specific to that tool.

### 3. PostgreSQL integration — complete
- Sync driver (`psycopg2-binary` + SQLAlchemy), not async — deliberate choice to avoid Alembic/async complexity.
- 5 tables implemented in `db_models.py`: `users`, `sessions`, `tasks`, `chat_histories`, `kt_generation_records`.
- UUID primary keys, JSONB for flexible fields, `ondelete="CASCADE"` + indexes on all FKs, timezone-aware timestamps throughout.
- Alembic migrations set up and applied (`alembic upgrade head`).
- Integration test passes, with cleanup fixed (no leftover junk rows on repeated runs).
- `hashed_password` field exists but hashing library (passlib/argon2) not wired up yet — intentional TODO, real auth is out of scope for now.

### 4. Neo4j integration — complete
- **Multi-tenancy baked in from the start**: every node has `organization_id`.
- **Community Edition workaround**: composite uniqueness constraints aren't supported in Community Edition, so a computed `tenant_key` (`f"{organization_id}:{id}"`) property is used instead, with a single-property uniqueness constraint on `tenant_key` + a separate range index on `organization_id` for fast tenant-wide queries.
- 12 node types implemented in `schemas.py` and `neo4j_client.py`: `Repository`, `Module`, `File`, `Person`, `Document`, `KTSession`, `Decision`, `Risk`, `Dependency`, `Topic`, `Function`, `Class`.
- Containment hierarchy: Repository → Module → File → Class / Function (explicit, non-skipped graph hierarchy).
- Relationships split into **structural** (deterministic, from parsing: `PART_OF`, `DEPENDS_ON`, `OWNS`, `IMPORTS`) vs. **semantic** (LLM-inferred, carry `source` + `confidence` properties: `REQUIRES_KNOWLEDGE_OF`, `DOCUMENTS`, `COVERS`).
- `Neo4jClient` implemented with: connection pooling, explicit `run_read_query()` / `run_write_query()` methods, typed exceptions (`Neo4jConnectionError`, `Neo4jQueryError`, `Neo4jConstraintError`), retry timeout (`max_transaction_retry_time=30s`), structured logging.
- FastAPI lifespan wired: connects + runs `create_constraints()` on startup for all 12 node labels, closes cleanly on shutdown.
- All 12 `tenant_key` constraints + 12 `organization_id` indexes confirmed ONLINE.

### 5. ChromaDB Vector Store Client — complete
- **Multi-tenancy baked in**: all document IDs stored as `f"{organization_id}:{doc_id}"` (scoped IDs), unscoped on read — same spirit as Neo4j `tenant_key`. Prevents cross-tenant ID collisions at the collection level.
- 3 collections created idempotently on startup: `docs_chunks`, `transcript_chunks`, `code_chunks`.
- Metadata schema on every stored embedding: `organization_id`, `module_id` (Optional — `""` for unlinked docs; Chroma v1 rejects `None`), `source_type`, `project_id`.
- `_build_where()` helper always emits explicit `{"$and": [...]}` for multi-clause filters — Chroma v1 does NOT implicitly AND multiple top-level keys.
- Typed exceptions: `ChromaConnectionError`, `ChromaCollectionError`, `ChromaQueryError`, `ChromaValidationError`.
- `add_documents()` validates `organization_id` is non-empty before any write (raises `ChromaValidationError`).
- `query()` and `get_documents()` guarantee all returned IDs are unscoped before returning to callers.
- `delete_by_metadata()` uses `collection.delete(where=...)` — confirmed supported in Chroma 1.x SQLite backend.
- FastAPI lifespan wired: `connect()` + `ensure_collections()` on startup, `close()` on shutdown.

### 6. Repo Ingestion Graph Pipeline — complete
- **Asynchronous task pipeline**: `POST /repos` returns HTTP 202 with `task_id` and runs ingestion in background via `FastAPI.BackgroundTasks`. `GET /repos/tasks/{task_id}` provides status and summary result polling.
- **Task tracking in Postgres**: `tasks` table updated with `0002_add_task_result_column` migration (`payload` = input params, `result` = JSON summary output). Background task runs under `SYSTEM_USER_ID` sentinel created on app startup.
- **Git parsing & security**: Remote `https://`, `http://`, `git://`, `ssh://` and SCP-like SSH (`git@host:org/repo.git`) repos parsed; `extract_repo_name()` extracts clean repo names. Local paths validated against `REPO_LOCAL_ALLOWED_ROOT` with path traversal guards (`pathlib.Path.resolve()`). Credentials sanitized via regex-based `sanitize_url()` (`re.sub`) across all exception/error strings before logging or saving to Postgres. Non-git local folders handled safely via directory copy fallback (records `is_git_repo=False`).
- **Neo4j graph MERGE**: Idempotent re-ingestion via Cypher `MERGE` on `tenant_key`. Builds `Repository` → `Module` → `File` → `Class` / `Function` hierarchy (`PART_OF` edges). Only files with extensions in `SOURCE_EXTENSIONS` (`.py`, `.js`, `.ts`, `.go`, `.java`, etc.) qualify for Module/File classification. Authors deduplicated by normalized email (`person:<email>`) with `OWNS` edges carrying commit counts and `last_commit_at` timestamps.
- **Vector indexing gate & status accuracy**: Checks `EMBEDDING_PROVIDER` setting. Skips vector generation if unconfigured (`chunks_count=0`, status marked `completed_without_embeddings`). Only reports `completed` when vector chunks are actually written. No dummy/fake embeddings.
- **Resource ceilings & handle safety**: Max 10,000 files per repo, files > 1 MB skipped, git log capped at 500 commits (sets `git_log_truncated=true`). Explicit `repo.close()` handle release in `finally:` block prevents Windows file lock issues prior to `shutil.rmtree()` cleanup.

### 7. Tree-sitter AST Code Chunking & Neo4j Function/Class Nodes — complete
- **Tree-sitter Parser Engine**: Integrated `tree-sitter>=0.22.0,<1.0.0` with `tree-sitter-python`, `tree-sitter-javascript`, and `tree-sitter-typescript` bindings.
- **Core Module (`app/core/ast_chunker.py`)**: `ASTChunk` dataclass supporting 4 chunk types: top-level functions (`parent_class=""`), class methods (`parent_class="<ClassName>"`), class-body chunks (`function_name=""`, `parent_class=""`), and fixed-window fallback chunks.
- **Header Formatting & Sub-Splitting**: Formatted headers (`# File:`, `# Class:`, `# Function:`) prepended to text for self-contained vector retrieval. Giant functions (>3000 chars) are sub-split while preserving AST metadata.
- **Fallback Safety**: Detects Tree-sitter syntax errors (`root.has_error`) and unsupported language extensions, safely falling back to sliding-window chunking.
- **Neo4j Extension**: Extended `NODE_LABELS` with `"Function"` and `"Class"`. Step 4b in `repo_ingestion.py` creates `Function` and `Class` graph nodes and `PART_OF` relationships (`Function -[:PART_OF]-> File`, `Class -[:PART_OF]-> File`, `Function -[:PART_OF]-> Class`).
- **Enriched Vector Metadata**: Step 6 in `repo_ingestion.py` attaches `chunking_method`, `start_line`, `end_line`, `function_name`, `class_name`, `parent_class`, and `file_path` metadata to code vector chunks.
- **Test Suite (31/31 Passed)**: 10 dedicated unit tests in `tests/test_ast_chunker.py` + 4 integration tests in `tests/test_repo_ingestion.py`. Full test suite passing in 11.04s.
- **Verified in Neo4j**: Live verification confirmed `constraint_function_tenant_key` and `constraint_class_tenant_key` online, and live `Function`/`Class` nodes correctly generated during ingestion.

### 8. Doc / Folder Ingestion Pipeline — complete
- **Multi-Tier Chunker (`app/core/doc_chunker.py`)**: Heading-aware chunker for Markdown and DOCX with section hierarchy, `heading_path` tracking, `parent_section_id` linking, code-fence/table guards, and short-section merging. Fixed-window chunker fallback (15% overlap) for PDF, TXT, and RST.
- **Multi-Format Document Parser (`app/core/doc_parser.py`)**: `pypdf>=4.0.0` for PDF page extraction, `python-docx>=1.1.0` for DOCX paragraph structure, UTF-8 text readers with error guards.
- **Graph & Vector Pipeline (`app/graphs/doc_ingestion.py`)**: Asynchronous task pipeline (`POST /docs/upload`, `GET /docs/tasks/{task_id}`). Two-phase dedup by relative path (`index_document_org_relpath` composite index) with stable `path_hash` tenant keys and automatic replace/cleanup of prior versions. Word-boundary path-matching (`_token_matches_module`) to link `Document` nodes to `Module` nodes via `DOCUMENTS` semantic edges (`confidence=1.0`). ChromaDB indexing gated on `EMBEDDING_PROVIDER`.
- **Test Suite**: 13 unit tests in `tests/test_doc_chunker.py` + 8 integration tests in `tests/test_doc_ingestion.py`.

### 9. Local Embedding Engine (`nomic-embed-text-v1.5`) & Vector Pipeline — complete
- **Local Embedding Engine (`app/core/embedder.py`)**: Integrated `nomic-ai/nomic-embed-text-v1.5` via `sentence-transformers 5.7.0` (with `einops`).
- **Prefix Convention Enforcement**: Automatically applies required task-conditioned prefixes: `search_document: <text>` for document/code ingestion into ChromaDB vs. `search_query: <text>` for semantic query search. Verified empirically that prefixes generate distinct task-conditioned 768-dimensional vector spaces for identical text.
- **Default Provider**: Set `EMBEDDING_PROVIDER="nomic"` as default in `config.py`. Connected `embed_documents()` to `chroma_client.add_documents()` across both `doc_ingestion.py` and `repo_ingestion.py`. Ingestion status now completes with status `"completed"` and live vector index.
- **Token Capacity Verified**: Measured actual real chunks from codebase and docs using Nomic tokenizer: max real code chunk (853 chars / 295 tokens, ~3.6% context), max real doc chunk (2,159 chars / 683 tokens, ~8.3% context) — safely inside Nomic's 8,192 token limit.
- **Dependencies Added**: `sentence-transformers>=3.0.0` and `einops>=0.7.0` added to `requirements.txt`.

### 10. Test Suite Verification & End-to-End Public Git Audits — complete
- **Full Test Suite (52/52 Passed — 100% Green)**: Tested across all 8 modules (`test_ast_chunker`, `test_chroma`, `test_doc_chunker`, `test_doc_ingestion`, `test_health`, `test_neo4j`, `test_postgres`, `test_repo_ingestion`).
- **Public Git Ingestion Audit #1 (`youtube-chatbot`)**: Remote public clone verified end-to-end. Extracted 1 committer, 7 files, 13 `Function` nodes in Neo4j, 19 code chunks in Chroma. Identified & fixed Windows Git pack file read-only permission issue during directory cleanup.
- **Public Git Ingestion Audit #2 (`Medicine-Diabetes-Assistant`)**: Multi-author, mixed Python/JS repository. Extracted 3 committers, 22 files, 76 `Function` nodes, 7 `Class` nodes, 153 vector chunks in Chroma. Added `"chroma_db"`, `"chroma"`, `".chroma"` to `EXCLUDED_DIRECTORIES`.

### 11. Code Chunks Metadata Schema Enrichment — complete
- Enriched `code_chunks` ChromaDB collection with 14 new metadata fields (24 total per chunk).
- Added `file_id`, `repository_id`, `chunk_type`, `language`, `tenant_key`, `commit_sha`, `last_modified_date`, `last_author_email`, `last_author_name`, `commit_count`, `has_parent_class`, `token_count`, `content_hash`, `visibility`.

### 12. Doc Chunks Metadata Schema Enrichment — complete
- Enriched `docs_chunks` ChromaDB collection with 13 new metadata fields (24 total per chunk).
- Added `chunk_type`, `chunk_total`, `file_format`, `content_hash`, `last_modified_date`, `ingested_at`, `version`, `uploaded_by`, `source_confidence`, `token_count`, `has_code_fence`, `is_stub_or_empty`, `visibility`.

### 13. Hybrid Search Engine (`GET /search`) — complete
- Implemented `GET /search` combining vector similarity search (ChromaDB `code_chunks` & `docs_chunks`) with single-pass batched graph context expansion (Neo4j).
- Uses `embed_query()` with Nomic's `search_query: ` prefix convention. Cypher `UNWIND` optimization on indexed `tenant_key`.
- Resilient fallback on Neo4j failure. Response includes `telemetry` metrics, curated metadata whitelist, pagination, and similarity score sorting.
- Test suite: 65/65 tests passed (100% green).

### 14. In-Repo Documentation Ingestion & Heading-Aware Routing — complete
- **Heading-Aware In-Repo Routing**: Documentation files discovered during repository walk are classified via `_is_doc_file()` and routed to `doc_chunker.py` (`heading_aware`), splitting cleanly by section headers instead of generic fixed-window code chunking.
- **Dual Vector Collection Placement & Cleanup**: In-repo docs are written to `docs_chunks` with enriched 24-field metadata. Re-ingestion purges previous chunks from both `code_chunks` and `docs_chunks`.
- **Neo4j Node & Git Ownership Model**: Creates `Document` nodes linked to modules via `DOCUMENTS` edges. Commit log history creates `OWNS` edges linking persons to both source code files and documentation.
- **Real-World Empirical Validation**: Live public GitHub repo ingested confirming real token counts, SHA-256 chunk hashes, git author ownership, and `0.5364` similarity score (+73% improvement over fixed-window baseline).
- **Test Suite**: 4/4 passed in `tests/test_in_repo_doc_routing.py`.

### 15. BM25 Keyword Search, RRF Fusion & Cross-Encoder Reranking — complete
- **BM25 Keyword Search & RRF Fusion**: Added full-corpus `BM25Okapi` keyword search and Reciprocal Rank Fusion (RRF, $k=60$) combining vector and keyword ranks into `rrf_score`.
- **Score-Based Cutoff**: Implemented `score > 0.0` BM25 filtering and `top_k_bm25 = max(candidate_fetch_count, 100)` candidate pool cap.
- **Cross-Encoder Reranking Stage**: Integrated `cross-encoder/ms-marco-MiniLM-L-6-v2` via `app/core/reranker.py` as the final precision stage. Reranks top-$K$ candidates using `rerank_score DESC`.
- **Head+Tail Truncation**: Applied head+tail truncation (`RERANKER_HEAD_CHARS=250`, `RERANKER_TAIL_CHARS=150`) to fit within the 512-token context window.

### 16. Developer Onboarding Pack Generator — complete
- **Task-Based Asynchronous Pipeline (`POST /onboarding-pack`, `GET /onboarding-pack/tasks/{task_id}`)**: Follows 202 + `task_id` + Postgres `tasks` status polling architecture.
- **Data Assembly Pipeline (`app/graphs/onboarding_pack.py`)**: Assembles structural graph topology from Neo4j, git commit authorship, linked documents, function documentation coverage, and repo-scoped dependency manifest parser.
- **Provider-Agnostic LLM Engine (`app/core/llm_client.py`)**: Supports `"stub"`, `"gemini"`, `"azure"`, and `"azure_foundry"` (Azure AI Foundry / OpenAI-compatible endpoint with `gpt-5-mini`).
- **Deterministic Markdown Renderer (`app/core/onboarding_renderer.py`)**: Converts structured 10-section JSON pack output into human-readable Markdown.
- **Hallucination & Accuracy Guardrails**: 8 of 10 sections derived directly from Neo4j/Chroma graph data. Post-generation grounding check detects ungrounded mentions and flags `generation_quality: "grounding_warning"`.
- **Test Suite (96/96 Tests Passing)**: Full backend test suite 100% passing.

### 17. KT Prep Questions Generator — complete
- **Task-Based Asynchronous Pipeline (`POST /kt-prep`, `GET /kt-prep/tasks/{task_id}`)**: Follows same 202 + `task_id` + Postgres `tasks` status polling architecture.
- **Graph-Grounded Context Assembly**: Assembles candidate questions from Neo4j graph topology (module ownership, function/class relationships, document coverage gaps) and Chroma vector search results.
- **LLM-Powered Question Generation**: Uses `llm_client.py` to generate structured KT questions per module/function domain.
- **Deduplication & Scoring**: Questions deduplicated and scored by graph coverage and semantic relevance.

### 18. Business Document Ingestion Pipeline — complete
- **Separate Pipeline (`app/graphs/business_doc_ingestion.py`)**: Dedicated ingestion path for business documents (PRDs, BRDs, specs) uploaded via `POST /business-docs/upload`, parallel to the code/doc ingestion pipelines.
- **Repository & Module Linking**: Business documents linked via explicit `(d:Document)-[:DOCUMENTS]->(r:Repository)` and optionally `(d:Document)-[:DOCUMENTS]->(m:Module)` edges when `module_id` is provided at upload time.
- **Dedup & Versioning**: Same `content_hash`-based dedup logic as `doc_ingestion.py`. Detects re-uploads of changed files, increments `version`, purges old Chroma chunks and Neo4j Document node, re-ingests fresh.
- **Verified**: `ChatDoc_PRD.pdf` successfully ingested and confirmed present in both Neo4j graph and `docs_chunks` ChromaDB collection.

### 19. PDF Heading-Aware Chunking — complete
- **Problem confirmed empirically**: All 8 chunks of `ChatDoc_PRD.pdf` previously showed `heading_path: ""` and `# Section: root` — the PDF had clear numbered section structure (`4. Product Overview`, `5.1 Primary User Personas`, etc.) that was being completely discarded.
- **Detection Engine (`app/core/doc_parser.py`)**: Implemented `_detect_pdf_headings_and_convert_to_md(pages_text: List[str])`:
  - **Cross-page running noise removal**: Short lines (< 120 chars) appearing identically on ≥ 2 pages are stripped as running headers/footers.
  - **TOC line suppression**: `_TOC_LINE_PATTERN = re.compile(r"^\d+(?:\.\d+)*\.?\s+.+\s+\d{1,3}$")` filters TOC entries ending in bare page numbers, preventing them from masquerading as body section headings.
  - **Heading classification**: Numbered headings (`1. `, `4.1 `, `5.1.2 `) and named section prefixes (`Section`, `Chapter`, `Appendix`) → Markdown `#`, `##`, `###`.
  - **Fallback gate**: If < 2 top-level H1 headings survive, falls back cleanly to `fixed_window`. Prevents single-section or unstructured PDFs from being misclassified as heading-aware.
- **No new dependencies**: Pattern-based heuristics on the existing `pypdf` text stream only. No `pdfplumber` or `PyMuPDF`.
- **Known Limitation (documented, not fixed)**: Heuristics were derived from `ChatDoc_PRD.pdf`'s specific conventions. PDFs without numbered headings (LaTeX papers, Confluence exports, scanned documents) will fall back to `fixed_window` — this is the correct safe behavior.
- **DOCX silent bug identified (separate PR)**: `doc_parser.py` DOCX path strips `Heading 1/2/3` styles, causing DOCX to silently produce `fixed_window` chunks. `_heading_aware_chunk_docx_paragraphs()` in `doc_chunker.py` is dead code (uninvoked). Fix deferred to a separate DOCX PR.
- **Test Suite**: 8 new tests in `tests/test_doc_parser.py` (all passing) including `test_pdf_toc_heading_interaction` — a realistic multi-page test using 3 simulated pages mirroring `ChatDoc_PRD.pdf`'s structure (identical TOC and body section numbers). 1 new test in `tests/test_doc_chunker.py`. All 24 tests pass.
- **Live Verification**: Executed `scratch/reingest_chatdoc_prd.py` on real `ChatDoc_PRD.pdf`. Chunks now show full nested `heading_path` (e.g., `['8. RAG Pipeline & Vector Processing Specifications', '8.2 Embedding & Similarity Search']`, `['10. Non-Functional Requirements & Security', '10.1 Performance & Scalability']`) — zero `# Section: root` chunks remaining.

### 20. Business-to-Code Mapping — complete
- **Feature**: Connects business requirements described in ingested business documents (PRDs, BRDs) to source code modules and functions.
- **Engine (`app/core/business_mapping.py`)**: 4-stage pipeline:
  1. *Stage 1 (Retrieval)*: Vector search + BM25 keyword search + RRF fusion ($k=60$) scoped to target repository.
  2. *Stage 2 (Reranking & Filtering)*: Cross-encoder scoring (`ms-marco-MiniLM-L-6-v2`) with threshold filtering (`rerank_score >= 0.5`). Returns honest `unmapped_reason` when no candidates survive.
  3. *Stage 3 (LLM Grounded Judge)*: Provider-agnostic LLM judging with strict JSON output schema parsing, regex grounding check against candidate files (flagging hallucinated files as `grounding_warning`), and fallback to Stage 2 reranker-only on LLM parse errors or API failures.
  4. *Stage 4 (Graph Enrichment)*: Neo4j 1-hop same-file callers query (`-[:CALLS]->`). Enforces mandatory `callers_scope: "same_file_only"` on every CodeMapping to explicitly signal the intra-file graph boundary.
- **API Layer (`app/api/business_mapping.py`)**:
  - `POST /business-mapping`: Synchronous single-chunk mapping endpoint (HTTP 200) with XOR validation (`chunk_id` vs `business_text`).
  - `POST /business-mapping/document`: Asynchronous full-document batch mapping pipeline (HTTP 202).
  - `GET /business-mapping/tasks/{task_id}`: Standard task status polling endpoint.
- **Calibration & Reranker Validation (`scratch/calibrate_mapping_threshold.py`)**:
  - Evaluated cross-encoder logits across clean requirements and raw page-split PRD chunks using the exact production pipeline.
  - Verified empirical logit distributions: exact function matches (e.g. `FR-102` $\to$ `get_chunks`) score **+2.75** (positive logit); moderate concept matches score **-2.1 to -2.7**; raw page-split chunks score **-5.6 to -6.6**; irrelevant candidates score **-8.0 to -9.5**.
  - **Threshold Confirmed**: `0.5` threshold is kept for high-confidence reranker-only mode (0 false positives).
### 21. DOCX Heading-Aware Chunking Fix — complete
- **Problem**: `doc_parser.py` previously stripped `Heading 1/2/3` paragraph style names when reading `.docx` files, causing Word documents to fall back to `fixed_window` chunking despite `parse_document()` returning `chunking_method="heading_aware"`.
- **Parser Fix (`app/core/doc_parser.py`)**:
  - Replaced raw `.text` paragraph joining with style-aware paragraph extraction (`p.style.name`).
  - Added defensive `p.style` checks (`style_name = (p.style.name or "").lower() if p.style else ""`).
  - Maps `Heading 1` $\to$ `#`, `Heading 2` $\to$ `##`, and `Heading 3+` (including Headings 4–9) $\to$ `###`.
- **Chunker Cleanup (`app/core/doc_chunker.py`)**:
  - Updated docstring of `_heading_aware_chunk_docx_paragraphs()` to `RETIRED (Milestone 21)`, documenting that style conversion occurs upstream in `doc_parser.py`.
- **Test Suite (`tests/test_doc_parser.py`)**:
  - Updated module docstring to document DOCX heading style preservation.
  - **Inverted & Renamed Test 8**: `test_docx_heading_styles_preserved_as_markdown` (asserts `# Heading 1 Text` & `## Heading 2 Subtitle`).
  - **Added Test 9**: `test_docx_heading_aware_full_pipeline` (asserts `h1_chunk.section_level == 1`, `h2_chunk.section_level == 2`, and `h2_chunk.parent_section_id == h1_chunk.chunk_id`).
  - **Added Test 10**: `test_docx_no_headings_falls_back_to_fixed_window` (plain text DOCX fallback verification).
- **Full Suite Verification**: **157 PASSED, 1 SKIPPED, 0 FAILED** (up from 156+1 baseline).

---

## Not Done Yet — Next Steps (in order)

1. **KT Health Score** — Coverage math + gap summary per module (documentation coverage, function ownership, stale code signals).
2. **Audio transcription** (Whisper) — Last, lower priority.

## Deferred Enterprise-Hardening Items (post-MVP, not blocking current work)

- Real auth (password hashing, RBAC, SSO/SAML for enterprise buyers)
- Secrets out of `docker-compose.yml` into a proper secrets manager
- Audit logging (who accessed what, for compliance)
- Neo4j clustering/backup strategy (currently single instance)
- Observability/monitoring (ingestion job failures, LLM cost spikes)
- CI-run test suite (currently manual `pytest` runs)
- LLM provider final decision (Azure OpenAI vs. Gemini)

## Known Limitations (Documented, Not Fixed)

These are intentional simplifications with known downstream impact. Recorded here so future work accounts for them explicitly.

### 1. Git metadata in `code_chunks` is file-granularity, not function-granularity
`commit_sha`, `last_modified_date`, `last_author_email`, `last_author_name`, and `commit_count` in every Chroma `code_chunks` record are computed once per file and copied to all chunks in that file. A function untouched for 2 years will show a recent date if any other line in the same file changed.

**Why accepted:** Accurate per-function blame requires `git blame --porcelain` per function range — 10–100× slower for large repos.

**KT Health Score impact:** Treat these as file-staleness proxies, not function-staleness indicators.

### 2. `is_documented` uses a text heuristic with known false negatives
`is_documented = True` when the first non-blank, non-signature body line starts with `"""`, `'''`, `//`, or `/*`. Known blind spots: docstring separated from signature by blank line, JSDoc comment preceding function declaration if separated.

### 3. No backfill for code chunks ingested before Milestone 11
Chroma records written before metadata enrichment do not have the 14 new fields. **Decision: no backfill migration.** Downstream consumers access new fields defensively: `.get("token_count", 0)`, `.get("chunk_type", "unknown")`.

### 4. `last_modified_date` on doc chunks is the upload timestamp, not original file mtime
A reliable original mtime would require the client to send it explicitly (future work — optional `Form` field on `/docs/upload`). For now, `last_modified_date` equals `ingested_at`.

**Downstream impact**: Do not use `last_modified_date` on doc chunks as a staleness signal. Use Neo4j `Document.content_hash` to detect changed files instead.

### 5. `uploaded_by` on doc chunks is not trackable yet
`run_doc_ingestion_task()` runs in a background task with no request-context access. Stored as `""` sentinel. Future work: pass `user_id` from API layer through task payload.

### 6. No backfill for doc chunks ingested before Milestone 12
Chroma records in `docs_chunks` written before metadata enrichment do not have the 13 new fields. **Decision: no backfill migration.** Downstream consumers access new fields defensively.

### 7. `source_confidence` on doc chunks is categorical string, not a numeric float
The DOCUMENTS edge carries `{confidence: 1.0, source: "path_match"}` but confidence is hardcoded `1.0` for all path-matched documents. Numeric score deferred until LLM semantic matching introduces variable confidence values.

### 8. BM25 keyword search rebuilds its index in-process on every `GET /search` request
At current data volumes (~500–3,000 chunks per tenant) this costs ~5–20ms per query. **Trigger for revisiting:** when a single tenant's chunk count consistently exceeds ~50,000 chunks or BM25 corpus fetch + index build latency exceeds 150ms, upgrade to a persistent FTS index (SQLite FTS5 recommended as first step).

### 9. BM25 candidate selection uses score-filtering before candidate pool count capping
BM25 candidates strictly filtered by `score > 0.0` to drop terms with zero keyword relevance before a candidate pool count cap (`top_k_bm25 = max(candidate_fetch_count, 100)`) is applied.

### 10. `total_results` reflects reranked pool size, not full match count (as of cross-encoder milestone)
`total_results` is set to `len(rrf_top_k)` — capped at `RERANKER_TOP_K` (default 30). A query matching 200 chunks will report `total_results=30`. Clients needing full corpus match count must use a separate `rrf_candidate_count` field (future schema addition).

### 11. PDF heading detection is tuned to numbered-heading format (ChatDoc_PRD.pdf conventions)
`_detect_pdf_headings_and_convert_to_md()` was derived from and validated against one real document's conventions. PDFs without numbered section headings (LaTeX papers, Confluence exports, Google Docs, scanned PDFs) will fall back to `fixed_window` cleanly — not misdetected. This is the correct safe behavior. A more general approach requires `pdfplumber` or `PyMuPDF` for font-size/layout data (deferred).

### 12. DOCX heading-aware chunking fix — complete (Milestone 21)
`doc_parser.py` updated to map python-docx `Heading 1/2/3` styles to Markdown `#`/`##`/`###` headings before chunking. Chunker `MIN_SECTION_TOKENS` reduced from 50 to 15 to prevent merging small sections, and `merged_prefix` formatting bug resolved. Heading-aware structures (including 3-tier deep DOCX hierarchies) verified end-to-end.

### 13. CALLS graph edges are intra-file only (`callers: []` is NOT a complete negative)
Stage 4 graph enrichment queries Neo4j for `-[:CALLS]->` function relationships. Since `repo_ingestion.py` extracts CALLS edges strictly within individual files, functions called by orchestrators in *different* files (e.g., `app.py` calling `document_utils.load_documents`) have no function-level CALLS edges. Every `CodeMapping` carries mandatory `callers_scope: "same_file_only"` to explicitly signal this graph boundary to consumers so empty `callers` arrays are never misread as a complete negative answer.

### 14. Cross-encoder threshold calibration logit characteristics
Empirical cross-encoder (`ms-marco-MiniLM-L-6-v2`) logits range from positive (exact semantic/keyword matches score $> +2.0$, e.g., `FR-102` $\to$ `get_chunks` at `+2.75`) to negative for broader concept matches ($-2.1$ to $-6.6$). The default `BUSINESS_MAPPING_RERANK_THRESHOLD = 0.5` ensures zero false positives in automated reranker-only mode, but queries against raw page-split fragments require BM25 keyword boosting or an LLM-judge candidate floor ($-3.0$) to surface candidates for stage 3 judging.

### 15. OWNS relationship `last_commit_at` reflects ingestion timestamp, not commit date
`repo_ingestion.py:L688` sets `"last_commit": datetime.now(timezone.utc).isoformat()` when merging `OWNS` edges between `Person` and `File` nodes, instead of using the actual git commit timestamp (`file_git_meta[f_id]["last_modified_date"]` computed at L639).

**Downstream impact**: `last_commit_at` cannot be used as a staleness signal in Milestone 22 KT Health Score. `overall_score` is weighted 40% `doc_score` and 60% `ownership_score`. Fix scheduled as Milestone 22.5 fast-follow (~30 min code change + repo re-ingest).

---

## Milestone 22: KT Health Score — Complete

Quantitative module health scoring endpoint implemented in `app/core/health_score.py` and `app/api/health_score.py`.

- **Endpoints**:
  - `GET /health-score/repo/{repository_id}?organization_id=...` (lists top 20 modules worst-first)
  - `GET /health-score/module/{module_id:path}?organization_id=...` (single module score & gaps)
- **Score formula**: `overall_score = 0.40 * doc_score + 0.60 * ownership_score`
  - `doc_score`: `coverage_pct` + 10 (has README) + min(linked_docs * 5, 15), capped at 100
  - `ownership_score`: `min(bus_factor / 3.0, 1.0) * 100` (bus_factor = unique owners with commits > 0)
- **Risk signals**: `sole_owner_risk` (bus_factor == 1), `unowned` (bus_factor == 0), human-readable `gaps` list.
- **Validation**: 15 unit/API tests + 172/172 full suite pass (1 skipped, 0 failed).



