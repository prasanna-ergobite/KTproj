"""
Tree-sitter AST-based Code Chunker for AutoKT.
Parses Python, JavaScript, and TypeScript source files into semantic AST chunks
(functions, methods, class-body chunks) with enriched metadata and fallback
to fixed-window chunking.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from tree_sitter import Language, Parser, Node
import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_typescript

logger = logging.getLogger("autokt.ast_chunker")

# Default character ceiling per chunk before sub-splitting kicks in (~750 tokens)
MAX_AST_CHUNK_CHARS = 3000

# Extensions supported by Tree-sitter grammars
AST_SUPPORTED_LANGUAGES: Set[str] = {"py", "js", "ts", "jsx", "tsx"}

# Initialize Tree-sitter Language instances
try:
    PY_LANGUAGE = Language(tree_sitter_python.language())
    JS_LANGUAGE = Language(tree_sitter_javascript.language())
    TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
    TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())
except Exception as exc:
    logger.warning("Failed to initialize tree-sitter language objects: %s", exc)
    PY_LANGUAGE = JS_LANGUAGE = TS_LANGUAGE = TSX_LANGUAGE = None


@dataclass
class ASTChunk:
    chunk_id: str            # e.g. "{file_id}-fn-{name}-{start_line}"
                             #       "{file_id}-cls-{name}-{start_line}" for class-body chunks
    text: str                # header + docstring + body (full source span)
    start_line: int          # 1-indexed, inclusive
    end_line: int            # 1-indexed, inclusive
    chunking_method: str     # "ast" | "fixed_window"
    function_name: str       # "" for class-body chunks and fixed_window chunks
    class_name: str          # "" for top-level function chunks and fixed_window chunks
    parent_class: str        # == class_name when chunk is a method;
                             # "" for class-body chunks (class_name is set but this
                             #    chunk is not itself inside another class)
                             # "" for top-level function chunks and fixed_window chunks
    language: str            # file extension without dot, e.g. "py"
    chunk_type: str          # "function" | "method" | "class_body" | "fixed_window"
    is_documented: bool      # True if leading docstring/block-comment detected


def _detect_is_documented(text: str, header_line_count: int) -> bool:
    """
    Returns True if the chunk body has a leading docstring or block-comment.
    header_line_count: number of header lines prepended (e.g. 2 for "# File:\n# Function:\n").
    Skips those header lines, then scans forward past blank lines for a
    documentation marker on the first non-blank body line.
    """
    lines = text.splitlines()
    body_lines = lines[header_line_count:]
    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("def ", "async def ", "class ", "@")):
            continue
        if any(stripped.startswith(kw) for kw in (
            "function ", "async function ", "constructor", "get ", "set ",
            "public ", "private ", "protected ", "static ", "export "
        )):
            continue
        return stripped.startswith(('"""', "'''", "//", "/*", "#"))
    return False


def _get_language(language_ext: str) -> Optional[Language]:
    ext = language_ext.lower().lstrip(".")
    if ext == "py":
        return PY_LANGUAGE
    elif ext in ("js", "jsx"):
        return JS_LANGUAGE
    elif ext == "ts":
        return TS_LANGUAGE
    elif ext == "tsx":
        return TSX_LANGUAGE
    return None


