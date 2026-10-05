"""
Developer Onboarding Pack Generation Pipeline.
Assembles structural graph context from Neo4j, vector metadata & semantic snippets
from ChromaDB, and synthesizes an onboarding pack using the LLM client.
"""

import re
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from app.core.config import settings
from app.db.neo4j_client import Neo4jClientError
from app.core.llm_client import generate_text, LLMClientError
from app.core.onboarding_renderer import render_markdown
from app.core.module_context import assemble_module_context, ModuleNotFoundError
from app.db.task_helpers import update_task_completed, update_task_failed

logger = logging.getLogger("autokt.onboarding_pack")


def run_onboarding_pack_task(
    task_id: str,
    module_id: str,
    organization_id: str,
    include_markdown: bool = True,
) -> Dict[str, Any]:
    """
    Executes the background onboarding pack generation pipeline:
    1. Assemble module context (Neo4j topology, Chroma metadata, code owners, docs, requirements, coverage).
    2. Compute suggested first onboarding tasks.
    3. Generate prose sections via LLMClient (module_purpose, onboarding_narrative).
    4. Perform post-generation grounding check.
    5. Render pre-compiled Markdown document.
    6. Persist result payload into Postgres task table.
    """
    t_start = time.perf_counter()
    llm_time_ms = 0.0

    org_id = organization_id.strip()
    mod_id = module_id.strip()

    # Assemble shared module context from Neo4j & ChromaDB
    try:
        ctx = assemble_module_context(mod_id, org_id)
    except Neo4jClientError as exc:
        logger.error("Failed Neo4j query for module topology: %s", exc)
        update_task_failed(task_id, f"Graph database query error: {exc}")
        return {"error": str(exc)}
    except ModuleNotFoundError as exc:
        logger.warning(str(exc))
        update_task_failed(task_id, str(exc))
        return {"error": str(exc)}

    # Compute suggested first tasks (Rule-Based Computed, specific to Onboarding Pack)
    suggested_tasks = []
    if not ctx.has_readme:
        suggested_tasks.append({
            "priority": "high",
            "description": f"Create a README.md for module '{ctx.module_name}'",
            "rationale": "Module has no linked overview or README documentation.",
        })
    if ctx.undocumented_count > 0:
        suggested_tasks.append({
            "priority": "medium",
            "description": f"Add docstrings to {ctx.undocumented_count} undocumented functions",
            "rationale": f"Estimated function documentation coverage is {ctx.coverage_pct:.1f}%.",
        })
    if not ctx.entry_points_list:
        suggested_tasks.append({
            "priority": "low",
            "description": "Identify and document main module entry points",
            "rationale": "No top-level exported functions were classified in AST parsing.",
        })

    # Generate LLM Prose (Purpose & Narrative)
    t_l0 = time.perf_counter()

    prompt_context = (
        f"Module Name: {ctx.module_name}\n"
        f"Module Path: {ctx.module_path}\n"
        f"Key Files: {', '.join(f['filename'] for f in ctx.key_files_list[:10])}\n"
        f"Entry Functions: {', '.join(fn['function_name'] for fn in ctx.entry_points_list[:10])}\n"
        f"Code Owners: {', '.join(o['name'] + ' <' + o['email'] + '>' for o in ctx.owners_list[:5])}\n"
        f"Linked Docs: {', '.join(d['filename'] for d in ctx.existing_docs_list)}\n\n"
        f"Code & Doc Excerpts:\n"
        + "\n---\n".join(ctx.semantic_snippets[:6])
    )

    sys_prompt = (
        "You are a senior software architect writing an onboarding guide for a new engineer. "
        "You are provided with REAL data extracted from the codebase knowledge graph and vector store. "
        "STRICT GROUNDING RULE: Never invent file names, function names, class names, or developer names. "
        "Only summarize and narrate over the context provided below. If context is missing, say so concisely."
    )

    purpose_prompt = f"{prompt_context}\n\nTask: Write a 2-3 paragraph technical overview of the purpose and responsibilities of module '{ctx.module_name}'."
    narrative_prompt = f"{prompt_context}\n\nTask: Write a 1 paragraph 'Getting Started' guide recommending how a new developer should start reading and exploring module '{ctx.module_name}'."

    try:
        module_purpose_text = generate_text(prompt=purpose_prompt, system_instruction=sys_prompt)
        narrative_text = generate_text(prompt=narrative_prompt, system_instruction=sys_prompt)
    except LLMClientError as llm_err:
        logger.warning("LLM client generation failed: %s. Falling back to stub prose.", llm_err)
        module_purpose_text = f"Module '{ctx.module_name}' contains {len(ctx.key_files_list)} files and {ctx.total_func_count} functions/methods across path '{ctx.module_path}'."
        narrative_text = f"To explore module '{ctx.module_name}', begin by reading entry points and reviewing key files ({', '.join(f['filename'] for f in ctx.key_files_list[:3])})."

    llm_time_ms += (time.perf_counter() - t_l0) * 1000

    # Post-Generation Grounding Verification
    generation_quality = "ok"
    known_people = {o["name"].lower() for o in ctx.owners_list} | {o["email"].lower() for o in ctx.owners_list}
    known_files = (
        {f["filename"].lower() for f in ctx.key_files_list}
        | {d["filename"].lower() for d in ctx.existing_docs_list}
        | {mf.split("/")[-1].lower() for mf in ctx.project_requirements_data.get("manifest_files", [])}
        | {"requirements.txt", "package.json", "pyproject.toml", "pipfile", "environment.yml", "setup.py", "readme.md", "contributing.md"}
    )

    combined_gen_text = (module_purpose_text + " " + narrative_text).lower()

    # Grounding check 1: If LLM mentions an email pattern not in known_people
    mentioned_emails = set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', combined_gen_text))
    for email in mentioned_emails:
        if email not in known_people and "autokt" not in email:
            logger.warning("Grounding warning: LLM mentioned unknown email '%s'", email)
            generation_quality = "grounding_warning"

    # Grounding check 2: If LLM mentions a specific filename with extension not in known_files
    if known_files:
        mentioned_files = set(re.findall(r'\b[a-zA-Z0-9_\-]+\.(?:py|js|ts|jsx|tsx|md|rst|txt|json|yaml|yml)\b', combined_gen_text))
        for fname in mentioned_files:
            if fname not in known_files:
                logger.warning("Grounding warning: LLM mentioned unknown file '%s'", fname)
                generation_quality = "grounding_warning"

    # Response Assembly & Task Persistence
    total_time_ms = (time.perf_counter() - t_start) * 1000

    pack_result = {
        "module_id": ctx.module_id,
        "module_name": ctx.module_name,
        "module_path": ctx.module_path,
        "organization_id": org_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generation_quality": generation_quality,
        "sections": {
            "module_purpose": {
                "source": "llm_generated",
                "content": module_purpose_text,
            },
            "key_files": {
                "source": "graph",
                "files": ctx.key_files_list,
            },
            "entry_points": {
                "source": "graph",
                "functions": ctx.entry_points_list,
            },
            "who_to_talk_to": {
                "source": "graph",
                "caveat": "File-level ownership only (file-granularity last-commit heuristic, not per-function blame — AUTHORED edges carry the same approximation)",
                "owners": ctx.owners_list,
            },
            "existing_docs": {
                "source": "graph",
                "documents": ctx.existing_docs_list,
            },
            "dependencies": {
                "source": "graph",
                "status": "available" if ctx.project_requirements_data.get("declared_dependencies") else "not_yet_available",
                "reason": "" if ctx.project_requirements_data.get("declared_dependencies") else "No declared dependencies found in repository manifest.",
                "related_modules": [],
            },
            "project_requirements": ctx.project_requirements_data,
            "doc_coverage": ctx.doc_coverage_data,
            "suggested_first_tasks": {
                "source": "computed",
                "tasks": suggested_tasks,
            },
            "onboarding_narrative": {
                "source": "llm_generated",
                "content": narrative_text,
            },
        },
        "known_limitations": [
            "owner_granularity: OWNS (Person->File) and AUTHORED (Person->Function/Class) both use file-level last-author heuristic, not per-function git blame — a function untouched for years will attribute to whoever last touched the file",
            "doc_linking: DOCUMENTS->Function/Class uses co-location heuristic (same dir + stem match, confidence 0.8) — in-text function name mentions in doc bodies are not yet detected; this is a planned Phase 3 enhancement",
            "embedding_provider_disabled: when EMBEDDING_PROVIDER is unconfigured, function documentation coverage and chunk metadata enrichment fallback to defaults",
        ],
        "telemetry": {
            "graph_query_ms": round(ctx.graph_time_ms, 2),
            "vector_query_ms": round(ctx.vector_time_ms, 2),
            "llm_generation_ms": round(llm_time_ms, 2),
            "total_ms": round(total_time_ms, 2),
            "llm_provider": settings.get_effective_llm_provider(),
            "llm_model": settings.LLM_MODEL,
        },
    }

    if include_markdown:
        pack_result["markdown"] = render_markdown(pack_result)

    # Persist completed task result into Postgres
    update_task_completed(task_id, pack_result)
    logger.info("Onboarding pack task '%s' completed successfully in %.2fms.", task_id, total_time_ms)
    return pack_result
