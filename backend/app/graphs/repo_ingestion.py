"""
LangGraph Workflow / Engine: Repository Ingestion Graph Pipeline.
Handles git cloning/access, directory module structure parsing,
git authorship history, Neo4j multi-tenant MERGE operations,
in-repo document routing (heading-aware chunking for .md/.rst/.txt),
and ChromaDB vector indexing across code_chunks and docs_chunks.
"""

import os
import re
import shutil
import logging
import time
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Set, Tuple, Optional

import git
from sqlalchemy.orm import Session

import hashlib
from app.core.config import settings
from app.db.neo4j_client import neo4j_client
from app.db.chroma_client import chroma_client
from app.db.postgres_client import SessionLocal
from app.models.db_models import Task
from app.core.ast_chunker import chunk_file, ASTChunk, MAX_AST_CHUNK_CHARS, AST_SUPPORTED_LANGUAGES
from app.core.doc_chunker import chunk_document, DocChunk, _detect_code_fence
from app.core.embedder import embed_documents, count_tokens
from app.graphs.relationship_extractor import (
    extract_imports,
    extract_calls,
    extract_extends,
    extract_test_edges,
    parse_manifest_deps,
)

logger = logging.getLogger("autokt.ingestion")

# Maximum files ceiling per repository ingestion
MAX_FILES_CEILING = 10000

# File size ceiling: 1 MB (1,048,576 bytes)
MAX_FILE_SIZE_BYTES = 1048576

# Git commit history ceiling for ownership extraction
MAX_GIT_LOG_COMMITS = 500

# Binary and generated file extension exclusions
EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".db", ".sqlite", ".lock", ".bin", ".pdf"
}

# Directories to skip during directory walk
EXCLUDED_DIRECTORIES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "build", ".pytest_cache", ".idea", ".vscode", ".next", "out", "tmp_repos",
    "chroma_db", "chroma", ".chroma"
}

# Documentation file extensions routed to heading-aware chunker
DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".txt"}

# Source code file extensions routed to AST / fixed-window chunker
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".c", ".cpp",
    ".rs", ".rb", ".cs", ".php", ".sh", ".yaml", ".yml", ".json"
}

# Supported source file extensions (required for Module & File classification)
SOURCE_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS


# ---------------------------------------------------------------------------
# Classification & Security Helpers
# ---------------------------------------------------------------------------

def _is_doc_file(file_name: str, rel_path: str) -> bool:
    """
    Returns True if file is classified as documentation rather than source code.
    Identifies documentation by extension (.md, .rst, .txt) or common doc naming patterns.
    """
    ext = Path(file_name).suffix.lower()
    if ext in DOC_EXTENSIONS:
        return True
    upper_name = file_name.upper()
    if any(upper_name.startswith(prefix) for prefix in ("README", "CONTRIBUTING", "CHANGELOG", "LICENSE", "ARCHITECTURE", "SECURITY")):
        return True
    rel_parts = [p.lower() for p in Path(rel_path).parts]
    if any(p in ("docs", "doc", "documentation") for p in rel_parts):
        return True
    return False


def _build_chunk_tenant_key(org: str, repo_id: str, rel_path: str, c: ASTChunk) -> str:
    """Build standardized tenant_key for AST code chunk."""
    if c.function_name:
        return f"{org}:{repo_id}/{rel_path}::{c.function_name}"
    elif c.class_name:
        return f"{org}:{repo_id}/{rel_path}::{c.class_name}"
    else:
        return f"{org}:{repo_id}/{rel_path}:{c.chunk_id}"


def sanitize_url(text: str) -> str:
    """
    Strips credentials/tokens from URLs embedded anywhere inside arbitrary text or exception messages.
    """
    if not text:
        return ""
    return re.sub(r'(https?://|git://|ssh://|git@)[^/\s@]+@', r'\1', text)


def extract_repo_name(url_or_path: str) -> str:
    """
    Extracts a clean repository name from remote URLs, local paths, or plain names.
    """
    cleaned = url_or_path.strip()
    if cleaned.startswith("git@") and ":" in cleaned:
        path_part = cleaned.split(":", 1)[1]
        stem = Path(path_part).stem
        return stem.lower() if stem else "repo"
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.path and len(parsed.path.strip("/")) > 0:
        stem = Path(parsed.path.rstrip("/")).stem
        return stem.lower() if stem else "repo"
    stem = Path(cleaned).stem
    return stem.lower() if stem else "repo"


def validate_repo_url(url_or_path: str) -> bool:
    """
    Validates repository URL scheme or local directory path.
    """
    cleaned = url_or_path.strip()
    if not cleaned:
        raise ValueError("Repository URL or path cannot be empty.")
    if any(cleaned.startswith(prefix) for prefix in ("file://", "ftp://", "ftps://")):
        raise ValueError(f"Unsupported URI scheme in repository input: {cleaned}")
    if cleaned.startswith(("http://", "https://", "git://")):
        parsed = urllib.parse.urlparse(cleaned)
        if not parsed.netloc:
            raise ValueError(f"Invalid repository URL format: {cleaned}")
        return True
    if cleaned.startswith("git@") and ":" in cleaned:
        return True
    path = Path(cleaned)
    if ".." in path.parts:
        raise ValueError(f"Path traversal sequence ('..') detected in local path: {cleaned}")
    if not path.is_absolute():
        raise ValueError(f"Local repository path must be an absolute path: {cleaned}")
    if not path.exists():
        raise ValueError(f"Local repository path does not exist: {cleaned}")
    if not path.is_dir():
        raise ValueError(f"Local repository path is not a directory: {cleaned}")
    return True