def _get_node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _fixed_window_fallback(
    file_text: str,
    file_path: str,
    file_id: str,
    language: str,
    chunk_size: int = 800,
    overlap: int = 100,
) -> List[ASTChunk]:
    """
    Fixed-window chunker fallback when AST parsing is unavailable or produces 0 nodes.
    """
    if not file_text or not file_text.strip():
        return []

    total_len = len(file_text)

    raw_slices: List[Tuple[str, int, int]] = []
    step = chunk_size - overlap
    if step <= 0:
        step = chunk_size

    for start_char in range(0, total_len, step):
        end_char = min(start_char + chunk_size, total_len)
        slice_text = file_text[start_char:end_char]
        if slice_text.strip():
            start_l = file_text[:start_char].count("\n") + 1
            end_l = file_text[:end_char].count("\n") + 1
            raw_slices.append((slice_text, start_l, end_l))

    total_chunks = len(raw_slices)
    chunks: List[ASTChunk] = []

    for idx, (slice_text, start_l, end_l) in enumerate(raw_slices, start=1):
        header = f"# File: {file_path}\n# Chunk: {idx}/{total_chunks}\n"
        full_text = header + "\n" + slice_text
        chunks.append(
            ASTChunk(
                chunk_id=f"{file_id}-chunk-{idx}",
                text=full_text,
                start_line=start_l,
                end_line=end_l,
                chunking_method="fixed_window",
                function_name="",
                class_name="",
                parent_class="",
                language=language,
                chunk_type="fixed_window",
                is_documented=False,
            )
        )
    return chunks


def _sub_split_giant_chunk(
    base_chunk: ASTChunk,
    header: str,
    raw_body: str,
    file_id: str,
    max_chunk_chars: int,
) -> List[ASTChunk]:
    """
    Sub-splits an AST chunk whose body exceeds max_chunk_chars, retaining
    AST metadata while applying sliding-window segmentation.
    """
    chunk_size = max(max_chunk_chars - len(header) - 50, 500)
    overlap = 100
    step = chunk_size - overlap
    if step <= 0:
        step = chunk_size

    sub_chunks: List[ASTChunk] = []
    body_len = len(raw_body)
    sub_idx = 1

    for start in range(0, body_len, step):
        end = min(start + chunk_size, body_len)
        part_body = raw_body[start:end]
        if not part_body.strip():
            continue

        full_text = header + "\n" + part_body
        tag = base_chunk.function_name or base_chunk.class_name or "body"
        sub_chunk_id = f"{file_id}-fn-{tag}-{base_chunk.start_line}-part-{sub_idx}"

        sub_chunks.append(
            ASTChunk(
                chunk_id=sub_chunk_id,
                text=full_text,
                start_line=base_chunk.start_line,
                end_line=base_chunk.end_line,
                chunking_method="ast",
                function_name=base_chunk.function_name,
                class_name=base_chunk.class_name,
                parent_class=base_chunk.parent_class,
                language=base_chunk.language,
                chunk_type=base_chunk.chunk_type,
                is_documented=base_chunk.is_documented,
            )
        )

        sub_idx += 1

    return sub_chunks or [base_chunk]


def _unwrap_python_decorated(node: Node) -> Tuple[Node, List[Node]]:
    """Unwraps a decorated_definition node in Python to extract decorators and target definition."""
    decorators = []
    target = node
    for child in node.children:
        if child.type == "decorator":
            decorators.append(child)
        elif child.type in ("function_definition", "async_function_definition", "class_definition"):
            target = child
    return target, decorators


