"""Deterministic structural extraction and repository-level synthesis."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from app.reverse_engineering.source_access import read_source_range, resolve_source_path

ANALYZER_VERSION = "structural-v1"
SUPPORTED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _fact_id(commit_sha: str, kind: str, file_path: str, line: int, identity: str) -> str:
    value = f"{commit_sha}:{kind}:{file_path}:{line}:{identity}".encode("utf-8")
    return f"{kind}:{hashlib.sha256(value).hexdigest()[:20]}"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def redact_source_excerpt(text: str) -> str:
    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password)(\s*[:=]\s*)([\"'])(.*?)(\3)"
    )
    return secret_assignment.sub(r"\1\2\3<redacted>\3", text)


def _evidence(
    evidence_items: list[dict[str, Any]],
    root: Path,
    commit_sha: str,
    kind: str,
    file_path: str,
    start_line: int,
    end_line: int,
    identity: str,
) -> str:
    evidence_id = _fact_id(commit_sha, "evidence", file_path, start_line, f"{kind}:{identity}")
    try:
        source = read_source_range(root, file_path, start_line, min(end_line, start_line + 20), max_characters=1600)
        excerpt = redact_source_excerpt(source["text"])
        actual_end = source["end_line"]
    except (OSError, ValueError):
        excerpt = ""
        actual_end = end_line
    evidence_items.append({
        "id": evidence_id,
        "kind": kind,
        "commit_sha": commit_sha,
        "file": file_path,
        "start_line": start_line,
        "end_line": actual_end,
        "excerpt": excerpt,
        "content_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    })
    return evidence_id


def _decorator_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    try:
        return ast.unparse(target)
    except Exception:
        return ""


def _string_value(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _function_nodes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Walk a function body while keeping nested function/class behavior separate."""
    discovered: list[ast.AST] = []
    pending = list(reversed(node.body))
    while pending:
        current = pending.pop()
        discovered.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        pending.extend(reversed(list(ast.iter_child_nodes(current))))
    return discovered