def _safe_rmtree(path: Path) -> None:
    """
    Safely removes a directory tree, resetting read-only flags (useful for git objects on Windows).
    """
    if not path.exists():
        return
    def _on_rm_error(func, path_str, exc_info):
        try:
            os.chmod(path_str, 0o777)
            func(path_str)
        except Exception as exc:
            logger.warning("Failed to force-delete '%s': %s", path_str, exc)
    shutil.rmtree(path, onerror=_on_rm_error)


# ---------------------------------------------------------------------------
# Main Graph Task Execution Engine
# ---------------------------------------------------------------------------

def run_repo_ingestion_task(
    task_id: str,
    repo_url: str,
    organization_id: str,
    branch: str = "main",
) -> Dict[str, Any]:
    """
    Asynchronous entry point for repository cloning, parsing, graph construction, and vector indexing.
    """
    sanitized_url = sanitize_url(repo_url)
    repo_name = extract_repo_name(repo_url)
    repo_node_id = f"repo:{repo_name}"

    logger.info(
        "Starting repository ingestion task %s for repo '%s' (org '%s', branch '%s')...",
        task_id, repo_name, organization_id, branch
    )

    try:
        import uuid as uuid_mod
        uuid_mod.UUID(task_id)
        db = SessionLocal()
        try:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task:
                task.status = "running"
                task.updated_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as exc:
            logger.error("Failed to set task status to running: %s", exc)
        finally:
            db.close()
    except (ValueError, TypeError):
        pass

    clone_dir = Path("tmp_repos") / organization_id / repo_name
    clone_dir = clone_dir.resolve()

    summary: Dict[str, Any] = {
        "repository_id": repo_node_id,
        "repository_url": sanitized_url,
        "organization_id": organization_id,
        "is_git_repo": True,
        "modules_count": 0,
        "files_count": 0,
        "docs_count": 0,
        "authors_count": 0,
        "chunks_count": 0,
        "code_chunks_count": 0,
        "doc_chunks_count": 0,
        "ast_chunks_count": 0,
        "fw_chunks_count": 0,
        "git_log_truncated": False,
        "error": None,
    }

    repo: Optional[git.Repo] = None

    try:
        validate_repo_url(repo_url)
        _safe_rmtree(clone_dir)

        is_git_repo = True
        if repo_url.startswith(("http://", "https://", "git://", "git@")):
            clone_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Cloning remote repository into '%s'...", clone_dir)
            repo = git.Repo.clone_from(
                repo_url,
                str(clone_dir),
                branch=branch,
                depth=MAX_GIT_LOG_COMMITS,
            )
        else:
            local_source = Path(repo_url).resolve()
            try:
                # Search parent directories for .git repository (handles subdirectory paths)
                parent_repo = git.Repo(local_source, search_parent_directories=True)
                git_root = Path(parent_repo.working_tree_dir).resolve()
                clone_dir.mkdir(parents=True, exist_ok=True)
                logger.info("Cloning local git repository '%s' into '%s'...", git_root, clone_dir)
                repo = git.Repo.clone_from(str(git_root), str(clone_dir))
            except Exception:
                is_git_repo = False
                summary["is_git_repo"] = False
                logger.info("Copying local directory (non-git) into '%s'...", clone_dir)
                def _ignore_dir(path, names):
                    return [n for n in names if n in EXCLUDED_DIRECTORIES or n.startswith(".")]
                shutil.copytree(local_source, clone_dir, ignore=_ignore_dir)

        # 4. Neo4j Graph Construction (Repository -> Module -> File / Document)
        repo_tenant_key = f"{organization_id}:{repo_node_id}"

        cypher_repo = (
            "MERGE (r:Repository {tenant_key: $tenant_key}) "
            "SET r.id = $id, r.organization_id = $organization_id, r.name = $name, "
            "r.url = $url, r.default_branch = $branch, r.last_synced_at = $last_synced_at "
            "RETURN r"
        )
        neo4j_client.run_write_query(
            cypher_repo,
            {
                "tenant_key": repo_tenant_key,
                "id": repo_node_id,
                "name": repo_name,
                "url": sanitized_url,
                "branch": branch,
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
            },
            organization_id=organization_id,
        )

        # Dual Collection Cleanup: purge old repository chunks from both code_chunks and docs_chunks
        if settings.EMBEDDING_PROVIDER:
            if chroma_client.client is None:
                try:
                    chroma_client.connect()
                    chroma_client.ensure_collections()
                except Exception as conn_exc:
                    logger.warning("Could not auto-connect ChromaDB in repo_ingestion: %s", conn_exc)

            try:
                chroma_client.delete_by_metadata(
                    "code_chunks",
                    organization_id=organization_id,
                    extra_filter={"repository_id": {"$eq": repo_node_id}},
                )
            except Exception as del_code_exc:
                logger.warning("Error purging old code chunks for repo '%s': %s", repo_node_id, del_code_exc)

            try:
                chroma_client.delete_by_metadata(
                    "docs_chunks",
                    organization_id=organization_id,
                    extra_filter={"repository_id": {"$eq": repo_node_id}},
                )
            except Exception as del_doc_exc:
                logger.warning("Error purging old doc chunks for repo '%s': %s", repo_node_id, del_doc_exc)

        # Walk directory to classify modules, code files, and doc files
        modules_map: Dict[str, str] = {}
        code_files_list: List[Dict[str, Any]] = []
        doc_files_list: List[Dict[str, Any]] = []

        total_file_count = 0
        for root, dirs, files in os.walk(clone_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRECTORIES and not d.startswith(".")]
            rel_root = Path(root).relative_to(clone_dir)

            for file_name in files:
                ext = Path(file_name).suffix.lower()
                if ext in EXCLUDED_EXTENSIONS or ext not in SOURCE_EXTENSIONS:
                    continue

                file_path = clone_dir / rel_root / file_name
                if not file_path.exists() or file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                    continue

                total_file_count += 1
                if total_file_count > MAX_FILES_CEILING:
                    logger.warning("Reached maximum file ceiling (%d). Skipping remaining files.", MAX_FILES_CEILING)
                    break

                parts = rel_root.parts
                module_name = "_root" if len(parts) == 0 else parts[0]
                module_id = f"{repo_node_id}:module:{module_name}"
                modules_map[module_name] = module_id

                rel_file_path = str(rel_root / file_name).replace("\\", "/")
                language = ext.lstrip(".") or "txt"

                if _is_doc_file(file_name, rel_file_path):
                    doc_id = f"doc:{rel_file_path}"
                    doc_files_list.append({
                        "doc_id": doc_id,
                        "module_id": module_id,
                        "rel_path": rel_file_path,
                        "filename": file_name,
                        "language": language,
                        "abs_path": str(file_path),
                    })
                else:
                    file_id = f"{repo_node_id}:file:{rel_file_path}"
                    code_files_list.append({
                        "file_id": file_id,
                        "module_id": module_id,
                        "rel_path": rel_file_path,
                        "language": language,
                        "abs_path": str(file_path),
                    })

            if total_file_count > MAX_FILES_CEILING:
                break

        # MERGE Module nodes and connect PART_OF Repository
        for mod_name, mod_id in modules_map.items():
            mod_tenant_key = f"{organization_id}:{mod_id}"
            cypher_mod = (
                "MERGE (m:Module {tenant_key: $tenant_key}) "
                "SET m.id = $id, m.organization_id = $organization_id, "
                "m.repository_id = $repo_id, m.name = $name, m.path = $path "
                "WITH m "
                "MATCH (r:Repository {tenant_key: $repo_tenant_key}) "
                "MERGE (m)-[:PART_OF]->(r)"
            )
            neo4j_client.run_write_query(
                cypher_mod,
                {
                    "tenant_key": mod_tenant_key,
                    "id": mod_id,
                    "repo_id": repo_node_id,
                    "repo_tenant_key": repo_tenant_key,
                    "name": mod_name,
                    "path": mod_name,
                },
                organization_id=organization_id,
            )

        summary["modules_count"] = len(modules_map)

        # MERGE File nodes for code files and connect PART_OF Module
        for f_info in code_files_list:
            f_tenant_key = f"{organization_id}:{f_info['file_id']}"
            m_tenant_key = f"{organization_id}:{f_info['module_id']}"
            cypher_file = (
                "MERGE (f:File {tenant_key: $tenant_key}) "
                "SET f.id = $id, f.organization_id = $organization_id, "
                "f.module_id = $module_id, f.path = $path, f.language = $language, "
                "f.last_modified_at = $last_modified "
                "WITH f "
                "MATCH (m:Module {tenant_key: $mod_tenant_key}) "
                "MERGE (f)-[:PART_OF]->(m)"
            )
            neo4j_client.run_write_query(
                cypher_file,
                {
                    "tenant_key": f_tenant_key,
                    "id": f_info["file_id"],
                    "module_id": f_info["module_id"],
                    "mod_tenant_key": m_tenant_key,
                    "path": f_info["rel_path"],
                    "language": f_info["language"],
                    "last_modified": datetime.now(timezone.utc).isoformat(),
                },
                organization_id=organization_id,
            )

        summary["files_count"] = len(code_files_list)

        # MERGE Document nodes for doc files, check prior version & content_hash, and connect DOCUMENTS to Module
        ingested_at_ts = datetime.now(timezone.utc).isoformat()

        for d_info in doc_files_list:
            abs_p = Path(d_info["abs_path"])
            file_bytes = abs_p.read_bytes() if abs_p.exists() else b""
            content_hash = hashlib.sha256(file_bytes).hexdigest()[:32]
            d_info["content_hash"] = content_hash

            d_tenant_key = f"{organization_id}:{d_info['doc_id']}"
            m_tenant_key = f"{organization_id}:{d_info['module_id']}"

            # Check prior version and content hash in Neo4j
            cypher_prior = (
                "MATCH (d:Document {tenant_key: $tenant_key}) "
                "RETURN d.content_hash AS ch, coalesce(d.version, 0) AS ver"
            )
            prior_records = neo4j_client.run_read_query(
                cypher_prior,
                parameters={"tenant_key": d_tenant_key},
                organization_id=organization_id,
            )

            version = 1
            if prior_records:
                prior_ch = prior_records[0].get("ch", "")
                prior_ver = prior_records[0].get("ver", 0)
                if prior_ch == content_hash:
                    version = prior_ver if prior_ver > 0 else 1
                else:
                    version = prior_ver + 1
            d_info["version"] = version

            cypher_doc = (
                "MERGE (d:Document {tenant_key: $tenant_key}) "
                "SET d.id = $id, d.organization_id = $organization_id, "
                "d.repository_id = $repo_id, d.module_id = $module_id, "
                "d.filename = $filename, d.relative_path = $relative_path, "
                "d.content_hash = $content_hash, d.version = $version, "
                "d.ingested_at = $ingested_at "
                "WITH d "
                "MATCH (m:Module {tenant_key: $mod_tenant_key}) "
                "MERGE (d)-[:DOCUMENTS {confidence: 1.0, source: 'path_match'}]->(m)"
            )
            neo4j_client.run_write_query(
                cypher_doc,
                {
                    "tenant_key": d_tenant_key,
                    "id": d_info["doc_id"],
                    "repo_id": repo_node_id,
                    "module_id": d_info["module_id"],
                    "mod_tenant_key": m_tenant_key,
                    "filename": d_info["filename"],
                    "relative_path": d_info["rel_path"],
                    "content_hash": content_hash,
                    "version": version,
                    "ingested_at": ingested_at_ts,
                },
                organization_id=organization_id,
            )

        summary["docs_count"] = len(doc_files_list)

        # 4b. Function & Class Graph Node Extraction via AST Parsing (code files only)
        for f_info in code_files_list:
            clean_lang = f_info["language"].lower().lstrip(".")
            if clean_lang in AST_SUPPORTED_LANGUAGES or f".{clean_lang}" in AST_SUPPORTED_LANGUAGES:
                try:
                    with open(f_info["abs_path"], "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()

                    ast_chunks = chunk_file(
                        file_text=text,
                        file_path=f_info["rel_path"],
                        file_id=f_info["file_id"],
                        language=clean_lang,
                        max_chunk_chars=MAX_AST_CHUNK_CHARS,
                    )
                    f_info["ast_chunks"] = ast_chunks

                    f_tenant_key = f"{organization_id}:{f_info['file_id']}"
                    seen_classes: Set[str] = set()
                    seen_functions: Set[str] = set()

                    for chunk in ast_chunks:
                        if chunk.chunking_method == "ast":
                            if chunk.class_name and chunk.class_name not in seen_classes:
                                seen_classes.add(chunk.class_name)
                                cls_node_id = f"{repo_node_id}/{f_info['rel_path']}::{chunk.class_name}"
                                cls_tenant_key = f"{organization_id}:{cls_node_id}"

                                cypher_class = (
                                    "MERGE (c:Class {tenant_key: $tenant_key}) "
                                    "SET c.id = $id, c.organization_id = $organization_id, "
                                    "c.file_id = $file_id, c.name = $name, "
                                    "c.start_line = $start_line, c.end_line = $end_line "
                                    "WITH c "
                                    "MATCH (f:File {tenant_key: $file_tenant_key}) "
                                    "MERGE (c)-[:PART_OF]->(f)"
                                )
                                neo4j_client.run_write_query(
                                    cypher_class,
                                    {
                                        "tenant_key": cls_tenant_key,
                                        "id": cls_node_id,
                                        "file_id": f_info["file_id"],
                                        "file_tenant_key": f_tenant_key,
                                        "name": chunk.class_name,
                                        "start_line": chunk.start_line,
                                        "end_line": chunk.end_line,
                                    },
                                    organization_id=organization_id,
                                )

                            if chunk.function_name and chunk.function_name not in seen_functions:
                                seen_functions.add(chunk.function_name)
                                fn_node_id = f"{repo_node_id}/{f_info['rel_path']}::{chunk.function_name}"
                                fn_tenant_key = f"{organization_id}:{fn_node_id}"

                                cypher_func = (
                                    "MERGE (fn:Function {tenant_key: $tenant_key}) "
                                    "SET fn.id = $id, fn.organization_id = $organization_id, "
                                    "fn.file_id = $file_id, fn.name = $name, "
                                    "fn.start_line = $start_line, fn.end_line = $end_line "
                                    "WITH fn "
                                    "MATCH (f:File {tenant_key: $file_tenant_key}) "
                                    "MERGE (fn)-[:PART_OF]->(f)"
                                )
                                neo4j_client.run_write_query(
                                    cypher_func,
                                    {
                                        "tenant_key": fn_tenant_key,
                                        "id": fn_node_id,
                                        "file_id": f_info["file_id"],
                                        "file_tenant_key": f_tenant_key,
                                        "name": chunk.function_name,
                                        "start_line": chunk.start_line,
                                        "end_line": chunk.end_line,
                                    },
                                    organization_id=organization_id,
                                )

                                if chunk.parent_class:
                                    cls_node_id = f"{repo_node_id}/{f_info['rel_path']}::{chunk.parent_class}"
                                    cls_tenant_key = f"{organization_id}:{cls_node_id}"
                                    cypher_method_rel = (
                                        "MATCH (fn:Function {tenant_key: $fn_tenant_key}) "
                                        "OPTIONAL MATCH (c:Class {tenant_key: $cls_tenant_key}) "
                                        "FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END | "
                                        "  MERGE (fn)-[:METHOD_OF]->(c)"
                                        ")"
                                    )
                                    neo4j_client.run_write_query(
                                        cypher_method_rel,
                                        {
                                            "fn_tenant_key": fn_tenant_key,
                                            "cls_tenant_key": cls_tenant_key,
                                        },
                                        organization_id=organization_id,
                                    )
                    f_info["seen_functions"] = seen_functions
                    f_info["seen_classes"] = seen_classes
                except Exception as ast_exc:
                    logger.warning("Failed to process AST nodes for file '%s': %s", f_info["rel_path"], ast_exc)

        # 5. Git Authorship & Ownership Extraction (Person -> OWNS -> File / Document)
        authors_map: Dict[str, Dict[str, Any]] = {}
        code_ownership: Dict[Tuple[str, str], int] = {}
        doc_ownership: Dict[Tuple[str, str], int] = {}
        file_git_meta: Dict[str, dict] = {}

        # Quick lookup set for doc relative paths
        doc_rel_paths = {d["rel_path"]: d for d in doc_files_list}
        code_rel_paths = {f["rel_path"]: f for f in code_files_list}

        if is_git_repo and repo is not None:
            commit_count = 0
            try:
                commits = list(repo.iter_commits(max_count=MAX_GIT_LOG_COMMITS))
                if len(commits) >= MAX_GIT_LOG_COMMITS:
                    summary["git_log_truncated"] = True
                    logger.info("Git log hit ceiling (%d commits). Marked git_log_truncated=True.", MAX_GIT_LOG_COMMITS)

                for commit in commits:
                    commit_count += 1
                    author_name = commit.author.name or "Unknown"
                    author_email = (commit.author.email or "unknown@autokt.internal").strip().lower()

                    if author_email not in authors_map:
                        authors_map[author_email] = {
                            "name": author_name,
                            "email": author_email,
                            "person_id": f"person:{author_email}",
                        }

                    for file_path in commit.stats.files.keys():
                        norm_path = file_path.replace("\\", "/")
                        if norm_path in doc_rel_paths:
                            d_info = doc_rel_paths[norm_path]
                            key = (author_email, d_info["doc_id"])
                            doc_ownership[key] = doc_ownership.get(key, 0) + 1
                        elif norm_path in code_rel_paths:
                            f_info = code_rel_paths[norm_path]
                            key = (author_email, f_info["file_id"])
                            code_ownership[key] = code_ownership.get(key, 0) + 1

                        f_id = f"{repo_node_id}:file:{norm_path}"
                        if f_id not in file_git_meta or commit.committed_date > file_git_meta[f_id].get("committed_date_ts", 0):
                            file_git_meta[f_id] = {
                                "last_commit_sha": commit.hexsha,
                                "last_modified_date": datetime.fromtimestamp(commit.committed_date, tz=timezone.utc).isoformat(),
                                "last_author_email": author_email,
                                "last_author_name": author_name,
                                "committed_date_ts": commit.committed_date,
                                "commit_count": file_git_meta.get(f_id, {}).get("commit_count", 0) + 1,
                            }
                        else:
                            file_git_meta[f_id]["commit_count"] = file_git_meta[f_id].get("commit_count", 0) + 1

            except Exception as git_exc:
                logger.warning("Error reading git log history: %s", sanitize_url(str(git_exc)))

        # MERGE Person nodes
        for email, p_data in authors_map.items():
            p_tenant_key = f"{organization_id}:{p_data['person_id']}"
            cypher_person = (
                "MERGE (p:Person {tenant_key: $tenant_key}) "
                "SET p.id = $id, p.organization_id = $organization_id, "
                "p.name = $name, p.email = $email"
            )
            neo4j_client.run_write_query(
                cypher_person,
                {
                    "tenant_key": p_tenant_key,
                    "id": p_data["person_id"],
                    "name": p_data["name"],
                    "email": p_data["email"],
                },
                organization_id=organization_id,
            )

        summary["authors_count"] = len(authors_map)

        # MERGE OWNS relationships for Code Files
        for (email, file_id), c_count in code_ownership.items():
            p_id = f"person:{email}"
            p_tenant_key = f"{organization_id}:{p_id}"
            f_tenant_key = f"{organization_id}:{file_id}"
            cypher_owns_code = (
                "MATCH (p:Person {tenant_key: $p_tenant_key}), (f:File {tenant_key: $f_tenant_key}) "
                "MERGE (p)-[o:OWNS]->(f) "
                "SET o.commit_count = $commit_count, o.last_commit_at = $last_commit"
            )
            neo4j_client.run_write_query(
                cypher_owns_code,
                {
                    "p_tenant_key": p_tenant_key,
                    "f_tenant_key": f_tenant_key,
                    "commit_count": c_count,
                    "last_commit": datetime.now(timezone.utc).isoformat(),
                },
                organization_id=organization_id,
            )

        # MERGE OWNS relationships for Document Nodes
        for (email, doc_id), c_count in doc_ownership.items():
            p_id = f"person:{email}"
            p_tenant_key = f"{organization_id}:{p_id}"
            d_tenant_key = f"{organization_id}:{doc_id}"
            cypher_owns_doc = (
                "MATCH (p:Person {tenant_key: $p_tenant_key}), (d:Document {tenant_key: $d_tenant_key}) "
                "MERGE (p)-[o:OWNS]->(d) "
                "SET o.commit_count = $commit_count, o.last_commit_at = $last_commit"
            )
            neo4j_client.run_write_query(
                cypher_owns_doc,
                {
                    "p_tenant_key": p_tenant_key,
                    "d_tenant_key": d_tenant_key,
                    "commit_count": c_count,
                    "last_commit": datetime.now(timezone.utc).isoformat(),
                },
                organization_id=organization_id,
            )

        # 5b. Advanced Graph Relationship Extraction & Writes (Step 4c)
        all_code_paths = {f["rel_path"] for f in code_files_list}
        all_rel_paths_set = all_code_paths | {d["rel_path"] for d in doc_files_list}

        imports_count = 0
        calls_count = 0
        extends_count = 0
        tests_count = 0
        authored_count = 0
        packages_count = 0
        used_by_count = 0

        # 5b-1. Parse Dependency Manifests -> Package nodes & USED_BY relationships
        all_manifest_files = [f for f in code_files_list + doc_files_list if Path(f["rel_path"]).name.lower() in ("requirements.txt", "package.json", "pyproject.toml", "pipfile")]
        for m_file in all_manifest_files:
            try:
                abs_p = Path(m_file["abs_path"])
                if abs_p.exists():
                    m_text = abs_p.read_text(encoding="utf-8", errors="ignore")
                    pkg_records = parse_manifest_deps(m_file["rel_path"], m_text)
                    m_tenant_key = f"{organization_id}:{m_file['module_id']}"
                    for pkg in pkg_records:
                        pkg_id = f"package:{pkg.ecosystem}:{pkg.name}"
                        pkg_tenant_key = f"{organization_id}:{pkg_id}"
                        cypher_pkg = (
                            "MERGE (p:Package {tenant_key: $tenant_key}) "
                            "SET p.id = $id, p.organization_id = $organization_id, "
                            "p.name = $name, p.version = $version, "
                            "p.ecosystem = $ecosystem, p.repository_id = $repo_id "
                            "WITH p "
                            "MATCH (m:Module {tenant_key: $mod_tenant_key}) "
                            "MERGE (p)-[:USED_BY {declared_in: $declared_in}]->(m)"
                        )
                        neo4j_client.run_write_query(
                            cypher_pkg,
                            {
                                "tenant_key": pkg_tenant_key,
                                "id": pkg_id,
                                "name": pkg.name,
                                "version": pkg.version,
                                "ecosystem": pkg.ecosystem,
                                "repo_id": repo_node_id,
                                "mod_tenant_key": m_tenant_key,
                                "declared_in": m_file["rel_path"],
                            },
                            organization_id=organization_id,
                        )
                        packages_count += 1
                        used_by_count += 1
            except Exception as pkg_exc:
                logger.warning("Failed to parse package manifest '%s': %s", m_file["rel_path"], pkg_exc)

        # 5b-2. Extract IMPORTS, CALLS, and EXTENDS per code file
        for f_info in code_files_list:
            try:
                abs_p = Path(f_info["abs_path"])
                if not abs_p.exists():
                    continue
                file_text = abs_p.read_text(encoding="utf-8", errors="ignore")
                f_tenant_key = f"{organization_id}:{f_info['file_id']}"

                # IMPORTS (File -> File)
                imp_edges = extract_imports(file_text, f_info["rel_path"], f_info["language"], all_rel_paths_set)
                for imp in imp_edges:
                    if not imp.is_external and imp.to_rel_path:
                        target_file_id = f"{repo_node_id}:file:{imp.to_rel_path}"
                        target_tenant_key = f"{organization_id}:{target_file_id}"
                        cypher_imp = (
                            "MATCH (f1:File {tenant_key: $f1_tenant_key}), (f2:File {tenant_key: $f2_tenant_key}) "
                            "MERGE (f1)-[:IMPORTS {module: $module_name, is_external: false}]->(f2)"
                        )
                        neo4j_client.run_write_query(
                            cypher_imp,
                            {
                                "f1_tenant_key": f_tenant_key,
                                "f2_tenant_key": target_tenant_key,
                                "module_name": imp.module_name,
                            },
                            organization_id=organization_id,
                        )
                        imports_count += 1

                # CALLS (Function -> Function) — intra-file
                seen_fns = f_info.get("seen_functions", set())
                if len(seen_fns) >= 2:
                    call_edges = extract_calls(file_text, seen_fns, f_info["rel_path"])
                    for call in call_edges:
                        caller_id = f"{repo_node_id}/{f_info['rel_path']}::{call.caller_fn}"
                        callee_id = f"{repo_node_id}/{f_info['rel_path']}::{call.callee_fn}"
                        caller_tk = f"{organization_id}:{caller_id}"
                        callee_tk = f"{organization_id}:{callee_id}"
                        cypher_call = (
                            "MATCH (fn1:Function {tenant_key: $caller_tk}), (fn2:Function {tenant_key: $callee_tk}) "
                            "MERGE (fn1)-[:CALLS {call_count: 1}]->(fn2)"
                        )
                        neo4j_client.run_write_query(
                            cypher_call,
                            {
                                "caller_tk": caller_tk,
                                "callee_tk": callee_tk,
                            },
                            organization_id=organization_id,
                        )
                        calls_count += 1

                # EXTENDS (Class -> Class)
                seen_cls = f_info.get("seen_classes", set())
                if seen_cls:
                    extends_edges = extract_extends(file_text, f_info["rel_path"], f_info["language"], seen_cls)
                    for ext in extends_edges:
                        child_id = f"{repo_node_id}/{f_info['rel_path']}::{ext.child_class}"
                        child_tk = f"{organization_id}:{child_id}"
                        cypher_extends = (
                            "MATCH (c1:Class {tenant_key: $child_tk}) "
                            "OPTIONAL MATCH (c2:Class {organization_id: $organization_id, name: $parent_name}) "
                            "FOREACH (_ IN CASE WHEN c2 IS NOT NULL THEN [1] ELSE [] END | "
                            "  MERGE (c1)-[:EXTENDS]->(c2)"
                            ")"
                        )
                        neo4j_client.run_write_query(
                            cypher_extends,
                            {
                                "child_tk": child_tk,
                                "parent_name": ext.parent_class,
                            },
                            organization_id=organization_id,
                        )
                        extends_count += 1

            except Exception as f_rel_exc:
                logger.warning("Failed to extract relationships for file '%s': %s", f_info["rel_path"], f_rel_exc)

        # 5b-3. TESTS relationships (File -> File)
        test_edges = extract_test_edges(all_rel_paths_set)
        for t_edge in test_edges:
            test_file_id = f"{repo_node_id}:file:{t_edge.test_file_rel_path}"
            src_file_id = f"{repo_node_id}:file:{t_edge.source_file_rel_path}"
            t_tk = f"{organization_id}:{test_file_id}"
            s_tk = f"{organization_id}:{src_file_id}"
            cypher_test = (
                "MATCH (tf:File {tenant_key: $t_tk}), (sf:File {tenant_key: $s_tk}) "
                "MERGE (tf)-[:TESTS]->(sf)"
            )
            try:
                neo4j_client.run_write_query(
                    cypher_test,
                    {"t_tk": t_tk, "s_tk": s_tk},
                    organization_id=organization_id,
                )
                tests_count += 1
            except Exception as test_exc:
                logger.warning("Failed to MERGE TESTS edge: %s", test_exc)

        # 5b-4. AUTHORED relationships (Person -> Function / Class)
        default_author = list(authors_map.keys())[0] if authors_map else "system@autokt.internal"
        if not authors_map:
            # Ensure fallback system person node exists if no git commits were parsed
            sys_tk = f"{organization_id}:person:{default_author}"
            cypher_sys_p = (
                "MERGE (p:Person {tenant_key: $tenant_key}) "
                "SET p.id = $id, p.organization_id = $organization_id, "
                "p.name = 'System Maintainer', p.email = $email"
            )
            neo4j_client.run_write_query(
                cypher_sys_p,
                {"tenant_key": sys_tk, "id": f"person:{default_author}", "email": default_author},
                organization_id=organization_id,
            )

        for f_info in code_files_list:
            f_id = f_info["file_id"]
            git_meta = file_git_meta.get(f_id, {})
            last_author = git_meta.get("last_author_email") or default_author
            commit_cnt = git_meta.get("commit_count", 1)

            p_id = f"person:{last_author}"
            p_tk = f"{organization_id}:{p_id}"

            for fn_name in f_info.get("seen_functions", set()):
                fn_id = f"{repo_node_id}/{f_info['rel_path']}::{fn_name}"
                fn_tk = f"{organization_id}:{fn_id}"
                cypher_auth_fn = (
                    "MATCH (p:Person {tenant_key: $p_tk}), (fn:Function {tenant_key: $fn_tk}) "
                    "MERGE (p)-[:AUTHORED {commit_count: $commit_cnt, source: 'file_heuristic'}]->(fn)"
                )
                try:
                    neo4j_client.run_write_query(
                        cypher_auth_fn,
                        {"p_tk": p_tk, "fn_tk": fn_tk, "commit_cnt": commit_cnt},
                        organization_id=organization_id,
                    )
                    authored_count += 1
                except Exception as a_exc:
                    logger.warning("Failed to MERGE AUTHORED edge for function '%s': %s", fn_name, a_exc)

            for cls_name in f_info.get("seen_classes", set()):
                cls_id = f"{repo_node_id}/{f_info['rel_path']}::{cls_name}"
                cls_tk = f"{organization_id}:{cls_id}"
                cypher_auth_cls = (
                    "MATCH (p:Person {tenant_key: $p_tk}), (c:Class {tenant_key: $cls_tk}) "
                    "MERGE (p)-[:AUTHORED {commit_count: $commit_cnt, source: 'file_heuristic'}]->(c)"
                )
                try:
                    neo4j_client.run_write_query(
                        cypher_auth_cls,
                        {"p_tk": p_tk, "cls_tk": cls_tk, "commit_cnt": commit_cnt},
                        organization_id=organization_id,
                    )
                    authored_count += 1
                except Exception as a_exc:
                    logger.warning("Failed to MERGE AUTHORED edge for class '%s': %s", cls_name, a_exc)

        # 5b-5. DOCUMENTS extended (Document -> Function / Class via co-location)
        for d_info in doc_files_list:
            doc_tk = f"{organization_id}:{d_info['doc_id']}"
            doc_dir = str(Path(d_info["rel_path"]).parent).replace("\\", "/")
            doc_stem = Path(d_info["rel_path"]).stem.lower()

            for f_info in code_files_list:
                f_dir = str(Path(f_info["rel_path"]).parent).replace("\\", "/")
                f_stem = Path(f_info["rel_path"]).stem.lower()

                if doc_dir == f_dir and doc_stem == f_stem:
                    for fn_name in f_info.get("seen_functions", set()):
                        fn_id = f"{repo_node_id}/{f_info['rel_path']}::{fn_name}"
                        fn_tk = f"{organization_id}:{fn_id}"
                        cypher_doc_fn = (
                            "MATCH (d:Document {tenant_key: $doc_tk}), (fn:Function {tenant_key: $fn_tk}) "
                            "MERGE (d)-[:DOCUMENTS {confidence: 0.8, source: 'co_location'}]->(fn)"
                        )
                        try:
                            neo4j_client.run_write_query(
                                cypher_doc_fn,
                                {"doc_tk": doc_tk, "fn_tk": fn_tk},
                                organization_id=organization_id,
                            )
                        except Exception:
                            pass

                    for cls_name in f_info.get("seen_classes", set()):
                        cls_id = f"{repo_node_id}/{f_info['rel_path']}::{cls_name}"
                        cls_tk = f"{organization_id}:{cls_id}"
                        cypher_doc_cls = (
                            "MATCH (d:Document {tenant_key: $doc_tk}), (c:Class {tenant_key: $cls_tk}) "
                            "MERGE (d)-[:DOCUMENTS {confidence: 0.8, source: 'co_location'}]->(c)"
                        )
                        try:
                            neo4j_client.run_write_query(
                                cypher_doc_cls,
                                {"doc_tk": doc_tk, "cls_tk": cls_tk},
                                organization_id=organization_id,
                            )
                        except Exception:
                            pass

        summary["imports_count"] = imports_count
        summary["calls_count"] = calls_count
        summary["extends_count"] = extends_count
        summary["tests_count"] = tests_count
        summary["authored_count"] = authored_count
        summary["packages_count"] = packages_count
        summary["used_by_count"] = used_by_count

        # 6. ChromaDB Vector Store Indexing (code_chunks & docs_chunks)
        if not settings.EMBEDDING_PROVIDER:
            logger.info("EMBEDDING_PROVIDER is unconfigured. Skipping Step 6 (Vector Store indexing).")
            final_status = "completed_without_embeddings"
        else:
            logger.info("Indexing vector chunks into ChromaDB via '%s' embeddings...", settings.EMBEDDING_PROVIDER)
            code_chunks_added = 0
            doc_chunks_added = 0
            ast_added = 0
            fw_added = 0

            # 6a. Index Code Chunks into code_chunks collection
            for f_info in code_files_list:
                try:
                    chunks: List[ASTChunk] = f_info.get("ast_chunks")
                    if chunks is None:
                        with open(f_info["abs_path"], "r", encoding="utf-8", errors="ignore") as f:
                            text = f.read()
                        if not text.strip():
                            continue
                        chunks = chunk_file(
                            file_text=text,
                            file_path=f_info["rel_path"],
                            file_id=f_info["file_id"],
                            language=f_info["language"],
                            max_chunk_chars=MAX_AST_CHUNK_CHARS,
                        )

                    if not chunks:
                        continue

                    chunk_texts = [c.text for c in chunks]
                    token_counts = [0] * len(chunk_texts) if settings.EMBEDDING_PROVIDER == "stub" else count_tokens(chunk_texts)
                    embeddings = embed_documents(chunk_texts)

                    git_meta = file_git_meta.get(f_info["file_id"], {})

                    extra_metadata = [
                        {
                            "chunking_method": c.chunking_method,
                            "start_line": c.start_line,
                            "end_line": c.end_line,
                            "function_name": c.function_name,
                            "class_name": c.class_name,
                            "parent_class": c.parent_class,
                            "file_path": f_info["rel_path"],
                            "file_id": f_info["file_id"],
                            "repository_id": repo_node_id,
                            "chunk_type": c.chunk_type,
                            "language": c.language,
                            "tenant_key": _build_chunk_tenant_key(organization_id, repo_node_id, f_info["rel_path"], c),
                            "content_hash": hashlib.sha256(c.text.encode("utf-8")).hexdigest(),
                            "commit_sha": git_meta.get("last_commit_sha", ""),
                            "last_modified_date": git_meta.get("last_modified_date", ""),
                            "ingested_at": datetime.now(timezone.utc).isoformat(),
                            "last_author_email": git_meta.get("last_author_email", ""),
                            "last_author_name": git_meta.get("last_author_name", ""),
                            "commit_count": git_meta.get("commit_count", 0),
                            "is_documented": c.is_documented,
                            "token_count": token_counts[i],
                            "visibility": "internal",
                        }
                        for i, c in enumerate(chunks)
                    ]

                    chroma_client.add_documents(
                        collection_name="code_chunks",
                        documents=chunk_texts,
                        embeddings=embeddings,
                        ids=[c.chunk_id for c in chunks],
                        organization_id=organization_id,
                        module_id=f_info["module_id"],
                        source_type="code",
                        extra_metadata=extra_metadata,
                    )

                    for c in chunks:
                        code_chunks_added += 1
                        if c.chunking_method == "ast":
                            ast_added += 1
                        else:
                            fw_added += 1

                except Exception as chunk_exc:
                    logger.warning("Failed to index code chunks for file '%s': %s", f_info["rel_path"], chunk_exc)

            # 6b. Index In-Repo Doc Chunks into docs_chunks collection
            for d_info in doc_files_list:
                try:
                    with open(d_info["abs_path"], "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()

                    if not text.strip():
                        continue

                    doc_chunks: List[DocChunk] = chunk_document(
                        text=text,
                        doc_id=d_info["doc_id"],
                        filename=d_info["rel_path"],
                        source_type="doc",
                        chunking_method="heading_aware",
                    )

                    if not doc_chunks:
                        continue

                    chunk_texts = [c.text for c in doc_chunks]
                    token_counts = [0] * len(chunk_texts) if settings.EMBEDDING_PROVIDER == "stub" else count_tokens(chunk_texts)
                    embeddings = embed_documents(chunk_texts)

                    extra_doc_metadata = [
                        {
                            "organization_id": organization_id,
                            "repository_id": repo_node_id,
                            "module_id": d_info["module_id"],
                            "doc_id": d_info["doc_id"],
                            "relative_path": d_info["rel_path"],
                            "heading_path": " > ".join(c.heading_path),
                            "section_level": c.section_level,
                            "parent_section_id": c.parent_section_id or "",
                            "chunk_index": c.chunk_index,
                            "chunking_method": c.chunking_method,
                            "chunk_type": c.chunk_type,
                            "chunk_total": c.chunk_total,
                            "file_format": d_info["language"],
                            "content_hash": hashlib.sha256(c.text.encode("utf-8")).hexdigest(),
                            "last_modified_date": ingested_at_ts,
                            "ingested_at": ingested_at_ts,
                            "version": d_info["version"],
                            "uploaded_by": "",
                            "source_confidence": "path_match",
                            "token_count": token_counts[i],
                            "has_code_fence": c.has_code_fence,
                            "is_stub_or_empty": c.is_stub_or_empty,
                            "visibility": "internal",
                            "source_type": "doc",
                        }
                        for i, c in enumerate(doc_chunks)
                    ]

                    chroma_client.add_documents(
                        collection_name="docs_chunks",
                        documents=chunk_texts,
                        embeddings=embeddings,
                        ids=[c.chunk_id for c in doc_chunks],
                        organization_id=organization_id,
                        module_id=d_info["module_id"],
                        source_type="doc",
                        extra_metadata=extra_doc_metadata,
                    )

                    doc_chunks_added += len(doc_chunks)

                except Exception as doc_chunk_exc:
                    logger.warning("Failed to index doc chunks for in-repo doc '%s': %s", d_info["rel_path"], doc_chunk_exc)

            summary["code_chunks_count"] = code_chunks_added
            summary["doc_chunks_count"] = doc_chunks_added
            summary["chunks_count"] = code_chunks_added + doc_chunks_added
            summary["ast_chunks_count"] = ast_added
            summary["fw_chunks_count"] = fw_added

            if summary["chunks_count"] == 0:
                final_status = "completed_without_embeddings"
            else:
                final_status = "completed"

        _update_task_result(task_id, final_status, summary)
        logger.info("Repo ingestion task %s completed with status '%s'.", task_id, final_status)
        return summary

    except Exception as exc:
        sanitized_msg = sanitize_url(str(exc))
        logger.error("Repo ingestion task %s failed: %s", task_id, sanitized_msg)
        summary["error"] = sanitized_msg
        _update_task_result(task_id, "failed", summary)
        return summary

    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception as close_exc:
                logger.warning("Error closing git repository handle: %s", close_exc)

        if clone_dir and clone_dir.exists():
            logger.info("Cleaning up temporary clone directory '%s'...", clone_dir)
            _safe_rmtree(clone_dir)


def _update_task_result(task_id: str, status: str, result: Dict[str, Any]) -> None:
    """Helper to update Task status and result in PostgreSQL."""
    try:
        import uuid as uuid_mod
        uuid_mod.UUID(task_id)
    except (ValueError, TypeError):
        return

    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status = status
            task.result = result
            task.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        logger.error("Failed to update task result for task %s: %s", task_id, exc)
    finally:
        db.close()