def _parse_python_ast(
    root: Node,
    source_bytes: bytes,
    file_path: str,
    file_id: str,
    language: str,
    max_chunk_chars: int,
) -> List[ASTChunk]:
    """
    Extracts Python class-body and function/method AST chunks.
    """
    chunks: List[ASTChunk] = []

    for child in root.children:
        target_node = child
        if child.type == "decorated_definition":
            target_node, _ = _unwrap_python_decorated(child)

        # 1. Class Definition
        if target_node.type == "class_definition":
            class_name_node = target_node.child_by_field_name("name")
            class_name = _get_node_text(class_name_node, source_bytes) if class_name_node else "UnknownClass"
            class_start_line = child.start_point[0] + 1
            class_end_line = child.end_point[0] + 1

            # Class Body Chunk
            block_node = target_node.child_by_field_name("body")
            class_non_method_parts: List[str] = []
            if block_node:
                for b_child in block_node.children:
                    b_target = b_child
                    if b_child.type == "decorated_definition":
                        b_target, _ = _unwrap_python_decorated(b_child)

                    if b_target.type not in ("function_definition", "async_function_definition"):
                        txt = _get_node_text(b_child, source_bytes)
                        if txt.strip():
                            class_non_method_parts.append(txt)

            class_body_source = "\n".join(class_non_method_parts) if class_non_method_parts else _get_node_text(child, source_bytes)
            header = f"# File: {file_path}\n# Class: {class_name}\n"
            full_text = header + "\n" + class_body_source

            base_class_chunk = ASTChunk(
                chunk_id=f"{file_id}-cls-{class_name}-{class_start_line}",
                text=full_text,
                start_line=class_start_line,
                end_line=class_end_line,
                chunking_method="ast",
                function_name="",
                class_name=class_name,
                parent_class="",
                language=language,
                chunk_type="class_body",
                is_documented=_detect_is_documented(full_text, 2),
            )

            if len(full_text) > max_chunk_chars:
                chunks.extend(_sub_split_giant_chunk(base_class_chunk, header, class_body_source, file_id, max_chunk_chars))
            else:
                chunks.append(base_class_chunk)

            # Class Methods
            if block_node:
                for b_child in block_node.children:
                    m_node = b_child
                    if b_child.type == "decorated_definition":
                        m_node, _ = _unwrap_python_decorated(b_child)

                    if m_node.type in ("function_definition", "async_function_definition"):
                        m_name_node = m_node.child_by_field_name("name")
                        m_name = _get_node_text(m_name_node, source_bytes) if m_name_node else "anonymous"
                        m_start = b_child.start_point[0] + 1
                        m_end = b_child.end_point[0] + 1

                        m_source = _get_node_text(b_child, source_bytes)
                        m_header = f"# File: {file_path}\n# Class: {class_name}\n# Function: {m_name}\n"
                        m_full_text = m_header + "\n" + m_source

                        base_m_chunk = ASTChunk(
                            chunk_id=f"{file_id}-fn-{m_name}-{m_start}",
                            text=m_full_text,
                            start_line=m_start,
                            end_line=m_end,
                            chunking_method="ast",
                            function_name=m_name,
                            class_name=class_name,
                            parent_class=class_name,
                            language=language,
                            chunk_type="method",
                            is_documented=_detect_is_documented(m_full_text, 3),
                        )

                        if len(m_full_text) > max_chunk_chars:
                            chunks.extend(_sub_split_giant_chunk(base_m_chunk, m_header, m_source, file_id, max_chunk_chars))
                        else:
                            chunks.append(base_m_chunk)

        # 2. Top-level Function Definition
        elif target_node.type in ("function_definition", "async_function_definition"):
            fn_name_node = target_node.child_by_field_name("name")
            fn_name = _get_node_text(fn_name_node, source_bytes) if fn_name_node else "anonymous"
            fn_start = child.start_point[0] + 1
            fn_end = child.end_point[0] + 1

            fn_source = _get_node_text(child, source_bytes)
            fn_header = f"# File: {file_path}\n# Function: {fn_name}\n"
            fn_full_text = fn_header + "\n" + fn_source

            base_fn_chunk = ASTChunk(
                chunk_id=f"{file_id}-fn-{fn_name}-{fn_start}",
                text=fn_full_text,
                start_line=fn_start,
                end_line=fn_end,
                chunking_method="ast",
                function_name=fn_name,
                class_name="",
                parent_class="",
                language=language,
                chunk_type="function",
                is_documented=_detect_is_documented(fn_full_text, 2),
            )

            if len(fn_full_text) > max_chunk_chars:
                chunks.extend(_sub_split_giant_chunk(base_fn_chunk, fn_header, fn_source, file_id, max_chunk_chars))
            else:
                chunks.append(base_fn_chunk)

    return chunks


