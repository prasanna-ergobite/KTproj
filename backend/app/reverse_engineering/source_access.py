"""Bounded, read-only access helpers for a reverse-engineering checkout."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def resolve_source_path(repository_root: Path, relative_path: str) -> Path:
    """Resolve a repository-relative path and reject traversal and symlinks."""
    root = repository_root.resolve()
    supplied = Path(relative_path)
    if supplied.is_absolute() or ".." in supplied.parts:
        raise ValueError("Source path must be repository-relative.")
    unresolved = root / supplied
    if unresolved.is_symlink() or any(parent.is_symlink() for parent in unresolved.parents if parent != root):
        raise ValueError("Symbolic source paths are not readable.")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Source path escapes the repository checkout.") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("Source path is not a readable repository file.")
    return candidate


def read_source_range(
    repository_root: Path,
    relative_path: str,
    start_line: int,
    end_line: int,
    *,
    max_characters: int = 20_000,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> dict[str, Any]:
    """Read a bounded, one-indexed line range from a repository source file."""
    if start_line < 1 or end_line < start_line:
        raise ValueError("Invalid source line range.")
    path = resolve_source_path(repository_root, relative_path)
    if path.stat().st_size > max_file_bytes:
        raise ValueError("Source file exceeds the bounded read limit.")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = "\n".join(lines[start_line - 1:end_line])
    truncated = len(selected) > max_characters
    return {
        "path": relative_path,
        "start_line": start_line,
        "end_line": min(end_line, len(lines)),
        "text": selected[:max_characters],
        "truncated": truncated,
    }


def search_source(
    repository_root: Path,
    relative_paths: Iterable[str],
    query: str,
    *,
    max_results: int = 50,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Perform a bounded literal source search without invoking repository tools."""
    needle = query.strip().casefold()
    if not needle:
        return []
    results: list[dict[str, Any]] = []
    for relative_path in sorted(set(relative_paths)):
        try:
            path = resolve_source_path(repository_root, relative_path)
            if path.stat().st_size > max_file_bytes:
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if needle in line.casefold():
                    results.append({"path": relative_path, "line": line_number, "text": line[:500]})
                    if len(results) >= max_results:
                        return results
        except (OSError, ValueError):
            continue
    return results


def lookup_symbol(extracted_facts: dict[str, Any], symbol_name: str, *, max_results: int = 25) -> list[dict[str, Any]]:
    """Find symbols by exact qualified name first, then by case-insensitive name."""
    symbols = extracted_facts.get("symbols", [])
    exact = [item for item in symbols if item.get("qualified_name") == symbol_name]
    if exact:
        return exact[:max_results]
    needle = symbol_name.casefold()
    return [item for item in symbols if str(item.get("name", "")).casefold() == needle][:max_results]

