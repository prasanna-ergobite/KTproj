"""Safe packaging helpers for reverse-engineering run artifacts."""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


def _safe_artifact(run_root: Path, relative_path: str, max_file_bytes: int) -> Path:
    root = run_root.resolve()
    supplied = PurePosixPath(relative_path)
    if "\\" in relative_path or supplied.is_absolute() or ".." in supplied.parts or not supplied.parts:
        raise ValueError("Artifact reference must be a safe run-relative path.")
    unresolved = root.joinpath(*supplied.parts)
    if unresolved.is_symlink() or any(parent.is_symlink() for parent in unresolved.parents if parent != root):
        raise ValueError("Symbolic artifact paths cannot be packaged.")
    artifact = unresolved.resolve()
    try:
        artifact.relative_to(root)
    except ValueError as exc:
        raise ValueError("Artifact reference escapes the run directory.") from exc
    if not artifact.is_file():
        raise ValueError(f"Artifact is missing: {relative_path}")
    if artifact.stat().st_size > max_file_bytes:
        raise ValueError(f"Artifact exceeds the bundle file limit: {relative_path}")
    return artifact


def build_download_bundle(
    run_root: Path,
    artifact_refs: dict[str, str],
    documents: list[dict[str, Any]],
    *,
    max_file_bytes: int = 20 * 1024 * 1024,
    max_total_bytes: int = 100 * 1024 * 1024,
) -> dict[str, Any]:
    """Create an atomic ZIP from explicit run artifacts without including source code."""
    root = run_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected: dict[str, Path] = {}
    for key, relative_path in artifact_refs.items():
        if key == "bundle":
            continue
        selected[relative_path] = _safe_artifact(root, relative_path, max_file_bytes)
    for document in documents:
        relative_path = document.get("artifact_ref")
        if document.get("generation_status") == "completed" and relative_path:
            selected[relative_path] = _safe_artifact(root, relative_path, max_file_bytes)

    total_source_bytes = sum(path.stat().st_size for path in selected.values())
    if total_source_bytes > max_total_bytes:
        raise ValueError("Selected artifacts exceed the bundle size limit.")

    destination = root / "bundle.zip"
    temporary = root / "bundle.zip.tmp"
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative_path, path in sorted(selected.items()):
                archive.write(path, arcname=PurePosixPath(relative_path).as_posix())
        if temporary.stat().st_size > max_total_bytes:
            raise ValueError("Compressed bundle exceeds the bundle size limit.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    digest_builder = hashlib.sha256()
    with destination.open("rb") as bundle_file:
        for chunk in iter(lambda: bundle_file.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    return {
        "artifact_ref": "bundle.zip",
        "filename": "reverse-engineering-documentation.zip",
        "size_bytes": destination.stat().st_size,
        "sha256": digest_builder.hexdigest(),
        "included_files": sorted(selected),
    }
