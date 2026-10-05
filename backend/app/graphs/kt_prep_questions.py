"""
KT Prep Questions Graph Pipeline.
Assembles module context, identifies documentation/ownership/architecture gaps,
and synthesizes a targeted list of KT preparation questions for outgoing engineers.
"""

import re
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

from app.core.config import settings
from app.db.neo4j_client import Neo4jClientError
from app.core.llm_client import generate_text, LLMClientError
from app.core.module_context import assemble_module_context, ModuleContext, ModuleNotFoundError
from app.db.task_helpers import update_task_completed, update_task_failed

logger = logging.getLogger("autokt.kt_prep_questions")


def _build_gap_signals(ctx: ModuleContext) -> List[Dict[str, Any]]:
    """
    Inspects assembled ModuleContext to extract structured gap signals.

    Returns:
        List of signal dicts sorted by severity.
    """
    signals: List[Dict[str, Any]] = []

    # 1. Complex undocumented functions (high token count + undocumented)
    for (f_id, fn_name, start_l), meta in ctx.chroma_func_meta.items():
        is_doc = meta.get("is_documented", False)
        t_count = meta.get("token_count", 0)
        c_type = meta.get("chunk_type", "function")
        p_class = meta.get("parent_class", "")

        if not is_doc and t_count > 200:
            signals.append({
                "type": "complex_undocumented",
                "severity": 1,
                "description": f"Function '{fn_name}' ({t_count} tokens) is undocumented and complex",
                "entity": fn_name,
                "token_count": t_count,
            })
        elif not is_doc and c_type == "method" and p_class:
            signals.append({
                "type": "undocumented_method",
                "severity": 4,
                "description": f"Method '{p_class}.{fn_name}' is undocumented",
                "entity": f"{p_class}.{fn_name}",
            })

    # 2. Undocumented top-level entry functions
    for fn in ctx.entry_points_list:
        if not fn.get("is_documented", False):
            fn_name = fn.get("function_name", "")
            f_path = fn.get("file_path", "")
            signals.append({
                "type": "undocumented_function",
                "severity": 3,
                "description": f"Entry function '{fn_name}' in '{f_path}' has no docstring",
                "entity": fn_name,
            })

    # 3. Sole owner file / Single committer risk
    if len(ctx.owners_list) == 1:
        owner_name = ctx.owners_list[0].get("name", "Unknown")
        signals.append({
            "type": "sole_owner_file",
            "severity": 2,
            "description": f"All files in module are owned solely by '{owner_name}' (bus-factor risk; ownership is file-level last-author, not per-function)",
            "entity": owner_name,
        })
    elif not ctx.owners_list:
        signals.append({
            "type": "no_owners",
            "severity": 2,
            "description": "No git commit authorship records found for files in this module",
            "entity": "",
        })

    # 4. Missing documentation & README
    if not ctx.existing_docs_list:
        signals.append({
            "type": "no_linked_docs",
            "severity": 5,
            "description": "No documentation files linked to this module",
            "entity": "",
        })

    if not ctx.has_readme:
        signals.append({
            "type": "no_readme",
            "severity": 6,
            "description": "Module has no linked README or overview document",
            "entity": "",
        })

    # 5. Low coverage pct
    if ctx.coverage_pct < 50.0 and ctx.total_func_count > 0:
        signals.append({
            "type": "low_doc_coverage",
            "severity": 7,
            "description": f"Module function documentation coverage is low ({ctx.coverage_pct:.1f}%)",
            "entity": str(ctx.coverage_pct),
        })

    # 6. Dependency manifest missing
    if ctx.project_requirements_data.get("status") == "not_found":
        signals.append({
            "type": "no_dependency_manifest",
            "severity": 8,
            "description": "No repository dependency manifest (requirements.txt, package.json, etc.) found",
            "entity": "",
        })

    # 7. Cross-module dependencies unknown
    signals.append({
        "type": "cross_module_deps_unknown",
        "severity": 9,
        "description": "Cross-module dependency graph not yet available",
        "entity": "",
    })

    # Sort by severity ascending (lower number = higher severity) and cap at 15
    signals.sort(key=lambda s: s["severity"])
    return signals[:15]


