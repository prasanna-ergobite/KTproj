"""Safe repository intake and deterministic source inventory."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

SOURCE_LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".kt": "Kotlin",
    ".kts": "Kotlin", ".go": "Go", ".rs": "Rust", ".cs": "C#", ".fs": "F#",
    ".php": "PHP", ".rb": "Ruby", ".scala": "Scala", ".c": "C", ".h": "C/C++",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".swift": "Swift",
    ".dart": "Dart", ".vue": "Vue", ".svelte": "Svelte", ".sql": "SQL",
    ".sh": "Shell", ".ps1": "PowerShell",
}

TEXT_EXTENSIONS = set(SOURCE_LANGUAGES) | {
    ".md", ".mdx", ".rst", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".xml", ".properties", ".gradle", ".html", ".css",
    ".scss", ".less", ".graphql", ".gql", ".proto", ".env", ".example",
}

IGNORED_DIRECTORIES = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".next", ".nuxt", ".cache",
    "node_modules", "vendor", "dist", "build", "out", "target", ".gradle",
    ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    "coverage", "htmlcov", "bin", "obj",
}

SENSITIVE_NAMES = {
    ".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "service-account.json",
}

MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "requirements.txt", "poetry.lock", "pipfile",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "go.mod",
    "cargo.toml", "composer.json", "gemfile", "mix.exs", "pubspec.yaml",
}

DEPLOYMENT_NAMES = {
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml",
    "compose.yaml", "helmfile.yaml", "procfile", "vercel.json", "netlify.toml",
}

FRAMEWORK_MARKERS = {
    "FastAPI": ("fastapi",), "Django": ("django",), "Flask": ("flask",),
    "React": ('"react"', "'react'"), "Next.js": ("next",), "Vue": ('"vue"',),
    "Angular": ("@angular/core",), "Express": ("express",), "NestJS": ("@nestjs/core",),
    "Spring Boot": ("spring-boot",), "Hibernate": ("hibernate",),
    ".NET": ("microsoft.net.sdk",), "Laravel": ("laravel/framework",),
    "Ruby on Rails": ("rails",),
}


def validate_remote_repository_url(repo_url: str, allowed_hosts: Iterable[str] = ()) -> str:
    """Validate a credential-free HTTPS Git URL and return its normalized value."""
    candidate = repo_url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Use a complete HTTPS repository URL.")
    if parsed.username or parsed.password:
        raise ValueError("Repository URLs containing credentials are not accepted.")
    if parsed.query or parsed.fragment:
        raise ValueError("Repository URLs cannot contain query parameters or fragments.")
    hosts = {host.strip().lower() for host in allowed_hosts if host.strip()}
    if hosts and parsed.hostname.lower() not in hosts:
        raise ValueError(f"Repository host '{parsed.hostname}' is not enabled for this demo.")
    return candidate.rstrip("/")


def validate_git_ref(ref: str | None) -> str | None:
    """Reject option-like or malformed branch/tag values before invoking Git."""
    if not ref or not ref.strip():
        return None
    candidate = ref.strip()
    if (
        candidate.startswith("-")
        or ".." in candidate
        or candidate.endswith(("/", ".", ".lock"))
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", candidate)
    ):
        raise ValueError("The requested ref is not a valid branch, tag, or commit name.")
    return candidate


def sanitize_repository_url(repo_url: str) -> str:
    """Remove credentials defensively before a URL is persisted or logged."""
    return re.sub(r"(https?://)[^/@]+@", r"\1", repo_url.strip())


def clone_repository(repo_url: str, destination: Path, ref: str | None = None) -> dict[str, str]:
    """Clone source without running repository build, test, or application commands."""
    from git import Repo

    destination.parent.mkdir(parents=True, exist_ok=True)
    if ref:
        repo = Repo.init(destination)
        repo.create_remote("origin", repo_url)
        repo.git.fetch("--depth=1", "--no-tags", "origin", ref)
        repo.git.checkout("--detach", "FETCH_HEAD")
        resolved_ref = ref
    else:
        repo = Repo.clone_from(repo_url, destination, depth=1, no_tags=True)
        try:
            resolved_ref = repo.active_branch.name
        except TypeError:
            resolved_ref = repo.head.commit.hexsha
    commit = repo.head.commit.hexsha
    return {"commit_sha": commit, "resolved_ref": resolved_ref}


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            sample = stream.read(4096)
    except OSError:
        return True
    return b"\x00" in sample


def _is_sensitive(relative_path: str) -> bool:
    name = Path(relative_path).name.lower()
    return name in SENSITIVE_NAMES or name.startswith(".env.") or name.endswith((".pem", ".key", ".p12", ".pfx"))


def _category(relative_path: str, suffix: str) -> str:
    lowered = relative_path.lower()
    name = Path(relative_path).name.lower()
    parts = set(Path(lowered).parts)
    if name in MANIFEST_NAMES or name.endswith((".csproj", ".fsproj", ".sln")):
        return "manifest"
    if name in DEPLOYMENT_NAMES or "k8s" in parts or "kubernetes" in parts or "helm" in parts:
        return "deployment"
    if "migration" in parts or "migrations" in parts:
        return "migration"
    if "test" in parts or "tests" in parts or "spec" in parts or re.search(r"(^|[._-])(test|spec)([._-]|$)", name):
        return "test"
    if suffix in {".md", ".mdx", ".rst", ".txt"} or "docs" in parts or "documentation" in parts:
        return "documentation"
    if suffix in SOURCE_LANGUAGES:
        return "source"
    if suffix in {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".properties"}:
        return "configuration"
    return "other_text"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _detect_frameworks(manifest_text: str) -> list[str]:
    lowered = manifest_text.lower()
    return sorted(name for name, markers in FRAMEWORK_MARKERS.items() if any(marker in lowered for marker in markers))


def build_source_inventory(
    repository_root: Path,
    *,
    max_files: int = 20_000,
    max_file_bytes: int = 2 * 1024 * 1024,
    max_included_bytes: int = 50 * 1024 * 1024,
) -> dict[str, Any]:
    """Inventory a checkout deterministically without importing or executing its source."""
    root = repository_root.resolve()
    files: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    languages: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    manifests: list[str] = []
    manifest_fragments: list[str] = []
    total_files = total_bytes = included_bytes = 0
    limitations: list[str] = []

    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for directory_name in sorted(directory_names):
            directory = current_path / directory_name
            relative = directory.relative_to(root).as_posix()
            if directory_name.lower() in IGNORED_DIRECTORIES:
                exclusions.append({"path": relative + "/", "reason": "generated_or_vendor_directory"})
            elif directory.is_symlink():
                exclusions.append({"path": relative + "/", "reason": "symbolic_link"})
            else:
                kept_dirs.append(directory_name)
        directory_names[:] = kept_dirs

        for file_name in sorted(file_names):
            path = current_path / file_name
            relative = path.relative_to(root).as_posix()
            total_files += 1
            try:
                size = path.stat().st_size
            except OSError:
                exclusions.append({"path": relative, "reason": "unreadable"})
                continue
            total_bytes += size
            if total_files > max_files:
                exclusions.append({"path": relative, "reason": "repository_file_limit"})
                if "Repository file limit reached; the inventory is partial." not in limitations:
                    limitations.append("Repository file limit reached; the inventory is partial.")
                continue
            if path.is_symlink():
                exclusions.append({"path": relative, "reason": "symbolic_link"})
                continue
            if _is_sensitive(relative):
                exclusions.append({"path": relative, "reason": "sensitive_file"})
                continue
            suffix = path.suffix.lower()
            name = path.name.lower()
            recognized_text = suffix in TEXT_EXTENSIONS or name in MANIFEST_NAMES or name in DEPLOYMENT_NAMES or name in {"makefile", "gemfile"}
            if not recognized_text:
                exclusions.append({"path": relative, "reason": "binary_or_unsupported"})
                continue
            if size > max_file_bytes:
                exclusions.append({"path": relative, "reason": "file_size_limit", "size_bytes": size})
                continue
            if _looks_binary(path):
                exclusions.append({"path": relative, "reason": "binary_or_unsupported"})
                continue
            if included_bytes + size > max_included_bytes:
                exclusions.append({"path": relative, "reason": "repository_size_limit", "size_bytes": size})
                if "Repository source-size limit reached; the inventory is partial." not in limitations:
                    limitations.append("Repository source-size limit reached; the inventory is partial.")
                continue

            language = SOURCE_LANGUAGES.get(suffix)
            category = _category(relative, suffix)
            item = {
                "path": relative,
                "size_bytes": size,
                "extension": suffix,
                "category": category,
                "language": language,
                "sha256": _sha256(path),
            }
            files.append(item)
            included_bytes += size
            categories[category] += 1
            if language:
                languages[language] += 1
            if category == "manifest":
                manifests.append(relative)
                try:
                    manifest_fragments.append(path.read_text(encoding="utf-8", errors="replace")[:262_144])
                except OSError:
                    pass

    files.sort(key=lambda item: item["path"])
    exclusions.sort(key=lambda item: item["path"])
    return {
        "repository_root": ".",
        "total_files": total_files,
        "included_files": len(files),
        "excluded_files": len(exclusions),
        "total_bytes": total_bytes,
        "included_bytes": included_bytes,
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        "categories": dict(sorted(categories.items())),
        "manifests": sorted(manifests),
        "frameworks": _detect_frameworks("\n".join(manifest_fragments)),
        "limitations": limitations,
        "files": files,
        "exclusions": exclusions,
    }


def write_json_artifact(path: Path, value: dict[str, Any]) -> None:
    """Write an artifact atomically so readers never observe partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)