def _parse_js_ts_ast(
    root: Node,
    source_bytes: bytes,
    file_path: str,
    file_id: str,
    language: str,
    max_chunk_chars: int,
) -> List[ASTChunk]:
    """
    Extracts JavaScript / TypeScript class-body and function/method AST chunks.
    """
    chunks: List[ASTChunk] = []

    def unwrap_export(node: Node) -> Node:
        if node.type == "export_statement":
            for c in node.children:
                if c.type in (
                    "function_declaration",
                    "class_declaration",
                    "lexical_declaration",
                    "variable_declaration",
                ):
                    return c
        return node

    for child in root.children:
        unwrapped = unwrap_export(child)

        # 1. Named Function Declaration
        if unwrapped.type == "function_declaration":
            fn_name_node = unwrapped.child_by_field_name("name")
            fn_name = _get_node_text(fn_name_node, source_bytes) if fn_name_node else "anonymous"
            fn_start = child.start_point[0] + 1
            fn_end = child.end_point[0] + 1

            fn_source = _get_node_text(child, source_bytes)
            fn_header = f"# File: {file_path}\n# Function: {fn_name}\n"
            fn_full_text = fn_header + "\n" + fn_source

            base_fn_chunk = ASTChunk(
                chunk_id=f"{file_id}-fn-{fn_name}-{fn_start}",
                text=fn_full_text,
                start_line=fn_start,
                end_line=fn_end,
                chunking_method="ast",
                function_name=fn_name,
                class_name="",
                parent_class="",
                language=language,
                chunk_type="function",
                is_documented=_detect_is_documented(fn_full_text, 2),
            )

            if len(fn_full_text) > max_chunk_chars:
                chunks.extend(_sub_split_giant_chunk(base_fn_chunk, fn_header, fn_source, file_id, max_chunk_chars))
            else:
                chunks.append(base_fn_chunk)

        # 2. Module-scope Arrow / Function Expression via Variable Declarator
        elif unwrapped.type in ("lexical_declaration", "variable_declaration"):
            for decl in unwrapped.children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    value_node = decl.child_by_field_name("value")
                    if name_node and value_node and value_node.type in ("arrow_function", "function_expression"):
                        fn_name = _get_node_text(name_node, source_bytes)
                        fn_start = child.start_point[0] + 1
                        fn_end = child.end_point[0] + 1

                        fn_source = _get_node_text(child, source_bytes)
                        fn_header = f"# File: {file_path}\n# Function: {fn_name}\n"
                        fn_full_text = fn_header + "\n" + fn_source

                        base_fn_chunk = ASTChunk(
                            chunk_id=f"{file_id}-fn-{fn_name}-{fn_start}",
                            text=fn_full_text,
                            start_line=fn_start,
                            end_line=fn_end,
                            chunking_method="ast",
                            function_name=fn_name,
                            class_name="",
                            parent_class="",
                            language=language,
                            chunk_type="function",
                            is_documented=_detect_is_documented(fn_full_text, 2),
                        )

                        if len(fn_full_text) > max_chunk_chars:
                            chunks.extend(_sub_split_giant_chunk(base_fn_chunk, fn_header, fn_source, file_id, max_chunk_chars))
                        else:
                            chunks.append(base_fn_chunk)

        # 3. Class Declaration
        elif unwrapped.type == "class_declaration":
            class_name_node = unwrapped.child_by_field_name("name")
            class_name = _get_node_text(class_name_node, source_bytes) if class_name_node else "UnknownClass"
            class_start = child.start_point[0] + 1
            class_end = child.end_point[0] + 1

            class_body = unwrapped.child_by_field_name("body")
            class_non_method_parts: List[str] = []
            if class_body:
                for cb_child in class_body.children:
                    if cb_child.type != "method_definition":
                        txt = _get_node_text(cb_child, source_bytes)
                        if txt.strip():
                            class_non_method_parts.append(txt)

            class_body_source = "\n".join(class_non_method_parts) if class_non_method_parts else _get_node_text(child, source_bytes)
            header = f"# File: {file_path}\n# Class: {class_name}\n"
            full_text = header + "\n" + class_body_source

            base_class_chunk = ASTChunk(
                chunk_id=f"{file_id}-cls-{class_name}-{class_start}",
                text=full_text,
                start_line=class_start,
                end_line=class_end,
                chunking_method="ast",
                function_name="",
                class_name=class_name,
                parent_class="",
                language=language,
                chunk_type="class_body",
                is_documented=_detect_is_documented(full_text, 2),
            )

            if len(full_text) > max_chunk_chars:
                chunks.extend(_sub_split_giant_chunk(base_class_chunk, header, class_body_source, file_id, max_chunk_chars))
            else:
                chunks.append(base_class_chunk)

            # Class Methods
            if class_body:
                for cb_child in class_body.children:
                    if cb_child.type == "method_definition":
                        m_name_node = cb_child.child_by_field_name("name")
                        m_name = _get_node_text(m_name_node, source_bytes) if m_name_node else "anonymous"
                        m_start = cb_child.start_point[0] + 1
                        m_end = cb_child.end_point[0] + 1

                        m_source = _get_node_text(cb_child, source_bytes)
                        m_header = f"# File: {file_path}\n# Class: {class_name}\n# Function: {m_name}\n"
                        m_full_text = m_header + "\n" + m_source

                        base_m_chunk = ASTChunk(
                            chunk_id=f"{file_id}-fn-{m_name}-{m_start}",
                            text=m_full_text,
                            start_line=m_start,
                            end_line=m_end,
                            chunking_method="ast",
                            function_name=m_name,
                            class_name=class_name,
                            parent_class=class_name,
                            language=language,
                            chunk_type="method",
                            is_documented=_detect_is_documented(m_full_text, 3),
                        )

                        if len(m_full_text) > max_chunk_chars:
                            chunks.extend(_sub_split_giant_chunk(base_m_chunk, m_header, m_source, file_id, max_chunk_chars))
                        else:
                            chunks.append(base_m_chunk)

    return chunks


