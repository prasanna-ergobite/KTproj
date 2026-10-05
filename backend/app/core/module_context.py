"""
Module Context Assembler for AutoKT.
Extracts and assembles graph topology from Neo4j and vector metadata from ChromaDB
into a unified ModuleContext dataclass used across pipelines.
"""

import time
import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple

from app.core.config import settings
from app.db.neo4j_client import neo4j_client, Neo4jClientError
from app.db.chroma_client import chroma_client
from app.core.embedder import embed_query

logger = logging.getLogger("autokt.module_context")


class ModuleNotFoundError(Exception):
    """Raised when a requested module ID is not found in Neo4j."""
    pass


def _get_tenant_key(organization_id: str, node_id: str) -> str:
    """Helper constructing tenant_key for Neo4j lookups."""
    if node_id.startswith(f"{organization_id}:"):
        return node_id
    return f"{organization_id}:{node_id}"


@dataclass
class ModuleContext:
    # Identity
    module_id: str
    module_name: str
    module_path: str
    organization_id: str

    # Raw Neo4j topology
    raw_files: List[Dict[str, Any]]
    raw_functions: List[Dict[str, Any]]
    raw_classes: List[Dict[str, Any]]

    # Assembled views
    key_files_list: List[Dict[str, Any]]
    entry_points_list: List[Dict[str, Any]]

    # Chroma function metadata: (file_id, fn_name, start_line) -> {is_documented, chunk_type, parent_class, token_count}
    chroma_func_meta: Dict[Tuple[str, str, int], Dict[str, Any]]

    # Ownership & Docs
    owners_list: List[Dict[str, Any]]
    existing_docs_list: List[Dict[str, Any]]
    has_readme: bool

    # Requirements
    project_requirements_data: Dict[str, Any]

    # Coverage metrics
    total_func_count: int
    documented_count: int
    undocumented_count: int
    coverage_pct: float
    doc_coverage_data: Dict[str, Any]

    # Semantic context snippets
    semantic_snippets: List[str]

    # Timing metrics (ms)
    graph_time_ms: float
    vector_time_ms: float