class _PythonExtractor(ast.NodeVisitor):
    def __init__(self, root: Path, file_path: str, text: str, commit_sha: str, evidence: list[dict[str, Any]]):
        self.root = root
        self.file_path = file_path
        self.text = text
        self.commit_sha = commit_sha
        self.evidence = evidence
        self.scope: list[str] = []
        self.symbols: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.routes: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.config_access: list[dict[str, Any]] = []
        self.entry_points: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.behaviors: list[dict[str, Any]] = []
        self.router_prefixes: dict[str, str] = {}

    def extract(self, tree: ast.Module) -> dict[str, list[dict[str, Any]]]:
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if isinstance(value, ast.Call):
                    call_name = _decorator_name(value)
                    if call_name.endswith(("APIRouter", "Blueprint")):
                        prefix = ""
                        for keyword in value.keywords:
                            if keyword.arg in {"prefix", "url_prefix"}:
                                prefix = _string_value(keyword.value) or ""
                        for target in targets:
                            if isinstance(target, ast.Name):
                                self.router_prefixes[target.id] = prefix
                    if call_name.endswith(("FastAPI", "Flask")):
                        for target in targets:
                            if isinstance(target, ast.Name):
                                self.router_prefixes.setdefault(target.id, "")
                                evidence_id = _evidence(self.evidence, self.root, self.commit_sha, "entry_point", self.file_path, node.lineno, getattr(node, "end_lineno", node.lineno), target.id)
                                self.entry_points.append({"kind": "application", "target": target.id, "framework": call_name.split(".")[-1], "file": self.file_path, "line": node.lineno, "evidence_id": evidence_id})
        self.visit(tree)
        return {
            "symbols": self.symbols, "imports": self.imports, "routes": self.routes,
            "models": self.models, "config_access": self.config_access,
            "entry_points": self.entry_points, "calls": self.calls,
            "behaviors": self.behaviors,
        }

    def _qualified(self, name: str) -> str:
        module = self.file_path.removesuffix(".py").replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        return ".".join(filter(None, [module, *self.scope, name]))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append({"module": alias.name, "names": [alias.asname or alias.name], "level": 0, "file": self.file_path, "line": node.lineno})

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append({"module": node.module or "", "names": [alias.name for alias in node.names], "level": node.level, "file": self.file_path, "line": node.lineno})

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualified(node.name)
        bases = [ast.unparse(base) for base in node.bases]
        evidence_id = _evidence(self.evidence, self.root, self.commit_sha, "symbol", self.file_path, node.lineno, getattr(node, "end_lineno", node.lineno), qualified)
        self.symbols.append({
            "id": _fact_id(self.commit_sha, "symbol", self.file_path, node.lineno, qualified),
            "kind": "class", "name": node.name, "qualified_name": qualified,
            "signature": f"class {node.name}({', '.join(bases)})", "file": self.file_path,
            "start_line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
            "enclosing_scope": ".".join(self.scope) or None, "evidence_id": evidence_id,
        })
        model_kinds = {"BaseModel": "pydantic", "BaseSettings": "settings", "Base": "sqlalchemy", "Model": "orm", "TypedDict": "typed_dict"}
        model_kind = next((kind for base in bases for marker, kind in model_kinds.items() if base == marker or base.endswith(f".{marker}")), None)
        decorators = {_decorator_name(item) for item in node.decorator_list}
        if "dataclass" in decorators or any(item.endswith(".dataclass") for item in decorators):
            model_kind = "dataclass"
        if model_kind:
            fields = []
            table_name = None
            for child in node.body:
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    default = ast.unparse(child.value) if child.value else None
                    fields.append({"name": child.target.id, "type": ast.unparse(child.annotation), "required": child.value is None, "default": default})
                    if model_kind == "settings":
                        self.config_access.append({"key": child.target.id, "accessor": qualified, "file": self.file_path, "line": child.lineno})
                elif isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == "__tablename__":
                            table_name = _string_value(child.value)
                        elif isinstance(target, ast.Name) and isinstance(child.value, ast.Call):
                            call_name = _decorator_name(child.value)
                            if call_name.endswith(("Column", "mapped_column")):
                                field_type = ast.unparse(child.value.args[0]) if child.value.args else None
                                fields.append({"name": target.id, "type": field_type, "required": None, "default": ast.unparse(child.value)})
            model_evidence = _evidence(self.evidence, self.root, self.commit_sha, "model", self.file_path, node.lineno, getattr(node, "end_lineno", node.lineno), qualified)
            self.models.append({"id": _fact_id(self.commit_sha, "model", self.file_path, node.lineno, qualified), "name": node.name, "qualified_name": qualified, "model_kind": model_kind, "table_name": table_name, "fields": fields, "file": self.file_path, "line": node.lineno, "evidence_id": model_evidence})
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        qualified = self._qualified(node.name)
        try:
            arguments = ast.unparse(node.args)
        except Exception:
            arguments = "..."
        signature = f"{'async ' if is_async else ''}def {node.name}({arguments})"
        kind = "method" if self.scope else "function"
        evidence_id = _evidence(self.evidence, self.root, self.commit_sha, "symbol", self.file_path, node.lineno, getattr(node, "end_lineno", node.lineno), qualified)
        self.symbols.append({"id": _fact_id(self.commit_sha, "symbol", self.file_path, node.lineno, qualified), "kind": kind, "name": node.name, "qualified_name": qualified, "signature": signature, "file": self.file_path, "start_line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno), "enclosing_scope": ".".join(self.scope) or None, "evidence_id": evidence_id})
        for decorator in node.decorator_list:
            name = _decorator_name(decorator)
            method = name.rsplit(".", 1)[-1].lower()
            if isinstance(decorator, ast.Call) and method in HTTP_METHODS:
                route_path = _string_value(decorator.args[0]) if decorator.args else None
                owner = name.rsplit(".", 1)[0] if "." in name else ""
                if owner not in self.router_prefixes:
                    continue
                full_path = f"{self.router_prefixes.get(owner, '')}{route_path or ''}" or None
                dependencies = [ast.unparse(keyword.value) for keyword in decorator.keywords if keyword.arg in {"dependencies", "response_model", "status_code"}]
                route_evidence = _evidence(self.evidence, self.root, self.commit_sha, "route", self.file_path, getattr(decorator, "lineno", node.lineno), node.lineno, f"{method}:{full_path}:{qualified}")
                self.routes.append({"id": _fact_id(self.commit_sha, "route", self.file_path, node.lineno, f"{method}:{full_path}:{qualified}"), "framework": "python_web", "confidence": "high", "method": method.upper(), "path": full_path, "handler": qualified, "metadata": dependencies, "file": self.file_path, "line": node.lineno, "evidence_id": route_evidence})
        function_nodes = _function_nodes(node)
        for child in function_nodes:
            if isinstance(child, ast.Call):
                target = _decorator_name(child)
                if target and target not in {node.name, qualified}:
                    self.calls.append({"caller": qualified, "target": target, "file": self.file_path, "line": child.lineno, "resolved_symbol_id": None})
        conditions = []
        exceptions = []
        state_changes = []
        side_effects = []
        side_effect_markers = {"add", "commit", "delete", "execute", "flush", "publish", "remove", "save", "send", "update", "write", "write_text", "write_bytes"}
        for child in function_nodes:
            if isinstance(child, ast.If):
                condition = ast.unparse(child.test)
                raised = [item for item in ast.walk(child) if isinstance(item, ast.Raise)]
                conditions.append({"expression": condition, "line": child.lineno, "validation": bool(raised)})
            elif isinstance(child, ast.Raise):
                exceptions.append({"expression": ast.unparse(child.exc) if child.exc else "re-raise", "line": child.lineno})
            elif isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets: list[ast.AST]
                if isinstance(child, ast.Assign):
                    targets = child.targets
                else:
                    targets = [child.target]
                for target_node in targets:
                    if isinstance(target_node, (ast.Attribute, ast.Subscript)):
                        state_changes.append({"target": ast.unparse(target_node), "operation": type(child).__name__, "line": child.lineno})
            elif isinstance(child, ast.Call):
                target = _decorator_name(child)
                final_name = target.rsplit(".", 1)[-1]
                if final_name in side_effect_markers:
                    side_effects.append({"target": target, "line": child.lineno})
        if conditions or exceptions or state_changes or side_effects:
            behavior_evidence = _evidence(self.evidence, self.root, self.commit_sha, "behavior", self.file_path, node.lineno, getattr(node, "end_lineno", node.lineno), qualified)
            self.behaviors.append({
                "id": _fact_id(self.commit_sha, "behavior", self.file_path, node.lineno, qualified),
                "symbol_id": _fact_id(self.commit_sha, "symbol", self.file_path, node.lineno, qualified),
                "qualified_name": qualified,
                "file": self.file_path,
                "start_line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "conditions": conditions,
                "exceptions": exceptions,
                "state_changes": state_changes,
                "side_effects": side_effects,
                "evidence_id": behavior_evidence,
            })
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _decorator_name(node)
        key = None
        if name in {"os.getenv", "os.environ.get"} and node.args:
            key = _string_value(node.args[0])
        if key:
            self.config_access.append({"key": key, "accessor": name, "file": self.file_path, "line": node.lineno})
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Attribute) and _decorator_name(node.value) == "os.environ":
            key = _string_value(node.slice)
            if key:
                self.config_access.append({"key": key, "accessor": "os.environ", "file": self.file_path, "line": node.lineno})
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        try:
            condition = ast.unparse(node.test)
        except Exception:
            condition = ""
        if "__name__" in condition and "__main__" in condition:
            evidence_id = _evidence(self.evidence, self.root, self.commit_sha, "entry_point", self.file_path, node.lineno, getattr(node, "end_lineno", node.lineno), "python_main")
            self.entry_points.append({"kind": "executable", "target": "__main__", "framework": "Python", "file": self.file_path, "line": node.lineno, "evidence_id": evidence_id})
        self.generic_visit(node)


def _extract_python(root: Path, file_path: str, text: str, commit_sha: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    tree = ast.parse(text, filename=file_path, type_comments=True)
    return _PythonExtractor(root, file_path, text, commit_sha, evidence).extract(tree)


def _empty_extraction() -> dict[str, list[dict[str, Any]]]:
    return {key: [] for key in ("symbols", "imports", "routes", "models", "config_access", "entry_points", "calls", "behaviors")}


def _extract_javascript(root: Path, file_path: str, text: str, commit_sha: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    result = _empty_extraction()
    module_name = file_path.rsplit(".", 1)[0].replace("/", ".")
    for match in re.finditer(r"(?m)^\s*import\s+(.*?)\s+from\s+['\"]([^'\"]+)['\"]|^\s*(?:const|let|var)\s+\w+\s*=\s*require\(['\"]([^'\"]+)['\"]\)", text):
        imported = match.group(2) or match.group(3) or ""
        names = re.findall(r"[A-Za-z_$][\w$]*", match.group(1) or "")
        result["imports"].append({"module": imported, "names": names, "level": 0, "file": file_path, "line": _line_number(text, match.start())})
    symbol_pattern = re.compile(r"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:(async)\s+)?(?:function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)|class\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>|interface\s+([A-Za-z_$][\w$]*))")
    for match in symbol_pattern.finditer(text):
        name = match.group(2) or match.group(4) or match.group(5) or match.group(7)
        kind = "class" if match.group(4) else "interface" if match.group(7) else "function"
        args = match.group(3) if match.group(2) else match.group(6) if match.group(5) else None
        line = _line_number(text, match.start())
        qualified = f"{module_name}.{name}"
        evidence_id = _evidence(evidence, root, commit_sha, "symbol", file_path, line, line + 12, qualified)
        result["symbols"].append({"id": _fact_id(commit_sha, "symbol", file_path, line, qualified), "kind": kind, "name": name, "qualified_name": qualified, "signature": f"{name}({args or ''})" if kind == "function" else f"{kind} {name}", "file": file_path, "start_line": line, "end_line": line, "enclosing_scope": None, "evidence_id": evidence_id})
    route_pattern = re.compile(r"(?m)(?:^|\s)([A-Za-z_$][\w$]*)\.(get|post|put|patch|delete|options|head)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z_$][\w$]*)?")
    for match in route_pattern.finditer(text):
        if match.group(1).lower() not in {"app", "router", "server"} and not match.group(1).lower().endswith("router"):
            continue
        line = _line_number(text, match.start())
        method, route_path, handler = match.group(2).upper(), match.group(3), match.group(4)
        identity = f"{method}:{route_path}:{handler or 'inline'}"
        evidence_id = _evidence(evidence, root, commit_sha, "route", file_path, line, line + 5, identity)
        result["routes"].append({"id": _fact_id(commit_sha, "route", file_path, line, identity), "framework": "javascript_web", "confidence": "candidate", "method": method, "path": route_path, "handler": handler, "metadata": [], "file": file_path, "line": line, "evidence_id": evidence_id})
    controller_match = re.search(r"@Controller\s*\(\s*['\"]([^'\"]*)['\"]\s*\)", text)
    controller_prefix = controller_match.group(1) if controller_match else ""
    nest_route_pattern = re.compile(r"@(Get|Post|Put|Patch|Delete)\s*\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)\s*(?:\r?\n\s*@[^\n]+)*\r?\n\s*(?:public\s+|private\s+|protected\s+)?(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(")
    for match in nest_route_pattern.finditer(text):
        line = _line_number(text, match.start())
        route_path = "/" + "/".join(part.strip("/") for part in (controller_prefix, match.group(2) or "") if part.strip("/"))
        identity = f"{match.group(1).upper()}:{route_path}:{match.group(3)}"
        evidence_id = _evidence(evidence, root, commit_sha, "route", file_path, line, line + 8, identity)
        result["routes"].append({"id": _fact_id(commit_sha, "route", file_path, line, identity), "framework": "nestjs", "confidence": "high", "method": match.group(1).upper(), "path": route_path, "handler": f"{module_name}.{match.group(3)}", "metadata": [], "file": file_path, "line": line, "evidence_id": evidence_id})
    for match in re.finditer(r"(?ms)(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)[^\{]*\{(.*?)\}", text):
        name, body = match.group(1), match.group(2)
        line = _line_number(text, match.start())
        fields = []
        for field in re.finditer(r"(?m)^\s*([A-Za-z_$][\w$]*)(\?)?\s*:\s*([^;\n]+)", body):
            fields.append({"name": field.group(1), "type": field.group(3).strip(), "required": not bool(field.group(2)), "default": None})
        qualified = f"{module_name}.{name}"
        model_evidence = _evidence(evidence, root, commit_sha, "model", file_path, line, line + 20, qualified)
        result["models"].append({"id": _fact_id(commit_sha, "model", file_path, line, qualified), "name": name, "qualified_name": qualified, "model_kind": "typescript_interface", "table_name": None, "fields": fields, "file": file_path, "line": line, "evidence_id": model_evidence})
    for match in re.finditer(r"(?m)(?:process\.env\.([A-Z][A-Z0-9_]+)|import\.meta\.env\.([A-Z][A-Z0-9_]+))", text):
        result["config_access"].append({"key": match.group(1) or match.group(2), "accessor": "process.env" if match.group(1) else "import.meta.env", "file": file_path, "line": _line_number(text, match.start())})
    for match in re.finditer(r"(?m)(?:createRoot\s*\(|ReactDOM\.render\s*\(|(?:app|server)\.listen\s*\()", text):
        line = _line_number(text, match.start())
        target = match.group(0).strip("(")
        evidence_id = _evidence(evidence, root, commit_sha, "entry_point", file_path, line, line + 5, target)
        result["entry_points"].append({"kind": "application", "target": target, "framework": "JavaScript", "file": file_path, "line": line, "evidence_id": evidence_id})
    return result


def _extract_java(root: Path, file_path: str, text: str, commit_sha: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    result = _empty_extraction()
    package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
    package = package_match.group(1) if package_match else file_path.rsplit("/", 1)[0].replace("/", ".")
    for match in re.finditer(r"(?m)^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", text):
        module = match.group(1)
        result["imports"].append({"module": module, "names": [module.rsplit(".", 1)[-1]], "level": 0, "file": file_path, "line": _line_number(text, match.start())})
    class_pattern = re.compile(r"(?m)^\s*(?:public\s+|protected\s+|private\s+)?(?:abstract\s+|final\s+)?(class|interface|record|enum)\s+(\w+)(?:\s+extends\s+([\w<>., ]+))?")
    class_names = []
    class_start = len(text)
    for match in class_pattern.finditer(text):
        kind, name = match.group(1), match.group(2)
        class_start = min(class_start, match.start())
        class_names.append(name)
        line = _line_number(text, match.start())
        qualified = f"{package}.{name}" if package else name
        evidence_id = _evidence(evidence, root, commit_sha, "symbol", file_path, line, line + 15, qualified)
        result["symbols"].append({"id": _fact_id(commit_sha, "symbol", file_path, line, qualified), "kind": kind, "name": name, "qualified_name": qualified, "signature": match.group(0).strip(), "file": file_path, "start_line": line, "end_line": line, "enclosing_scope": None, "evidence_id": evidence_id})
        annotations = text[max(0, match.start() - 300):match.start()]
        if any(marker in annotations for marker in ("@Entity", "@Document", "@Embeddable")) or kind == "record":
            fields = []
            for field in re.finditer(r"(?m)^\s*(?:private|protected|public)\s+(?:final\s+)?([\w<>?,.]+)\s+(\w+)\s*(?:=.*)?;", text[match.end():]):
                fields.append({"name": field.group(2), "type": field.group(1), "required": None, "default": None})
            model_evidence = _evidence(evidence, root, commit_sha, "model", file_path, line, line + 20, qualified)
            result["models"].append({"id": _fact_id(commit_sha, "model", file_path, line, qualified), "name": name, "qualified_name": qualified, "model_kind": "java_entity" if "@Entity" in annotations else "java_record", "table_name": None, "fields": fields[:100], "file": file_path, "line": line, "evidence_id": model_evidence})
    method_pattern = re.compile(r"(?m)^\s*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?([\w<>?,.\[\]]+)\s+(\w+)\s*\(([^)]*)\)")
    owner = class_names[0] if class_names else PurePosixPath(file_path).stem
    for match in method_pattern.finditer(text):
        name = match.group(2)
        line = _line_number(text, match.start())
        signature = f"{match.group(1)} {name}({match.group(3)})"
        qualified = f"{package}.{owner}.{name}({match.group(3)})"
        evidence_id = _evidence(evidence, root, commit_sha, "symbol", file_path, line, line + 12, qualified)
        result["symbols"].append({"id": _fact_id(commit_sha, "symbol", file_path, line, qualified), "kind": "method", "name": name, "qualified_name": qualified, "signature": signature, "file": file_path, "start_line": line, "end_line": line, "enclosing_scope": owner, "evidence_id": evidence_id})
    class_header = text[max(0, class_start - 600):class_start]
    class_mapping = re.search(r"@RequestMapping\s*\(\s*(?:value\s*=\s*)?[\"']([^\"']*)[\"']", class_header)
    class_prefix = class_mapping.group(1) if class_mapping else ""
    route_pattern = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\s*(?:\(\s*(?:value\s*=\s*)?[\"']([^\"']*)[\"'])?")
    for match in route_pattern.finditer(text):
        if match.start() < class_start:
            continue
        line = _line_number(text, match.start())
        method = match.group(1).upper().replace("REQUEST", "ANY")
        annotation_end = text.find(")", match.start(), min(len(text), match.start() + 600))
        annotation_tail = text[match.start():(annotation_end + 1 if annotation_end >= 0 else match.end())]
        request_method = re.search(r"RequestMethod\.(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)", annotation_tail)
        if method == "ANY" and request_method:
            method = request_method.group(1)
        route_path = "/" + "/".join(part.strip("/") for part in (class_prefix, match.group(2) or "") if part.strip("/"))
        method_match = method_pattern.search(text, match.end(), min(len(text), match.end() + 800))
        handler = f"{owner}.{method_match.group(2)}" if method_match else owner
        identity = f"{method}:{route_path}:{handler}"
        evidence_id = _evidence(evidence, root, commit_sha, "route", file_path, line, line + 8, identity)
        result["routes"].append({"id": _fact_id(commit_sha, "route", file_path, line, identity), "framework": "spring", "confidence": "high", "method": method, "path": route_path, "handler": handler, "metadata": [], "file": file_path, "line": line, "evidence_id": evidence_id})
    for match in re.finditer(r"\$\{([A-Za-z0-9_.-]+)(?::[^}]*)?\}", text):
        result["config_access"].append({"key": match.group(1), "accessor": "Spring value", "file": file_path, "line": _line_number(text, match.start())})
    if "public static void main" in text:
        line = text[:text.index("public static void main")].count("\n") + 1
        evidence_id = _evidence(evidence, root, commit_sha, "entry_point", file_path, line, line + 8, f"{owner}.main")
        result["entry_points"].append({"kind": "executable", "target": f"{owner}.main", "framework": "Java", "file": file_path, "line": line, "evidence_id": evidence_id})
    return result


def _manifest_dependencies(root: Path, inventory: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dependencies: list[dict[str, Any]] = []
    entry_points: list[dict[str, Any]] = []
    for relative_path in inventory.get("manifests", []):
        try:
            path = resolve_source_path(root, relative_path)
            text = path.read_text(encoding="utf-8", errors="replace")
            name = path.name.lower()
            if name == "package.json":
                value = json.loads(text)
                for group in ("dependencies", "devDependencies", "peerDependencies"):
                    for dependency, version in (value.get(group) or {}).items():
                        dependencies.append({"name": dependency, "version": str(version), "scope": group, "source": relative_path})
                for script, command in (value.get("scripts") or {}).items():
                    if script in {"start", "dev", "serve", "build"}:
                        entry_points.append({"kind": "package_script", "target": script, "command": command, "framework": "npm", "file": relative_path, "line": 1, "evidence_id": None})
            elif name == "requirements.txt":
                for line in text.splitlines():
                    clean = line.strip()
                    if clean and not clean.startswith(("#", "-")):
                        match = re.match(r"([A-Za-z0-9_.-]+(?:\[[^\]]+\])?)\s*([<>=!~].*)?$", clean)
                        if match:
                            dependencies.append({"name": match.group(1), "version": match.group(2) or None, "scope": "runtime", "source": relative_path})
            elif name == "pyproject.toml":
                value = tomllib.loads(text)
                project = value.get("project", {})
                for item in project.get("dependencies", []) or []:
                    match = re.match(r"([A-Za-z0-9_.-]+)(.*)", item)
                    if match:
                        dependencies.append({"name": match.group(1), "version": match.group(2).strip() or None, "scope": "runtime", "source": relative_path})
                poetry = value.get("tool", {}).get("poetry", {}).get("dependencies", {})
                for dependency, version in poetry.items():
                    if dependency.lower() != "python":
                        dependencies.append({"name": dependency, "version": str(version), "scope": "runtime", "source": relative_path})
            elif name == "pom.xml":
                xml_root = ET.fromstring(text)
                for dependency in xml_root.findall(".//{*}dependency"):
                    group = dependency.findtext("{*}groupId") or ""
                    artifact = dependency.findtext("{*}artifactId") or ""
                    version = dependency.findtext("{*}version")
                    if artifact:
                        dependencies.append({"name": f"{group}:{artifact}".strip(":"), "version": version, "scope": dependency.findtext("{*}scope") or "runtime", "source": relative_path})
            elif name.startswith("build.gradle"):
                for match in re.finditer(r"(?m)^\s*(implementation|api|runtimeOnly|testImplementation)\s*[(']\s*['\"]([^'\"]+)['\"]", text):
                    dependencies.append({"name": match.group(2), "version": None, "scope": match.group(1), "source": relative_path})
        except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError, ET.ParseError):
            continue
    unique = {(item["name"], item.get("version"), item["scope"], item["source"]): item for item in dependencies}
    return sorted(unique.values(), key=lambda item: (item["name"].lower(), item["source"])), entry_points


def _deployment_entry_points(
    root: Path,
    inventory: dict[str, Any],
    commit_sha: str,
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    deployment_files = [item["path"] for item in inventory.get("files", []) if item.get("category") == "deployment"]
    for file_path in deployment_files:
        try:
            text = resolve_source_path(root, file_path).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        name = PurePosixPath(file_path).name.lower()
        patterns: list[tuple[str, re.Pattern[str]]]
        if name == "dockerfile":
            patterns = [("container_command", re.compile(r"(?mi)^\s*(CMD|ENTRYPOINT)\s+(.+)$"))]
        elif name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}:
            patterns = [("compose_command", re.compile(r"(?mi)^\s*command\s*:\s*(.+)$"))]
        elif name == "procfile":
            patterns = [("process_command", re.compile(r"(?mi)^\s*([\w-]+)\s*:\s*(.+)$"))]
        else:
            patterns = []
        for kind, pattern in patterns:
            for match in pattern.finditer(text):
                line = _line_number(text, match.start())
                if name == "procfile":
                    target, command = match.group(1), match.group(2).strip()
                elif name == "dockerfile":
                    target, command = match.group(1).upper(), match.group(2).strip()
                else:
                    target, command = kind, match.group(1).strip()
                evidence_id = _evidence(evidence, root, commit_sha, "entry_point", file_path, line, line, f"{kind}:{target}:{command}")
                entries.append({"kind": kind, "target": target, "command": command, "framework": "deployment", "file": file_path, "line": line, "evidence_id": evidence_id})
    return entries


def _component_name(file_path: str) -> str:
    parts = PurePosixPath(file_path).parts[:-1]
    if not parts:
        return "_root"
    if parts[0] in {"apps", "packages", "services"} and len(parts) > 1:
        return "/".join(parts[:2])
    if parts[0] in {"backend", "frontend", "server", "client", "api", "web"}:
        return parts[0]
    if parts[0] == "src" and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]


def _resolve_imports(facts: dict[str, Any], source_files: list[str]) -> list[dict[str, Any]]:
    file_set = set(source_files)
    python_modules = {}
    for file_path in source_files:
        if file_path.endswith(".py"):
            module = file_path[:-3].replace("/", ".")
            python_modules[module.removesuffix(".__init__")] = file_path
    interactions: Counter[tuple[str, str]] = Counter()
    for item in facts["imports"]:
        source = item["file"]
        module = item["module"]
        resolved = None
        if source.endswith(".py"):
            current_module = source[:-3].replace("/", ".").split(".")[:-1]
            if item.get("level"):
                keep = max(0, len(current_module) - int(item["level"]) + 1)
                module = ".".join([*current_module[:keep], *([module] if module else [])])
            candidates = [module, *[".".join(module.split(".")[:index]) for index in range(len(module.split(".")) - 1, 0, -1)]]
            resolved = next((python_modules[candidate] for candidate in candidates if candidate in python_modules), None)
        elif source.endswith((".js", ".jsx", ".ts", ".tsx")) and module.startswith("."):
            base = (PurePosixPath(source).parent / module).as_posix()
            candidates = [base + ext for ext in (".ts", ".tsx", ".js", ".jsx")] + [f"{base}/index{ext}" for ext in (".ts", ".tsx", ".js", ".jsx")]
            resolved = next((candidate for candidate in candidates if candidate in file_set), None)
        elif source.endswith(".java"):
            candidate = module.replace(".", "/").removesuffix("/*") + ".java"
            resolved = next((path for path in source_files if path.endswith(candidate)), None)
        item["resolved_file"] = resolved
        if resolved:
            source_component, target_component = _component_name(source), _component_name(resolved)
            if source_component != target_component:
                interactions[(source_component, target_component)] += 1
    return [{"source": source, "target": target, "type": "imports", "evidence_count": count} for (source, target), count in sorted(interactions.items())]


def analyze_repository(repository_root: Path, inventory: dict[str, Any], commit_sha: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Extract source facts, evidence, coverage, and a reusable repository synthesis."""
    root = repository_root.resolve()
    evidence_items: list[dict[str, Any]] = []
    facts = _empty_extraction()
    source_entries = [item for item in inventory.get("files", []) if item.get("category") in {"source", "test"}]
    parsed_files: list[str] = []
    unsupported_files: list[dict[str, str]] = []
    failed_files: list[dict[str, str]] = []

    for item in source_entries:
        file_path = item["path"]
        extension = item.get("extension", "").lower()
        if extension not in SUPPORTED_EXTENSIONS:
            unsupported_files.append({"path": file_path, "reason": f"No structural extractor for {item.get('language') or extension}."})
            continue
        try:
            text = resolve_source_path(root, file_path).read_text(encoding="utf-8", errors="replace")
            if extension == ".py":
                extracted = _extract_python(root, file_path, text, commit_sha, evidence_items)
            elif extension in {".js", ".jsx", ".ts", ".tsx"}:
                extracted = _extract_javascript(root, file_path, text, commit_sha, evidence_items)
            else:
                extracted = _extract_java(root, file_path, text, commit_sha, evidence_items)
            for key in facts:
                facts[key].extend(extracted[key])
            parsed_files.append(file_path)
        except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
            failed_files.append({"path": file_path, "reason": f"{type(exc).__name__}: {exc}"})

    dependencies, manifest_entries = _manifest_dependencies(root, inventory)
    facts["entry_points"].extend(manifest_entries)
    facts["entry_points"].extend(_deployment_entry_points(root, inventory, commit_sha, evidence_items))
    source_files = [item["path"] for item in source_entries]
    interactions = _resolve_imports(facts, source_files)
    symbol_ids = {item["qualified_name"]: item["id"] for item in facts["symbols"]}
    short_symbol_ids = defaultdict(list)
    for item in facts["symbols"]:
        short_symbol_ids[item["name"]].append(item["id"])
    for call in facts["calls"]:
        target = call["target"]
        call["resolved_symbol_id"] = symbol_ids.get(target)
        if not call["resolved_symbol_id"] and len(short_symbol_ids.get(target, [])) == 1:
            call["resolved_symbol_id"] = short_symbol_ids[target][0]

    components_map: dict[str, dict[str, Any]] = {}
    files_by_component: defaultdict[str, list[str]] = defaultdict(list)
    for item in source_entries:
        files_by_component[_component_name(item["path"])].append(item["path"])
    for component, component_files in sorted(files_by_component.items()):
        languages = Counter(item.get("language") for item in source_entries if item["path"] in component_files and item.get("language"))
        components_map[component] = {
            "id": f"component:{component}", "name": component, "files": sorted(component_files),
            "file_count": len(component_files), "languages": dict(languages),
            "symbol_count": sum(1 for item in facts["symbols"] if item["file"] in component_files),
            "route_count": sum(1 for item in facts["routes"] if item["file"] in component_files),
            "model_count": sum(1 for item in facts["models"] if item["file"] in component_files),
        }

    eligible_count = len(source_entries)
    parsed_count = len(parsed_files)
    coverage_percent = round((parsed_count / eligible_count * 100) if eligible_count else 100.0, 1)
    coverage_status = "complete" if not unsupported_files and not failed_files and not inventory.get("limitations") else "partial"
    coverage = {
        "status": coverage_status,
        "eligible_files": eligible_count,
        "parsed_files": parsed_count,
        "unsupported_files": len(unsupported_files),
        "failed_files": len(failed_files),
        "coverage_percent": coverage_percent,
        "unsupported": unsupported_files,
        "failures": failed_files,
        "inventory_limitations": inventory.get("limitations", []),
        "methods": {
            "Python": "standard_library_ast",
            "JavaScript": "bounded_structural_patterns",
            "TypeScript": "bounded_structural_patterns",
            "Java": "bounded_structural_patterns",
        },
    }
    facts.update({
        "schema_version": 1, "analyzer_version": ANALYZER_VERSION, "commit_sha": commit_sha,
        "dependencies": dependencies, "coverage": coverage,
    })
    architecture_style = "multi-component application" if len(components_map) > 1 else "single-component application"
    if facts["routes"] and any(item["framework"] == "JavaScript" for item in facts["entry_points"]):
        architecture_style = "client-server web application"
    analysis = {
        "schema_version": 1,
        "analyzer_version": ANALYZER_VERSION,
        "commit_sha": commit_sha,
        "architecture": {
            "style": architecture_style,
            "components": list(components_map.values()),
            "interactions": interactions,
            "entry_points": facts["entry_points"],
        },
        "summary": {
            "components": len(components_map), "symbols": len(facts["symbols"]),
            "routes": len(facts["routes"]), "models": len(facts["models"]),
            "dependencies": len(dependencies), "config_keys": len({item['key'] for item in facts['config_access']}),
            "behavior_records": len(facts["behaviors"]),
        },
        "coverage": coverage,
        "uncertainties": [
            *[f"Unsupported structural analysis: {item['path']}" for item in unsupported_files[:100]],
            *[f"Parser failure: {item['path']} ({item['reason']})" for item in failed_files[:100]],
            *inventory.get("limitations", []),
        ],
    }
    evidence_artifact = {"schema_version": 1, "commit_sha": commit_sha, "items": evidence_items}
    return facts, analysis, evidence_artifact
