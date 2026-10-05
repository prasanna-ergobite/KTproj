"""
Onboarding Pack Markdown Renderer.
Pure function transforming structured onboarding pack result dictionaries into
clean, human-readable Markdown documents.
"""

from typing import Dict, Any


def render_markdown(pack: Dict[str, Any]) -> str:
    """
    Renders an OnboardingPackResult dictionary into a structured Markdown document.

    Args:
        pack: The full dictionary output of an onboarding pack generation task.

    Returns:
        Formatted Markdown string.
    """
    module_name = pack.get("module_name", "Unknown Module")
    module_path = pack.get("module_path", "")
    sections = pack.get("sections", {})

    md_lines = [
        f"# Developer Onboarding Pack: `{module_name}`",
        "",
        f"**Module Path:** `{module_path}`  ",
        f"**Generated At:** `{pack.get('generated_at', '')}`  ",
        f"**Generation Quality:** `{pack.get('generation_quality', 'ok')}`",
        "",
        "---",
        "",
    ]

    # 1. Module Purpose (LLM Generated)
    purpose_sec = sections.get("module_purpose", {})
    md_lines.extend([
        "## 1. Module Purpose & Overview",
        "",
        purpose_sec.get("content", "*No purpose summary available.*"),
        "",
    ])

    # 2. Key Files (Graph Sourced)
    key_files_sec = sections.get("key_files", {})
    files = key_files_sec.get("files", [])
    md_lines.extend([
        "## 2. Key Files",
        "",
    ])
    if files:
        md_lines.append("| Filename | Relative Path | Language | Functions | Classes |")
        md_lines.append("| --- | --- | --- | --- | --- |")
        for f in files:
            fname = f.get("filename", "")
            rpath = f.get("relative_path", "")
            lang = f.get("language", "")
            fn_cnt = f.get("function_count", 0)
            cl_cnt = f.get("class_count", 0)
            md_lines.append(f"| `{fname}` | `{rpath}` | `{lang}` | {fn_cnt} | {cl_cnt} |")
    else:
        md_lines.append("*No source files found in this module.*")
    md_lines.append("")

    # 3. Entry Points & Top-Level Functions (Graph + Vector Sourced)
    entry_sec = sections.get("entry_points", {})
    funcs = entry_sec.get("functions", [])
    md_lines.extend([
        "## 3. Entry Points & Top-Level Functions",
        "",
    ])
    if funcs:
        md_lines.append("| Function Name | File Path | Documented? |")
        md_lines.append("| --- | --- | --- |")
        for fn in funcs:
            fn_name = fn.get("function_name", "")
            fpath = fn.get("file_path", "")
            doc_status = "✅ Yes" if fn.get("is_documented") else "⚠️ No"
            md_lines.append(f"| `{fn_name}` | `{fpath}` | {doc_status} |")
    else:
        md_lines.append("*No top-level entry functions identified.*")
    md_lines.append("")

    # 4. Who to Talk To (Graph Sourced)
    owners_sec = sections.get("who_to_talk_to", {})
    owners = owners_sec.get("owners", [])
    caveat = owners_sec.get("caveat", "")
    md_lines.extend([
        "## 4. Who To Talk To (Code Owners)",
        "",
    ])
    if caveat:
        md_lines.extend([f"> ⚠️ **Note:** {caveat}", ""])

    if owners:
        md_lines.append("| Name | Email | Total Commits | Last Active |")
        md_lines.append("| --- | --- | --- | --- |")
        for o in owners:
            name = o.get("name", "")
            email = o.get("email", "")
            commits = o.get("total_commits", 0)
            last_act = o.get("last_active", "")
            md_lines.append(f"| {name} | `{email}` | {commits} | `{last_act}` |")
    else:
        md_lines.append("*No git commit authorship records found for files in this module.*")
    md_lines.append("")

    # 5. Existing Documentation (Graph + Vector Sourced)
    docs_sec = sections.get("existing_docs", {})
    docs = docs_sec.get("documents", [])
    md_lines.extend([
        "## 5. Existing Documentation",
        "",
    ])
    if docs:
        for d in docs:
            fname = d.get("filename", "")
            rpath = d.get("relative_path", "")
            ver = d.get("version", 1)
            excerpt = d.get("lead_excerpt", "")
            md_lines.extend([
                f"### `{fname}` (`{rpath}`, v{ver})",
                "",
                f"> {excerpt}" if excerpt else "> *(No text content excerpt)*",
                "",
            ])
    else:
        md_lines.append("*No documentation files currently linked to this module.*")
    md_lines.append("")

    # 6. Related Modules & Dependencies (Short-Circuited)
    deps_sec = sections.get("dependencies", {})
    dep_status = deps_sec.get("status", "available")
    dep_reason = deps_sec.get("reason", "")
    md_lines.extend([
        "## 6. Module Dependencies",
        "",
    ])
    if dep_status == "not_yet_available":
        md_lines.append(f"> ℹ️ *{dep_reason}*")
    else:
        related = deps_sec.get("related_modules", [])
        if related:
            md_lines.append("| Module | Path | Import Strength |")
            md_lines.append("| --- | --- | --- |")
            for r in related:
                md_lines.append(f"| `{r.get('module_name')}` | `{r.get('path')}` | {r.get('import_strength', 0)} |")
        else:
            md_lines.append("*No cross-module dependencies detected.*")
    md_lines.append("")

    # 7. Project Requirements & Setup (Computed - Repo Scoped)
    req_sec = sections.get("project_requirements", {})
    md_lines.extend([
        "## 7. Project Requirements & Setup",
        "",
    ])
    if req_sec.get("status") == "found":
        manifests = ", ".join(f"`{m}`" for m in req_sec.get("manifest_files", []))
        md_lines.extend([
            f"**Manifest Files (Repo Scope):** {manifests}  ",
            f"**Setup Instructions:** {req_sec.get('setup_instructions', '')}",
            "",
        ])
        deps = req_sec.get("declared_dependencies", [])
        if deps:
            md_lines.append("**Declared Dependencies:**")
            for dep in deps:
                md_lines.append(f"- `{dep}`")
            md_lines.append("")
    else:
        md_lines.extend([
            f"> ℹ️ *{req_sec.get('reason', 'No manifest files found.')}*",
            f"**Setup Instructions:** {req_sec.get('setup_instructions', '')}",
            "",
        ])

    # 8. Documentation Coverage (Computed Metric)
    cov_sec = sections.get("doc_coverage", {})
    md_lines.extend([
        "## 8. Documentation Coverage & Health",
        "",
        f"- **Total AST Functions/Methods:** {cov_sec.get('total_functions', 0)}",
        f"- **Documented Functions:** {cov_sec.get('documented_functions', 0)}",
        f"- **Undocumented Functions:** {cov_sec.get('undocumented_functions', 0)}",
        f"- **Estimated Function Coverage:** {cov_sec.get('coverage_pct', 0.0):.1f}%",
        f"- **Module Has README:** {'✅ Yes' if cov_sec.get('has_readme') else '❌ No'}",
        f"- **Linked Documents Count:** {cov_sec.get('linked_doc_count', 0)}",
        "",
    ])

    # 9. Suggested First Tasks (Rule-Based Computed)
    tasks_sec = sections.get("suggested_first_tasks", {})
    stasks = tasks_sec.get("tasks", [])
    md_lines.extend([
        "## 9. Suggested First Onboarding Tasks",
        "",
    ])
    if stasks:
        for t in stasks:
            prio = t.get("priority", "medium").upper()
            desc = t.get("description", "")
            rat = t.get("rationale", "")
            md_lines.append(f"- **[{prio}]** {desc}  \n  *Rationale:* {rat}")
    else:
        md_lines.append("*No specific initial onboarding tasks generated.*")
    md_lines.append("")

    # 10. Getting Started Narrative (LLM Generated)
    narrative_sec = sections.get("onboarding_narrative", {})
    md_lines.extend([
        "## 10. Getting Started Narrative",
        "",
        narrative_sec.get("content", "*No narrative available.*"),
        "",
    ])

    # Known Limitations & Telemetry Footnote
    limitations = pack.get("known_limitations", [])
    if limitations:
        md_lines.extend([
            "---",
            "### Known System Limitations",
            "",
        ])
        for lim in limitations:
            md_lines.append(f"- {lim}")
        md_lines.append("")

    telemetry = pack.get("telemetry", {})
    if telemetry:
        md_lines.extend([
            "---",
            f"*Pack generated in {telemetry.get('total_ms', 0.0):.2f}ms "
            f"(Graph: {telemetry.get('graph_query_ms', 0.0):.2f}ms, "
            f"Vector: {telemetry.get('vector_query_ms', 0.0):.2f}ms, "
            f"LLM: {telemetry.get('llm_generation_ms', 0.0):.2f}ms using {telemetry.get('llm_provider', 'stub')})*",
            "",
        ])

    return "\n".join(md_lines)
