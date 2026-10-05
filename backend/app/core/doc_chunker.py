"""
Document Chunker Module for AutoKT.
Provides two-tier document chunking:
1. Heading-aware chunking (Markdown and DOCX) with section hierarchy,
   heading_path tracking, parent_section_id linking, code-fence/table guards,
   and short-section merging.
2. Fixed-window chunking (PDF, TXT, RST, and fallback) with 15% overlap
   and paragraph boundary awareness.
"""

import re
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("autokt.doc_chunker")

FIXED_WINDOW_TARGET_CHARS = 2000
FIXED_WINDOW_OVERLAP_CHARS = 300    # 15% overlap
MIN_SECTION_TOKENS = 15             # ~60 chars (~15 tokens); stub sections below this merge into parent
HEADING_AWARE_MAX_CHARS = 2400      # ~600 tokens; sections above this sub-split


@dataclass
class DocChunk:
    chunk_id: str                   # e.g. "{doc_id}:chunk:{index}"
    doc_id: str                     # source doc id (unscoped)
    text: str                       # header prefix + section body
    chunk_index: int                # 0-indexed within document
    start_char: int                 # char offset in source text
    end_char: int                   # char end offset in source text
    heading_path: List[str]         # ["H1", "H2", ...]; [] for fixed-window
    parent_section_id: str          # chunk_id of parent section; "" if root/fixed-window
    section_level: int              # 0=root/fixed-window, 1/2/3 = H1-H3
    chunking_method: str            # "heading_aware" | "fixed_window"
    source_type: str                # "doc" | "transcript"
    # --- Enriched metadata fields (added Milestone 12) ---
    chunk_type: str = ""            # "heading_body" | "fixed_window" (explicit enum, not inferred)
    chunk_total: int = 0            # total chunks in this document (M in # Chunk: N/M)
    has_code_fence: bool = False    # True if chunk text contains any ``` or ~~~ fence marker
    is_stub_or_empty: bool = False  # True for heading-body chunks emitted with empty body text


def _count_tokens_approx(text: str) -> int:
    """Approximate token count without external tokenizer dependency."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _detect_code_fence(text: str) -> bool:
    """
    Returns True if text contains any ``` or ~~~ fence marker.
    Applied per-chunk — may return True for a partial fence (opener/closer
    split across sub-chunks by the long-section sub-splitter).
    Reuses the same fence-detection logic present in the heading-aware parser,
    without a second parse pass.
    """
    return bool(re.search(r"```|~~~", text))


def _build_header(filename: str, heading_path: List[str], chunk_index: int, total_chunks: int) -> str:
    """Build standardized header prefix matching AST chunker header conventions."""
    section_str = " > ".join(heading_path) if heading_path else "root"
    return f"# Document: {filename}\n# Section: {section_str}\n# Chunk: {chunk_index + 1}/{total_chunks}\n"