def chunk_file(
    file_text: str,
    file_path: str,
    file_id: str,
    language: str,
    max_chunk_chars: int = MAX_AST_CHUNK_CHARS,
) -> List[ASTChunk]:
    """
    Parse file_text with Tree-sitter and return AST chunks (functions, methods, class-body).
    Falls back to fixed-window chunker for the entire file if parsing fails, language is unsupported,
    or 0 AST nodes are extracted.
    Never raises exceptions. Returns [] for empty files.
    """
    if not file_text or not file_text.strip():
        return []

    clean_lang = language.lower().lstrip(".")
    if clean_lang not in AST_SUPPORTED_LANGUAGES:
        return _fixed_window_fallback(file_text, file_path, file_id, clean_lang)

    ts_lang = _get_language(clean_lang)
    if not ts_lang:
        return _fixed_window_fallback(file_text, file_path, file_id, clean_lang)

    try:
        parser = Parser(ts_lang)
        source_bytes = file_text.encode("utf-8")
        tree = parser.parse(source_bytes)
        root = tree.root_node

        if root.has_error:
            logger.info("AST root for '%s' contains syntax errors. Falling back to fixed-window.", file_path)
            return _fixed_window_fallback(file_text, file_path, file_id, clean_lang)

        if clean_lang == "py":
            ast_chunks = _parse_python_ast(root, source_bytes, file_path, file_id, clean_lang, max_chunk_chars)
        else:
            ast_chunks = _parse_js_ts_ast(root, source_bytes, file_path, file_id, clean_lang, max_chunk_chars)

        if not ast_chunks:
            logger.info("AST parsing for '%s' produced 0 nodes. Falling back to fixed-window.", file_path)
            return _fixed_window_fallback(file_text, file_path, file_id, clean_lang)

        return ast_chunks

    except Exception as exc:
        logger.warning("AST chunking failed for '%s' (%s): %s. Falling back to fixed-window.", file_path, clean_lang, exc)
        return _fixed_window_fallback(file_text, file_path, file_id, clean_lang)