def _rule_based_questions(signals: List[Dict[str, Any]]) -> str:
    """
    Generates rule-based question list from extracted gap signals.
    Capped at 8 questions.
    """
    questions: List[str] = []
    seen_types = set()

    for s in signals:
        stype = s.get("type", "")
        entity = s.get("entity", "")
        desc = s.get("description", "")

        if stype == "complex_undocumented" and entity:
            questions.append(f"Walk me through `{entity}` — it is a large function with no docstring. What are its non-obvious behaviors or edge cases?")
        elif stype == "undocumented_function" and entity and "undocumented_function" not in seen_types:
            questions.append(f"What does `{entity}` do, and what was the design rationale behind its signature?")
            seen_types.add("undocumented_function")
        elif stype == "undocumented_method" and entity and "undocumented_method" not in seen_types:
            questions.append(f"What is the expected contract and state lifecycle for `{entity}`?")
            seen_types.add("undocumented_method")
        elif stype == "sole_owner_file" and entity:
            questions.append(f"You are the primary committer for these files ({entity}). What unwritten rules or operational gotchas should a maintainer know?")
        elif stype == "no_owners":
            questions.append("No active git commit owner was found for this module. Who originally authored it and who can be consulted for legacy context?")
        elif stype == "no_linked_docs" and "no_linked_docs" not in seen_types:
            questions.append("There are no linked documentation files for this module. Where is tribal knowledge or architectural intent recorded?")
            seen_types.add("no_linked_docs")
        elif stype == "no_readme" and "no_readme" not in seen_types:
            questions.append("There is no README for this module. What is the recommended entry path for exploring this codebase?")
            seen_types.add("no_readme")
        elif stype == "low_doc_coverage" and "low_doc_coverage" not in seen_types:
            questions.append("Documentation coverage across this module is under 50%. Which specific undocumented areas have the highest risk of regression?")
            seen_types.add("low_doc_coverage")
        elif stype == "no_dependency_manifest" and "no_dependency_manifest" not in seen_types:
            questions.append("No build/dependency manifest file was found. What external runtime services or environment variables are required to run this?")
            seen_types.add("no_dependency_manifest")
        elif stype == "cross_module_deps_unknown" and "cross_module_deps_unknown" not in seen_types:
            questions.append("What other modules or external APIs does this module interface with, and are there known coupling issues?")
            seen_types.add("cross_module_deps_unknown")

        if len(questions) >= 8:
            break

    if not questions:
        return "1. What is the overall architecture and main operational gotchas for this module?"

    return "\n".join(f"{idx + 1}. {q}" for idx, q in enumerate(questions))


def _generate_questions(ctx: ModuleContext, gap_signals: List[Dict[str, Any]]) -> Tuple[str, str]:
    """
    Synthesizes KT prep questions using LLM or rule-based fallback.

    Returns:
        Tuple of (questions_text, generation_method)
    """
    # If no real gaps exist (only cross_module_deps_unknown signal present on a pristine module)
    real_gaps = [s for s in gap_signals if s["type"] != "cross_module_deps_unknown"]
    if not real_gaps:
        return (
            "This module is well-documented. No critical knowledge gaps requiring dedicated KT session questions were identified.",
            "no_gaps_detected",
        )

    prov = settings.get_effective_llm_provider()

    # Stub mode check
    if prov == "stub":
        logger.info("LLM provider is 'stub'. Generating rule-based KT prep questions.")
        return _rule_based_questions(gap_signals), "rule_based_fallback"

    # LLM prompt synthesis
    gap_descriptions = "\n".join(f"- {s['description']}" for s in gap_signals)
    prompt_context = (
        f"Module Name: {ctx.module_name}\n"
        f"Module Path: {ctx.module_path}\n"
        f"Key Files: {', '.join(f['filename'] for f in ctx.key_files_list[:10])}\n"
        f"Code Owners: {', '.join(o['name'] + ' <' + o['email'] + '>' for o in ctx.owners_list[:5])}\n"
        f"Linked Docs: {', '.join(d['filename'] for d in ctx.existing_docs_list)}\n\n"
        f"Identified Knowledge & Documentation Gaps:\n"
        f"{gap_descriptions}\n\n"
        f"Code & Doc Excerpts (Context Only):\n"
        + "\n---\n".join(ctx.semantic_snippets[:4])
    )

    sys_prompt = (
        "You are a senior engineer writing KT session questions for a new hire who will have one "
        "limited conversation with the engineer leaving this project. Your job is to produce specific, "
        "high-value questions that target things that CANNOT be learned by reading code or docs.\n"
        "STRICT RULES:\n"
        "1. Only mention file names, function names, or people present in the context below. Never invent any.\n"
        "2. Do NOT ask about anything already answerable from the pack sections (ownership, entry points, "
        "existing docs, declared dependencies are all documented — do not re-ask).\n"
        "3. Focus exclusively on: rationale behind key decisions, known gotchas or footguns, "
        "operational risk, unwritten conventions, historical context, migration plans.\n"
        "4. Output a numbered list of 5-10 questions only. No preamble, no closing remarks.\n"
        "5. Each question must be specific to this project — not a generic template question."
    )

    user_prompt = f"{prompt_context}\n\nTask: Generate the list of targeted KT preparation questions."

    try:
        gen_text = generate_text(prompt=user_prompt, system_instruction=sys_prompt)
        if gen_text and len(gen_text.strip()) > 10:
            return gen_text.strip(), "llm"
        raise LLMClientError("LLM returned empty or too-short question list.")
    except LLMClientError as llm_err:
        logger.warning("LLM client generation for KT prep questions failed: %s. Falling back to rule-based.", llm_err)
        return _rule_based_questions(gap_signals), "rule_based_fallback"