def _fixed_window_chunk(
    text: str,
    doc_id: str,
    filename: str,
    source_type: str = "doc",
    target_chars: int = FIXED_WINDOW_TARGET_CHARS,
    overlap_chars: int = FIXED_WINDOW_OVERLAP_CHARS,
) -> List[DocChunk]:
    """
    Fixed-window chunking fallback for unstructured text (PDF, TXT, RST).
    Splits text into chunks of target size with overlap, preserving paragraph breaks where possible.
    """
    if not text or not text.strip():
        return []

    total_len = len(text)
    step = target_chars - overlap_chars
    if step <= 0:
        step = target_chars

    raw_slices: List[Tuple[str, int, int]] = []
    start = 0

    while start < total_len:
        end = min(start + target_chars, total_len)

        # Try to adjust end to paragraph break \n\n or newline \n if not at text end
        if end < total_len:
            para_break = text.rfind("\n\n", start + step // 2, end)
            if para_break != -1:
                end = para_break + 2
            else:
                line_break = text.rfind("\n", start + step // 2, end)
                if line_break != -1:
                    end = line_break + 1

        slice_text = text[start:end]
        if slice_text.strip():
            raw_slices.append((slice_text, start, end))

        if end >= total_len:
            break
        start += step

    total_chunks = len(raw_slices)
    chunks: List[DocChunk] = []

    for idx, (slice_text, s_char, e_char) in enumerate(raw_slices):
        chunk_id = f"{doc_id}:chunk:{idx}"
        header = _build_header(filename, [], idx, total_chunks)
        full_text = header + "\n" + slice_text.strip()

        chunks.append(
            DocChunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                text=full_text,
                chunk_index=idx,
                start_char=s_char,
                end_char=e_char,
                heading_path=[],
                parent_section_id="",
                section_level=0,
                chunking_method="fixed_window",
                source_type=source_type,
                chunk_type="fixed_window",
                chunk_total=total_chunks,
                has_code_fence=_detect_code_fence(full_text),
                is_stub_or_empty=False,
            )
        )

    return chunks


# ---------------------------------------------------------------------------
# Heading-Aware Markdown Chunker
# ---------------------------------------------------------------------------

@dataclass
class _RawSection:
    heading_title: str
    level: int
    heading_path: List[str]
    body_lines: List[str]
    start_char: int
    end_char: int
    children: List["_RawSection"]
    parent: Optional["_RawSection"] = None


def _heading_aware_chunk_markdown(
    text: str,
    doc_id: str,
    filename: str,
    source_type: str = "doc",
) -> List[DocChunk]:
    """
    Parses Markdown into hierarchical sections based on # / ## / ### headers.
    Guards code fences and Markdown tables against false-positive heading matches.
    """
    if not text or not text.strip():
        return []

    lines = text.splitlines(keepends=True)
    
    # 1. Parse lines into raw sections
    sections: List[_RawSection] = []
    stack: List[Tuple[int, _RawSection]] = []  # (level, section_node)

    root_section = _RawSection(
        heading_title="",
        level=0,
        heading_path=[],
        body_lines=[],
        start_char=0,
        end_char=len(text),
        children=[],
        parent=None,
    )
    sections.append(root_section)
    stack.append((0, root_section))

    in_code_fence = False
    current_char = 0

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

    for line in lines:
        line_len = len(line)
        stripped = line.strip()

        # Toggle code fence state
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence
            stack[-1][1].body_lines.append(line)
            current_char += line_len
            continue

        # If not inside code fence, check for Markdown heading
        if not in_code_fence:
            match = heading_pattern.match(stripped)
            if match:
                level = len(match.group(1))
                title = match.group(2).strip()

                # Pop stack until top is higher level (smaller number)
                while stack and stack[-1][0] >= level:
                    stack.pop()

                parent_sec = stack[-1][1] if stack else root_section
                path = parent_sec.heading_path + [title] if parent_sec.heading_title else [title]

                new_sec = _RawSection(
                    heading_title=title,
                    level=level,
                    heading_path=path,
                    body_lines=[],
                    start_char=current_char,
                    end_char=current_char + line_len,
                    children=[],
                    parent=parent_sec,
                )

                parent_sec.children.append(new_sec)
                sections.append(new_sec)
                stack.append((level, new_sec))

                current_char += line_len
                continue

        # Regular body line (or code fence line)
        stack[-1][1].body_lines.append(line)
        current_char += line_len

    # If no Markdown headings were found (only root_section exists), fallback to fixed window
    if len(sections) == 1 and not root_section.heading_title:
        return _fixed_window_chunk(text, doc_id, filename, source_type)

    # 2. Short-section merge: merge leaf sections with body < MIN_SECTION_TOKENS into parent HEADING (parent.level >= 1)
    for sec in list(sections):
        if sec == root_section or not sec.parent:
            continue
        # Merge into parent ONLY if parent is a heading (level >= 1), not root_section
        if sec.parent and sec.parent.level >= 1 and not sec.children:
            body_text = "".join(sec.body_lines).strip()
            if _count_tokens_approx(body_text) < MIN_SECTION_TOKENS:
                level_hashes = "#" * sec.level if sec.level > 0 else "###"
                merged_prefix = f"\n{level_hashes} {sec.heading_title}\n" if sec.heading_title else "\n"
                sec.parent.body_lines.append(merged_prefix + body_text)
                if sec in sec.parent.children:
                    sec.parent.children.remove(sec)
                if sec in sections:
                    sections.remove(sec)

    # Filter out empty root section if it has no direct body text
    active_sections: List[_RawSection] = []
    for sec in sections:
        body_str = "".join(sec.body_lines).strip()
        if sec == root_section:
            if body_str:
                active_sections.append(sec)
        else:
            # Heading sections are active if they exist
            active_sections.append(sec)

    if not active_sections:
        return _fixed_window_chunk(text, doc_id, filename, source_type)

    # 3. Create DocChunks from active sections
    sec_to_chunk_id: Dict[id, str] = {}
    chunk_specs: List[Dict[str, Any]] = []

    for sec in active_sections:
        body_text = "".join(sec.body_lines).strip()
        parent_chunk_id = sec_to_chunk_id.get(id(sec.parent), "") if sec.parent else ""

        # Sub-split long section body if > HEADING_AWARE_MAX_CHARS
        if len(body_text) > HEADING_AWARE_MAX_CHARS:
            sub_slices = _sub_split_text(body_text, HEADING_AWARE_MAX_CHARS)
            first_sub_id = ""
            for sub_idx, sub_text in enumerate(sub_slices):
                spec = {
                    "heading_path": sec.heading_path,
                    "section_level": sec.level,
                    "parent_section_id": parent_chunk_id if sub_idx == 0 else first_sub_id,
                    "body_text": sub_text,
                    "start_char": sec.start_char,
                    "end_char": sec.end_char,
                }
                chunk_specs.append(spec)
                if sub_idx == 0:
                    sec_anchor_id = f"{doc_id}:chunk:{len(chunk_specs) - 1}"
                    sec_to_chunk_id[id(sec)] = sec_anchor_id
                    first_sub_id = sec_anchor_id
        else:
            spec = {
                "heading_path": sec.heading_path,
                "section_level": sec.level,
                "parent_section_id": parent_chunk_id,
                "body_text": body_text,
                "start_char": sec.start_char,
                "end_char": sec.end_char,
            }
            chunk_specs.append(spec)
            sec_anchor_id = f"{doc_id}:chunk:{len(chunk_specs) - 1}"
            sec_to_chunk_id[id(sec)] = sec_anchor_id

    total_chunks = len(chunk_specs)
    chunks: List[DocChunk] = []

    for idx, spec in enumerate(chunk_specs):
        chunk_id = f"{doc_id}:chunk:{idx}"
        header = _build_header(filename, spec["heading_path"], idx, total_chunks)
        full_text = header + "\n" + spec["body_text"] if spec["body_text"] else header

        chunks.append(
            DocChunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                text=full_text,
                chunk_index=idx,
                start_char=spec["start_char"],
                end_char=spec["end_char"],
                heading_path=spec["heading_path"],
                parent_section_id=spec["parent_section_id"],
                section_level=spec["section_level"],
                chunking_method="heading_aware",
                source_type=source_type,
                chunk_type="heading_body",
                chunk_total=total_chunks,
                has_code_fence=_detect_code_fence(full_text),
                is_stub_or_empty=(spec["body_text"] == ""),
            )
        )

    return chunks


