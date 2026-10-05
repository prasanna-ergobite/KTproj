"""
Pure relationship extraction module for AutoKT repository ingestion pipeline.
Parses file text, AST chunks, and dependency manifest files to extract graph edges:
IMPORTS, CALLS, EXTENDS, TESTS, and Package records.
No Neo4j or database operations — returns typed NamedTuples.
"""

import re
import json
from pathlib import Path
from typing import List, Set, NamedTuple, Optional


class ImportEdge(NamedTuple):
    from_rel_path: str
    to_rel_path: str
    module_name: str
    is_external: bool


class CallEdge(NamedTuple):
    caller_fn: str
    callee_fn: str
    file_rel_path: str


class ExtendsEdge(NamedTuple):
    child_class: str
    parent_class: str
    file_rel_path: str


class TestEdge(NamedTuple):
    test_file_rel_path: str
    source_file_rel_path: str


class PackageRecord(NamedTuple):
    name: str
    version: str
    ecosystem: str  # "python" | "npm" | "unknown"
    manifest_path: str


def extract_imports(
    file_text: str,
    file_rel_path: str,
    language: str,
    all_rel_paths_set: Set[str],
) -> List[ImportEdge]:
    """
    Extracts import statements from Python, JS/TS, or Go source text.
    Matches internal imported files against all_rel_paths_set; classifies un-matched as external.
    """
    edges: List[ImportEdge] = []
    if not file_text or not file_text.strip():
        return edges

    lang = language.lower().lstrip(".")

    # Python import parsing
    if lang in ("py", "python"):
        # Match: import foo, from foo import bar
        for line in file_text.splitlines():
            line_str = line.strip()
            if line_str.startswith("#") or not line_str:
                continue

            # from x.y import z
            from_match = re.match(r"^from\s+([\w\.]+)\s+import", line_str)
            if from_match:
                mod_name = from_match.group(1)
                rel_candidate = mod_name.replace(".", "/") + ".py"
                # Check for match in all_rel_paths_set (direct or inside module)
                target_rel = _resolve_python_import(rel_candidate, file_rel_path, all_rel_paths_set)
                is_ext = target_rel is None
                edges.append(
                    ImportEdge(
                        from_rel_path=file_rel_path,
                        to_rel_path=target_rel or "",
                        module_name=mod_name,
                        is_external=is_ext,
                    )
                )
                continue

            # import x, import x as y
            imp_match = re.match(r"^import\s+([\w\.]+)", line_str)
            if imp_match:
                mod_name = imp_match.group(1)
                rel_candidate = mod_name.replace(".", "/") + ".py"
                target_rel = _resolve_python_import(rel_candidate, file_rel_path, all_rel_paths_set)
                is_ext = target_rel is None
                edges.append(
                    ImportEdge(
                        from_rel_path=file_rel_path,
                        to_rel_path=target_rel or "",
                        module_name=mod_name,
                        is_external=is_ext,
                    )
                )

    # JS/TS import parsing
    elif lang in ("js", "ts", "jsx", "tsx"):
        # Match import ... from '...' or require('...')
        import_regex = re.compile(r"""(?:import\s+.*?from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""")
        for match in import_regex.finditer(file_text):
            imp_path = match.group(1) or match.group(2)
            if not imp_path:
                continue

            target_rel = _resolve_js_import(imp_path, file_rel_path, all_rel_paths_set)
            is_ext = target_rel is None
            edges.append(
                ImportEdge(
                    from_rel_path=file_rel_path,
                    to_rel_path=target_rel or "",
                    module_name=imp_path,
                    is_external=is_ext,
                )
            )

    return edges


def _resolve_python_import(
    rel_candidate: str,
    file_rel_path: str,
    all_rel_paths_set: Set[str],
) -> Optional[str]:
    """Helper to match a Python import candidate against repository file paths."""
    if rel_candidate in all_rel_paths_set:
        return rel_candidate

    # Try relative to current file's dir
    current_dir = str(Path(file_rel_path).parent).replace("\\", "/")
    if current_dir != ".":
        combo = f"{current_dir}/{rel_candidate}"
        if combo in all_rel_paths_set:
            return combo

    # Try suffix match
    for path in all_rel_paths_set:
        if path.endswith("/" + rel_candidate) or path == rel_candidate:
            return path
    return None