def run_kt_prep_questions_task(
    task_id: str,
    module_id: str,
    organization_id: str,
) -> Dict[str, Any]:
    """
    Executes background KT Prep Questions generation pipeline.
    1. Assembles module context from Neo4j & ChromaDB.
    2. Extracts gap signals.
    3. Synthesizes KT questions via LLM or rule-based fallback.
    4. Performs grounding check.
    5. Persists result payload into Postgres task table.
    """
    t_start = time.perf_counter()
    llm_time_ms = 0.0

    org_id = organization_id.strip()
    mod_id = module_id.strip()

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

    # Extract gap signals & generate questions
    gap_signals = _build_gap_signals(ctx)

    t_l0 = time.perf_counter()
    questions_text, gen_method = _generate_questions(ctx, gap_signals)
    llm_time_ms += (time.perf_counter() - t_l0) * 1000

    # Grounding check on generated questions
    generation_quality = "ok"
    known_people = {o["name"].lower() for o in ctx.owners_list} | {o["email"].lower() for o in ctx.owners_list}
    known_files = (
        {f["filename"].lower() for f in ctx.key_files_list}
        | {d["filename"].lower() for d in ctx.existing_docs_list}
        | {mf.split("/")[-1].lower() for mf in ctx.project_requirements_data.get("manifest_files", [])}
        | {"requirements.txt", "package.json", "pyproject.toml", "pipfile", "environment.yml", "setup.py", "readme.md", "contributing.md"}
    )

    q_lower = questions_text.lower()
    mentioned_emails = set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', q_lower))
    for email in mentioned_emails:
        if email not in known_people and "autokt" not in email:
            logger.warning("Grounding warning: KT Prep Questions mentioned unknown email '%s'", email)
            generation_quality = "grounding_warning"

    if known_files:
        mentioned_files = set(re.findall(r'\b[a-zA-Z0-9_\-]+\.(?:py|js|ts|jsx|tsx|md|rst|txt|json|yaml|yml)\b', q_lower))
        for fname in mentioned_files:
            if fname not in known_files:
                logger.warning("Grounding warning: KT Prep Questions mentioned unknown file '%s'", fname)
                generation_quality = "grounding_warning"

    total_time_ms = (time.perf_counter() - t_start) * 1000

    result = {
        "module_id": ctx.module_id,
        "module_name": ctx.module_name,
        "module_path": ctx.module_path,
        "organization_id": org_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generation_quality": generation_quality,
        "generation_method": gen_method,
        "signal_count": len(gap_signals),
        "signals_used": [s["description"] for s in gap_signals],
        "questions": questions_text,
        "telemetry": {
            "graph_query_ms": round(ctx.graph_time_ms, 2),
            "vector_query_ms": round(ctx.vector_time_ms, 2),
            "llm_generation_ms": round(llm_time_ms, 2),
            "total_ms": round(total_time_ms, 2),
            "llm_provider": settings.get_effective_llm_provider(),
            "llm_model": settings.LLM_MODEL,
        },
    }

    update_task_completed(task_id, result)
    logger.info("KT prep questions task '%s' completed successfully in %.2fms.", task_id, total_time_ms)
    return result