def _sub_split_text(text: str, max_chars: int) -> List[str]:
    """Sub-split a long section body into chunks <= max_chars with paragraph/line breaks."""
    slices: List[str] = []
    paras = text.split("\n\n")
    current: List[str] = []
    current_len = 0

    for p in paras:
        p_len = len(p)
        if current_len + p_len + 2 <= max_chars:
            current.append(p)
            current_len += p_len + 2
        else:
            if current:
                slices.append("\n\n".join(current))
                current = []
                current_len = 0
            if p_len > max_chars:
                lines = p.split("\n")
                line_curr: List[str] = []
                line_len = 0
                for l in lines:
                    if line_len + len(l) + 1 <= max_chars:
                        line_curr.append(l)
                        line_len += len(l) + 1
                    else:
                        if line_curr:
                            slices.append("\n".join(line_curr))
                            line_curr = []
                            line_len = 0
                        slices.append(l[:max_chars])
                if line_curr:
                    slices.append("\n".join(line_curr))
            else:
                current.append(p)
                current_len = p_len

    if current:
        slices.append("\n\n".join(current))

    return [s for s in slices if s.strip()] or [text[:max_chars]]


# ---------------------------------------------------------------------------
# Heading-Aware DOCX Paragraph Chunker
# ---------------------------------------------------------------------------

def _heading_aware_chunk_docx_paragraphs(
    paragraphs: List[Any],
    doc_id: str,
    filename: str,
    source_type: str = "doc",
) -> List[DocChunk]:
    """
    RETIRED (Milestone 21): doc_parser.py now performs style-to-Markdown conversion upstream
    (Heading 1 -> #, Heading 2 -> ##, Heading 3+ -> ###). This function is permanently
    unreachable via the public API. Retained for reference only.

    Parses DOCX paragraphs into heading-aware chunks based on paragraph style names ('Heading 1/2/3').
    """
    if not paragraphs:
        return []

    text_lines: List[str] = []
    for p in paragraphs:
        p_text = getattr(p, "text", str(p)).strip()
        if not p_text:
            continue
        style_name = ""
        if hasattr(p, "style") and hasattr(p.style, "name"):
            style_name = str(p.style.name).lower()

        if "heading 1" in style_name:
            text_lines.append(f"\n# {p_text}\n")
        elif "heading 2" in style_name:
            text_lines.append(f"\n## {p_text}\n")
        elif "heading 3" in style_name:
            text_lines.append(f"\n### {p_text}\n")
        else:
            text_lines.append(f"{p_text}\n")

    full_md_text = "\n".join(text_lines)
    return _heading_aware_chunk_markdown(full_md_text, doc_id, filename, source_type)


# ---------------------------------------------------------------------------
# Public Dispatch API
# ---------------------------------------------------------------------------

def chunk_document(
    text: str,
    doc_id: str,
    filename: str,
    source_type: str = "doc",
    chunking_method: str = "heading_aware",
) -> List[DocChunk]:
    """
    Public entry point for document chunking.
    Dispatches to heading-aware or fixed-window chunker based on chunking_method.
    """
    if not text or not text.strip():
        return []

    if chunking_method == "heading_aware":
        try:
            chunks = _heading_aware_chunk_markdown(text, doc_id, filename, source_type)
            if chunks:
                return chunks
        except Exception as exc:
            logger.warning("Heading-aware chunking failed for '%s', falling back to fixed-window: %s", filename, exc)

    return _fixed_window_chunk(text, doc_id, filename, source_type)
