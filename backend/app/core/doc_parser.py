"""
Document Parser Module for AutoKT.
Extracts text from uploaded documents (Markdown, DOCX, PDF, TXT, RST)
and determines appropriate chunking tier ("heading_aware" vs "fixed_window").
"""

import re
import logging
from pathlib import Path
from typing import Tuple, List, Any

logger = logging.getLogger("autokt.doc_parser")

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None


# Regex patterns for heading detection
_NUMBERED_H3 = re.compile(r"^\d+\.\d+\.\d+\s+[A-Za-z].*")
_NUMBERED_H2 = re.compile(r"^\d+\.\d+\s+[A-Za-z].*")
_NUMBERED_H1 = re.compile(r"^\d+\.\s+[A-Z].*")
_NAMED_H1 = re.compile(r"^(?:Section|Chapter|Appendix)\s+[\dA-Z]+[:\.]", re.IGNORECASE)

# TOC entry suppression regex: e.g. "3. User Personas & Key Workflows 4" or "5.1 Primary User Personas 4"
_TOC_LINE_PATTERN = re.compile(r"^\d+(?:\.\d+)*\.?\s+.+\s+\d{1,3}$")

# Common footer noise regexes
_FOOTER_NOISE_PATTERN = re.compile(r"(?:Confidential|Page\s+\d+\s+of\s+\d+)", re.IGNORECASE)


def _detect_pdf_headings_and_convert_to_md(pages_text: List[str]) -> Tuple[str, str]:
    """
    Detects section headings in PDF text streams and converts matched lines to Markdown (# / ## / ###).
    Returns (converted_markdown_text, "heading_aware") if >= 2 H1-level headings are found,
    else returns (raw_joined_text, "fixed_window").
    """
    if not pages_text:
        return "", "fixed_window"

    # Step 1: Running header/footer dedup across pages
    page_short_lines = []
    line_page_counts = {}

    for page_str in pages_text:
        lines = [line.strip() for line in page_str.splitlines() if line.strip()]
        short_lines_set = set()
        for line in lines:
            if len(line) < 120:
                short_lines_set.add(line)
        page_short_lines.append(short_lines_set)
        for line in short_lines_set:
            line_page_counts[line] = line_page_counts.get(line, 0) + 1

    # Lines appearing on >= 2 pages are marked as running noise
    running_noise = {line for line, count in line_page_counts.items() if count >= 2}

    # Step 2 & 3: Process pages and classify lines
    converted_pages = []
    h1_count = 0

    for page_str in pages_text:
        converted_lines = []
        for line in page_str.splitlines():
            stripped = line.strip()
            if not stripped:
                converted_lines.append(line)
                continue

            # Strip running noise or explicit footer noise
            if stripped in running_noise or _FOOTER_NOISE_PATTERN.search(stripped):
                continue

            # Skip TOC entries (ending in bare page number)
            if _TOC_LINE_PATTERN.match(stripped):
                continue

            # Check heading classification
            if _NUMBERED_H3.match(stripped):
                converted_lines.append(f"### {stripped}")
            elif _NUMBERED_H2.match(stripped):
                converted_lines.append(f"## {stripped}")
            elif _NUMBERED_H1.match(stripped) or _NAMED_H1.match(stripped):
                converted_lines.append(f"# {stripped}")
                h1_count += 1
            else:
                converted_lines.append(line)

        converted_pages.append("\n".join(converted_lines))

    # Step 4: Fallback gate (< 2 H1 headings -> fallback to fixed_window)
    if h1_count < 2:
        return "\n\n".join(pages_text), "fixed_window"

    return "\n\n".join(converted_pages), "heading_aware"


def parse_document(file_path: Path, filename: str) -> Tuple[str, str]:
    """
    Extracts text content from a file and returns (extracted_text, chunking_method).

    - .md: heading_aware
    - .docx: heading_aware (via python-docx)
    - .pdf: fixed_window (via pypdf)
    - .txt, .rst: fixed_window
    - fallback: fixed_window
    """
    ext = Path(filename).suffix.lower()

    if ext == ".md":
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            return text, "heading_aware"
        except Exception as exc:
            logger.warning("Error reading markdown file '%s': %s", filename, exc)
            return "", "heading_aware"

    elif ext == ".docx":
        if docx is not None:
            try:
                doc = docx.Document(str(file_path))
                md_lines: List[str] = []
                for p in doc.paragraphs:
                    p_text = p.text.strip()
                    if not p_text:
                        continue
                    style_name = (p.style.name or "").lower() if p.style else ""
                    if "heading 1" in style_name:
                        md_lines.append(f"# {p_text}")
                    elif "heading 2" in style_name:
                        md_lines.append(f"## {p_text}")
                    elif "heading 3" in style_name or any(f"heading {n}" in style_name for n in range(4, 10)):
                        md_lines.append(f"### {p_text}")
                    else:
                        md_lines.append(p_text)
                return "\n\n".join(md_lines), "heading_aware"
            except Exception as exc:
                logger.warning("Error parsing DOCX file '%s', falling back to raw text: %s", filename, exc)

        # Fallback reading raw text
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            return text, "fixed_window"
        except Exception:
            return "", "fixed_window"

    elif ext == ".pdf":
        if pypdf is not None:
            try:
                reader = pypdf.PdfReader(str(file_path))
                pages_text = []
                for page in reader.pages:
                    txt = page.extract_text()
                    if txt:
                        pages_text.append(txt)
                if pages_text:
                    return _detect_pdf_headings_and_convert_to_md(pages_text)
                return "", "fixed_window"
            except Exception as exc:
                logger.warning("Error parsing PDF file '%s': %s", filename, exc)
                return "", "fixed_window"

        # Fallback reading raw text if pypdf unavailable
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            return text, "fixed_window"
        except Exception:
            return "", "fixed_window"

    else:
        # Plain text (.txt, .rst, fallback)
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            return text, "fixed_window"
        except Exception as exc:
            logger.warning("Error reading plain text file '%s': %s", filename, exc)
            return "", "fixed_window"


def get_docx_paragraphs(file_path: Path) -> List[Any]:
    """Helper returning python-docx Paragraph objects for DOCX files (uninvoked as of Milestone 21)."""
    if docx is not None and file_path.suffix.lower() == ".docx":
        try:
            doc = docx.Document(str(file_path))
            return list(doc.paragraphs)
        except Exception as exc:
            logger.warning("Error extracting paragraphs from DOCX '%s': %s", file_path, exc)
    return []