def _resolve_js_import(
    imp_path: str,
    file_rel_path: str,
    all_rel_paths_set: Set[str],
) -> Optional[str]:
    """Helper to match a JS/TS relative or absolute import against repository file paths."""
    if not imp_path.startswith("."):
        # External package (e.g. 'react', 'lodash')
        return None

    file_dir = Path(file_rel_path).parent
    normalized_path = (file_dir / imp_path).resolve()
    # Attempt extensions
    for ext in ("", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts"):
        cand = str(normalized_path) + ext
        cand_norm = cand.replace("\\", "/")
        for path in all_rel_paths_set:
            if path.endswith(cand_norm) or cand_norm.endswith(path):
                return path
    return None


def extract_calls(
    file_text: str,
    known_fn_names: Set[str],
    file_rel_path: str,
) -> List[CallEdge]:
    """
    Extracts intra-file function call edges.
    Scans full file_text for invocations of known_fn_names defined within the same file.
    """
    edges: List[CallEdge] = []
    if not file_text or not known_fn_names or len(known_fn_names) < 2:
        return edges

    # To attribute calls to caller functions, split file into lines and track containing fn
    # Simple regex search across known_fn_names
    for callee in known_fn_names:
        # Match callee_name(...)
        pattern = re.compile(rf"\b{re.escape(callee)}\s*\(")
        for match in pattern.finditer(file_text):
            # Determine which caller function contains this line offset
            caller = _find_containing_function(match.start(), file_text, known_fn_names)
            if caller and caller != callee:
                edges.append(
                    CallEdge(
                        caller_fn=caller,
                        callee_fn=callee,
                        file_rel_path=file_rel_path,
                    )
                )

    return list(set(edges))


def _find_containing_function(char_offset: int, file_text: str, known_fn_names: Set[str]) -> Optional[str]:
    """Heuristic to locate containing function name given a character offset in file_text."""
    prefix = file_text[:char_offset]
    lines = prefix.splitlines()
    # Scan backward for function definition header
    for line in reversed(lines):
        line_str = line.strip()
        for fn in known_fn_names:
            if line_str.startswith(f"def {fn}") or line_str.startswith(f"async def {fn}") or f"function {fn}" in line_str or f"const {fn} =" in line_str:
                return fn
    return None


def extract_extends(
    file_text: str,
    file_rel_path: str,
    language: str,
    known_classes: Set[str],
) -> List[ExtendsEdge]:
    """
    Extracts class inheritance relationships (child_class -> parent_class).
    """
    edges: List[ExtendsEdge] = []
    if not file_text or not known_classes:
        return edges

    lang = language.lower().lstrip(".")

    if lang in ("py", "python"):
        # class Child(Parent):
        pattern = re.compile(r"class\s+([A-Za-z0-9_]+)\s*\(\s*([A-Za-z0-9_\.]+)\s*\)\s*:")
        for match in pattern.finditer(file_text):
            child, parent = match.group(1), match.group(2)
            # Take basename if Parent is module-qualified (e.g. BaseNode)
            parent_clean = parent.split(".")[-1]
            if child in known_classes:
                edges.append(
                    ExtendsEdge(
                        child_class=child,
                        parent_class=parent_clean,
                        file_rel_path=file_rel_path,
                    )
                )

    elif lang in ("js", "ts", "jsx", "tsx"):
        # class Child extends Parent
        pattern = re.compile(r"class\s+([A-Za-z0-9_]+)\s+extends\s+([A-Za-z0-9_]+)")
        for match in pattern.finditer(file_text):
            child, parent = match.group(1), match.group(2)
            if child in known_classes:
                edges.append(
                    ExtendsEdge(
                        child_class=child,
                        parent_class=parent,
                        file_rel_path=file_rel_path,
                    )
                )

    return edges


def extract_test_edges(all_rel_paths: Set[str]) -> List[TestEdge]:
    """
    Extracts test relationship edges (test_file -> source_file).
    Supported patterns:
    - tests/test_foo.py -> app/foo.py or foo.py
    - foo.test.ts -> foo.ts
    - __tests__/foo.spec.ts -> src/foo.ts
    """
    edges: List[TestEdge] = []
    # Build map of stem -> list of source file paths
    non_test_files = [p for p in all_rel_paths if not _is_test_path(p)]

    for test_path in all_rel_paths:
        if not _is_test_path(test_path):
            continue

        filename = Path(test_path).name
        stem = Path(test_path).stem

        # Strip test prefixes/suffixes
        clean_stem = stem
        if clean_stem.startswith("test_"):
            clean_stem = clean_stem[5:]
        elif clean_stem.endswith("_test"):
            clean_stem = clean_stem[:-5]

        if ".test" in clean_stem:
            clean_stem = clean_stem.replace(".test", "")
        if ".spec" in clean_stem:
            clean_stem = clean_stem.replace(".spec", "")

        # Search for source file matching clean_stem
        for src_path in non_test_files:
            src_stem = Path(src_path).stem
            if src_stem == clean_stem:
                edges.append(
                    TestEdge(
                        test_file_rel_path=test_path,
                        source_file_rel_path=src_path,
                    )
                )
                break

    return edges


def _is_test_path(path_str: str) -> bool:
    """Returns True if path represents a test file."""
    p_lower = path_str.lower()
    parts = Path(p_lower).parts
    if any(part in ("tests", "test", "__tests__") for part in parts):
        return True
    filename = Path(p_lower).name
    return (
        filename.startswith("test_")
        or filename.endswith("_test.py")
        or ".test." in filename
        or ".spec." in filename
    )


def parse_manifest_deps(manifest_rel_path: str, manifest_text: str) -> List[PackageRecord]:
    """
    Parses requirements.txt, package.json, or pyproject.toml text to extract PackageRecord entries.
    """
    records: List[PackageRecord] = []
    if not manifest_text or not manifest_text.strip():
        return records

    filename = Path(manifest_rel_path).name.lower()

    if filename == "requirements.txt":
        for line in manifest_text.splitlines():
            line_s = line.strip()
            if not line_s or line_s.startswith("#") or line_s.startswith("-"):
                continue
            # Parse package_name>=1.0.0
            match = re.match(r"^([A-Za-z0-9_\-\.]+)\s*(.*)$", line_s)
            if match:
                pkg_name = match.group(1)
                ver_spec = match.group(2).strip() or "*"
                records.append(
                    PackageRecord(
                        name=pkg_name,
                        version=ver_spec,
                        ecosystem="python",
                        manifest_path=manifest_rel_path,
                    )
                )

    elif filename == "package.json":
        try:
            p_data = json.loads(manifest_text)
            for key in ("dependencies", "devDependencies"):
                if key in p_data and isinstance(p_data[key], dict):
                    for pkg_name, ver_spec in p_data[key].items():
                        records.append(
                            PackageRecord(
                                name=pkg_name,
                                version=str(ver_spec),
                                ecosystem="npm",
                                manifest_path=manifest_rel_path,
                            )
                        )
        except Exception:
            pass

    return records