def assemble_module_context(module_id: str, organization_id: str) -> ModuleContext:
    """
    Assembles full module context from Neo4j graph and ChromaDB.

    Args:
        module_id: Module identifier.
        organization_id: Organization/Tenant ID.

    Returns:
        Populated ModuleContext dataclass instance.

    Raises:
        ModuleNotFoundError: If module is not present in Neo4j graph.
        Neo4jClientError: If Neo4j query fails fatally.
    """
    graph_time_ms = 0.0
    vector_time_ms = 0.0

    org_id = organization_id.strip()
    mod_id = module_id.strip()
    mod_id_stripped = mod_id[5:] if mod_id.startswith("repo:") else mod_id
    mod_id_with_repo = f"repo:{mod_id}" if not mod_id.startswith("repo:") else mod_id

    mod_tenant_key = _get_tenant_key(org_id, mod_id)
    mod_tenant_key_stripped = _get_tenant_key(org_id, mod_id_stripped)
    mod_tenant_key_with_repo = _get_tenant_key(org_id, mod_id_with_repo)

    # 1a. Topology query from Neo4j
    t_g0 = time.perf_counter()
    cypher_topology = (
        "MATCH (m:Module) "
        "WHERE (m.tenant_key = $mod_tenant_key OR m.tenant_key = $mod_tenant_key_stripped OR m.tenant_key = $mod_tenant_key_with_repo "
        "   OR m.id = $mod_id OR m.id = $mod_id_stripped OR m.id = $mod_id_with_repo OR elementId(m) = $mod_id) "
        "  AND (m.organization_id = $organization_id OR m.tenant_key STARTS WITH $organization_id + ':') "
        "OPTIONAL MATCH (f:File)-[:PART_OF]->(m) "
        "OPTIONAL MATCH (fn:Function)-[:PART_OF]->(f) "
        "OPTIONAL MATCH (cl:Class)-[:PART_OF]->(f) "
        "RETURN m.id AS module_id, m.name AS module_name, m.path AS module_path, m.tenant_key AS tenant_key, "
        "collect(distinct {id: f.id, path: f.path, language: f.language}) AS files, "
        "collect(distinct {name: fn.name, file_id: fn.file_id, start_line: fn.start_line}) AS functions, "
        "collect(distinct {name: cl.name, file_id: cl.file_id}) AS classes"
    )

    topo_records = neo4j_client.run_read_query(
        cypher_topology,
        parameters={
            "mod_tenant_key": mod_tenant_key,
            "mod_tenant_key_stripped": mod_tenant_key_stripped,
            "mod_tenant_key_with_repo": mod_tenant_key_with_repo,
            "mod_id": mod_id,
            "mod_id_stripped": mod_id_stripped,
            "mod_id_with_repo": mod_id_with_repo,
            "organization_id": org_id,
        },
        organization_id=org_id,
    )
    graph_time_ms += (time.perf_counter() - t_g0) * 1000

    # Fallback: If input ID is a File node ID instead of Module ID, resolve its parent Module
    if not topo_records or not topo_records[0].get("module_id"):
        t_fb0 = time.perf_counter()
        cypher_file_fallback = (
            "MATCH (f:File)-[:PART_OF]->(m:Module) "
            "WHERE (f.id = $mod_id OR f.id = $mod_id_stripped OR f.id = $mod_id_with_repo OR f.tenant_key = $mod_tenant_key OR elementId(f) = $mod_id) "
            "  AND (m.organization_id = $organization_id OR m.tenant_key STARTS WITH $organization_id + ':') "
            "OPTIONAL MATCH (f_all:File)-[:PART_OF]->(m) "
            "OPTIONAL MATCH (fn:Function)-[:PART_OF]->(f_all) "
            "OPTIONAL MATCH (cl:Class)-[:PART_OF]->(f_all) "
            "RETURN m.id AS module_id, m.name AS module_name, m.path AS module_path, m.tenant_key AS tenant_key, "
            "collect(distinct {id: f_all.id, path: f_all.path, language: f_all.language}) AS files, "
            "collect(distinct {name: fn.name, file_id: fn.file_id, start_line: fn.start_line}) AS functions, "
            "collect(distinct {name: cl.name, file_id: cl.file_id}) AS classes"
        )
        topo_records = neo4j_client.run_read_query(
            cypher_file_fallback,
            parameters={
                "mod_tenant_key": mod_tenant_key,
                "mod_tenant_key_stripped": mod_tenant_key_stripped,
                "mod_tenant_key_with_repo": mod_tenant_key_with_repo,
                "mod_id": mod_id,
                "mod_id_stripped": mod_id_stripped,
                "mod_id_with_repo": mod_id_with_repo,
                "organization_id": org_id,
            },
            organization_id=org_id,
        )
        graph_time_ms += (time.perf_counter() - t_fb0) * 1000

    if not topo_records or not topo_records[0].get("module_id"):
        raise ModuleNotFoundError(f"Module with ID '{mod_id}' not found in organization '{org_id}'.")

    mod_record = topo_records[0]
    module_name = mod_record.get("module_name", mod_id)
    module_path = mod_record.get("module_path", "")

    raw_files = [f for f in mod_record.get("files", []) if f and f.get("id")]
    raw_functions = [fn for fn in mod_record.get("functions", []) if fn and fn.get("name")]
    raw_classes = [cl for cl in mod_record.get("classes", []) if cl and cl.get("name")]

    # 1b. Chroma function metadata enrichment
    t_v0 = time.perf_counter()
    file_ids = [f["id"] for f in raw_files]
    chroma_func_meta: Dict[Tuple[str, str, int], Dict[str, Any]] = {}

    if file_ids and settings.EMBEDDING_PROVIDER:
        for f_id in file_ids:
            try:
                c_res = chroma_client.get_documents(
                    collection_name="code_chunks",
                    organization_id=org_id,
                    extra_filter={"file_id": {"$eq": f_id}},
                )
                metadatas = c_res.get("metadatas", [])
                for meta in metadatas:
                    if not meta:
                        continue
                    fn_name = meta.get("function_name", "")
                    start_l = int(meta.get("start_line", 0))
                    if fn_name:
                        key = (f_id, fn_name, start_l)
                        chroma_func_meta[key] = {
                            "is_documented": bool(meta.get("is_documented", False)),
                            "chunk_type": meta.get("chunk_type", "function"),
                            "parent_class": meta.get("parent_class", ""),
                            "token_count": int(meta.get("token_count", 0)),
                        }
            except Exception as c_exc:
                logger.warning("Error fetching Chroma metadata for file '%s': %s", f_id, c_exc)

    vector_time_ms += (time.perf_counter() - t_v0) * 1000

    # Key Files view
    key_files_list = []
    for f in raw_files:
        f_id = f["id"]
        rel_p = f.get("path", "")
        filename = rel_p.split("/")[-1] if "/" in rel_p else rel_p
        lang = f.get("language", "")
        f_fn_count = sum(1 for fn in raw_functions if fn.get("file_id") == f_id)
        f_cl_count = sum(1 for cl in raw_classes if cl.get("file_id") == f_id)
        key_files_list.append({
            "file_id": f_id,
            "filename": filename,
            "relative_path": rel_p,
            "function_count": f_fn_count,
            "class_count": f_cl_count,
            "language": lang,
        })

    # Entry points & coverage counting
    entry_points_list = []
    documented_count = 0
    total_func_count = 0

    for fn in raw_functions:
        fn_name = fn.get("name", "")
        f_id = fn.get("file_id", "")
        start_l = int(fn.get("start_line", 0))
        key = (f_id, fn_name, start_l)
        c_meta = chroma_func_meta.get(key, {})

        is_doc = c_meta.get("is_documented", False)
        chunk_type = c_meta.get("chunk_type", "function")
        parent_cls = c_meta.get("parent_class", "")

        total_func_count += 1
        if is_doc:
            documented_count += 1

        if chunk_type == "function" and not parent_cls:
            f_rel_path = next((f["path"] for f in raw_files if f["id"] == f_id), "")
            entry_points_list.append({
                "function_name": fn_name,
                "file_path": f_rel_path,
                "is_documented": is_doc,
                "chunk_type": chunk_type,
            })

    # 2. Ownership from Neo4j
    t_g1 = time.perf_counter()
    owners_list = []
    cypher_owners = (
        "MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(f:File)-[:PART_OF]->(m:Module) "
        "WHERE (m.tenant_key = $mod_tenant_key OR m.id = $mod_id OR elementId(m) = $mod_id) "
        "  AND (m.organization_id = $organization_id OR m.tenant_key STARTS WITH $organization_id + ':') "
        "RETURN p.name AS name, p.email AS email, "
        "sum(o.commit_count) AS total_commits, "
        "max(o.last_commit_at) AS last_active "
        "ORDER BY total_commits DESC LIMIT 10"
    )
    try:
        owners_records = neo4j_client.run_read_query(
            cypher_owners,
            parameters={"mod_tenant_key": mod_tenant_key, "mod_id": mod_id, "organization_id": org_id},
            organization_id=org_id,
        )
        owners_list = [
            {
                "name": rec.get("name", "Unknown"),
                "email": rec.get("email", ""),
                "total_commits": int(rec.get("total_commits", 0)),
                "last_active": str(rec.get("last_active", "")),
            }
            for rec in owners_records
        ]
    except Exception as o_exc:
        logger.warning("Failed Neo4j query for code owners: %s", o_exc)
    graph_time_ms += (time.perf_counter() - t_g1) * 1000

    # 3. Linked Documents from Neo4j + Lead Excerpt from Chroma
    t_g2 = time.perf_counter()
    cypher_docs = (
        "MATCH (d:Document)-[:DOCUMENTS]->(m:Module) "
        "WHERE (m.tenant_key = $mod_tenant_key OR m.id = $mod_id OR elementId(m) = $mod_id) "
        "  AND (m.organization_id = $organization_id OR m.tenant_key STARTS WITH $organization_id + ':') "
        "RETURN d.id AS doc_id, d.filename AS filename, d.relative_path AS relative_path, "
        "coalesce(d.version, 1) AS version"
    )
    doc_records = []
    try:
        doc_records = neo4j_client.run_read_query(
            cypher_docs,
            parameters={"mod_tenant_key": mod_tenant_key, "mod_id": mod_id, "organization_id": org_id},
            organization_id=org_id,
        )
    except Exception as d_g_exc:
        logger.warning("Failed Neo4j query for linked docs: %s", d_g_exc)
    graph_time_ms += (time.perf_counter() - t_g2) * 1000

    existing_docs_list = []
    has_readme = False

    t_v1 = time.perf_counter()
    for d_rec in doc_records:
        d_id = d_rec.get("doc_id", "")
        fname = d_rec.get("filename", "")
        rpath = d_rec.get("relative_path", "")
        ver = int(d_rec.get("version", 1))

        fname_upper = fname.upper()
        if "README" in fname_upper or "CONTRIBUTING" in fname_upper:
            has_readme = True

        lead_excerpt = ""
        if settings.EMBEDDING_PROVIDER and d_id:
            try:
                d_res = chroma_client.get_documents(
                    collection_name="docs_chunks",
                    organization_id=org_id,
                    extra_filter={"doc_id": {"$eq": d_id}},
                )
                d_docs = d_res.get("documents", [])
                if d_docs and d_docs[0]:
                    lead_excerpt = d_docs[0][:300].strip()
            except Exception as d_exc:
                logger.warning("Error fetching lead excerpt for doc '%s': %s", d_id, d_exc)

        existing_docs_list.append({
            "doc_id": d_id,
            "filename": fname,
            "relative_path": rpath,
            "version": ver,
            "lead_excerpt": lead_excerpt,
        })
    vector_time_ms += (time.perf_counter() - t_v1) * 1000

    # 3b. Project Requirements & Dependencies (Repository-Scoped)
    t_g3 = time.perf_counter()
    manifest_files_found: List[str] = []
    declared_deps: List[str] = []
    setup_inst_text = "Check project documentation for runtime environment details."

    # First, query Package nodes connected via USED_BY from Neo4j
    cypher_packages = (
        "MATCH (pkg:Package {organization_id: $organization_id})-[u:USED_BY]->(m:Module) "
        "WHERE (m.tenant_key = $mod_tenant_key OR m.id = $mod_id OR elementId(m) = $mod_id) "
        "RETURN pkg.name AS name, pkg.version AS version, pkg.ecosystem AS ecosystem, u.declared_in AS manifest"
    )
    try:
        pkg_records = neo4j_client.run_read_query(
            cypher_packages,
            parameters={"mod_tenant_key": mod_tenant_key, "mod_id": mod_id, "organization_id": org_id},
            organization_id=org_id,
        )
        for prec in pkg_records:
            p_name = prec.get("name", "")
            p_ver = prec.get("version", "")
            p_mf = prec.get("manifest", "")
            if p_mf and p_mf not in manifest_files_found:
                manifest_files_found.append(p_mf)
            if p_name:
                dep_str = f"{p_name} {p_ver}".strip()
                if dep_str not in declared_deps:
                    declared_deps.append(dep_str)
    except Exception as pkg_g_exc:
        logger.warning("Failed Neo4j query for Package nodes: %s", pkg_g_exc)

    cypher_manifests = (
        "MATCH (m:Module)-[:PART_OF]->(r:Repository) "
        "WHERE (m.tenant_key = $mod_tenant_key OR m.id = $mod_id OR elementId(m) = $mod_id) "
        "  AND (m.organization_id = $organization_id OR m.tenant_key STARTS WITH $organization_id + ':') "
        "OPTIONAL MATCH (d:Document)-[:DOCUMENTS]->(m2:Module)-[:PART_OF]->(r) "
        "WHERE d.filename IN ['requirements.txt', 'package.json', 'pyproject.toml', 'Pipfile', 'environment.yml', 'setup.py'] "
        "RETURN d.id AS doc_id, d.filename AS filename, d.relative_path AS relative_path"
    )
    manifest_records = []
    try:
        manifest_records = neo4j_client.run_read_query(
            cypher_manifests,
            parameters={"mod_tenant_key": mod_tenant_key, "mod_id": mod_id, "organization_id": org_id},
            organization_id=org_id,
        )
    except Exception as m_g_exc:
        logger.warning("Failed Neo4j query for manifests: %s", m_g_exc)
    graph_time_ms += (time.perf_counter() - t_g3) * 1000

    for m_rec in manifest_records:
        d_id = m_rec.get("doc_id")
        fn = m_rec.get("filename", "")
        rp = m_rec.get("relative_path", "")
        if fn and rp:
            if rp not in manifest_files_found:
                manifest_files_found.append(rp)
            if not declared_deps and settings.EMBEDDING_PROVIDER and d_id:
                try:
                    m_res = chroma_client.get_documents(
                        collection_name="docs_chunks",
                        organization_id=org_id,
                        extra_filter={"doc_id": {"$eq": d_id}},
                    )
                    m_docs = m_res.get("documents", [])
                    if m_docs:
                        full_content = "\n".join(m_docs)
                        if fn.lower() == "requirements.txt":
                            for line in full_content.splitlines():
                                line_s = line.strip()
                                if line_s and not line_s.startswith("#") and not line_s.startswith("-"):
                                    declared_deps.append(line_s)
                        elif fn.lower() == "package.json":
                            import json as py_json
                            try:
                                p_data = py_json.loads(full_content)
                                for dep_k in ("dependencies", "devDependencies"):
                                    if dep_k in p_data and isinstance(p_data[dep_k], dict):
                                        for pkg, v_str in p_data[dep_k].items():
                                            declared_deps.append(f"{pkg}@{v_str}")
                            except Exception:
                                pass
                except Exception as m_exc:
                    logger.warning("Error fetching manifest content for '%s': %s", fn, m_exc)

    if has_readme:
        setup_inst_text = "See linked README.md for repository setup and installation instructions."

    if manifest_files_found:
        project_requirements_data = {
            "source": "computed",
            "scope": "repository",
            "status": "found",
            "manifest_files": manifest_files_found,
            "declared_dependencies": declared_deps,
            "setup_instructions": setup_inst_text,
        }
    else:
        project_requirements_data = {
            "source": "computed",
            "scope": "repository",
            "status": "not_found",
            "reason": "No dependency manifest file (requirements.txt, package.json, pyproject.toml, Pipfile) found in repository.",
            "manifest_files": [],
            "declared_dependencies": [],
            "setup_instructions": setup_inst_text,
        }

    # 4. Documentation coverage calculation
    undocumented_count = max(0, total_func_count - documented_count)
    coverage_pct = (documented_count / total_func_count * 100.0) if total_func_count > 0 else 100.0

    doc_coverage_data = {
        "source": "computed",
        "total_functions": total_func_count,
        "documented_functions": documented_count,
        "undocumented_functions": undocumented_count,
        "coverage_pct": round(coverage_pct, 1),
        "has_readme": has_readme,
        "linked_doc_count": len(existing_docs_list),
    }

    # 6. Semantic Context Snippets via Chroma
    t_v2 = time.perf_counter()
    semantic_snippets: List[str] = []

    if settings.EMBEDDING_PROVIDER:
        try:
            query_str = f"what does module {module_name} do, its purpose and main responsibilities"
            q_emb = embed_query(query_str)
            code_res = chroma_client.query(
                collection_name="code_chunks",
                query_embeddings=[q_emb],
                organization_id=org_id,
                n_results=5,
                extra_filter={"module_id": {"$eq": mod_id}},
            )
            if code_res and code_res.get("documents") and code_res["documents"][0]:
                semantic_snippets.extend(code_res["documents"][0])

            doc_res = chroma_client.query(
                collection_name="docs_chunks",
                query_embeddings=[q_emb],
                organization_id=org_id,
                n_results=3,
                extra_filter={"module_id": {"$eq": mod_id}},
            )
            if doc_res and doc_res.get("documents") and doc_res["documents"][0]:
                semantic_snippets.extend(doc_res["documents"][0])
        except Exception as q_exc:
            logger.warning("Error fetching semantic context snippets for module '%s': %s", mod_id, q_exc)

    vector_time_ms += (time.perf_counter() - t_v2) * 1000

    return ModuleContext(
        module_id=mod_id,
        module_name=module_name,
        module_path=module_path,
        organization_id=org_id,
        raw_files=raw_files,
        raw_functions=raw_functions,
        raw_classes=raw_classes,
        key_files_list=key_files_list,
        entry_points_list=entry_points_list,
        chroma_func_meta=chroma_func_meta,
        owners_list=owners_list,
        existing_docs_list=existing_docs_list,
        has_readme=has_readme,
        project_requirements_data=project_requirements_data,
        total_func_count=total_func_count,
        documented_count=documented_count,
        undocumented_count=undocumented_count,
        coverage_pct=round(coverage_pct, 1),
        doc_coverage_data=doc_coverage_data,
        semantic_snippets=semantic_snippets,
        graph_time_ms=graph_time_ms,
        vector_time_ms=vector_time_ms,
    )
