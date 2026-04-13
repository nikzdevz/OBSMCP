"""
Code Atlas — Multi-language code structure extractor for obsmcp.

Scans a codebase and produces a structured Markdown document that documents
every file, function, class, feature, and cross-reference. Designed for use
as a project map that lets new AI agents understand the codebase instantly.

Supported languages:
  Python (.py), JavaScript/TypeScript (.js/.ts/.jsx/.tsx),
  Rust (.rs), Java (.java), C/C++ (.c/.cpp/.h/.hpp),
  Go (.go), Shell (.sh/.bat), HTML (.html),
  CSS (.css), JSON (.json), YAML (.yml/.yaml),
  TOML (.toml), Markdown (.md), Plain text (.txt),
  C# (.cs), PHP (.php), Ruby (.rb), SQL (.sql)
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import utc_now


# ---------------------------------------------------------------------------
# Exclusion patterns — folders and files to skip entirely
# ---------------------------------------------------------------------------

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
    ".tox",
    ".coverage",
    "htmlcov",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".parcel-cache",
    ".cache",
    "tmp",
    "temp",
    ".tmp",
    ".temp",
    ".svn",
    ".hg",
    ".idea",
    ".vscode",
    ".vs",
    "target",  # Rust build output
    "bin",
    "obj",  # .NET
    "packages",  # NuGet
    ".gradle",
    ".cocoapods",
}

EXCLUDE_FILES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".gitignore",
    ".gitattributes",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    " Pipfile.lock",
    "requirements.txt",
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
}

EXCLUDE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".webp",
    ".mp4",
    ".mp3",
    ".wav",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".dat",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".ico",
}

# ---------------------------------------------------------------------------
# Language definitions
# ---------------------------------------------------------------------------

LANGUAGE_MAP: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript (JSX)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (TSX)",
    ".rs": "Rust",
    ".java": "Java",
    ".c": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".h": "C/C++ Header",
    ".hpp": "C++ Header",
    ".go": "Go",
    ".sh": "Shell",
    ".bat": "Batch",
    ".ps1": "PowerShell",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
    ".txt": "Plain Text",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".sql": "SQL",
    ".xml": "XML",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".kts": "Kotlin Script",
    ".scala": "Scala",
    ".r": "R",
    ".lua": "Lua",
    ".pl": "Perl",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".erl": "Erlang",
    ".jl": "Julia",
    ".dart": "Dart",
    ".groovy": "Groovy",
    ".gradle": "Gradle",
    ".tf": "Terraform",
    ".dockerfile": "Dockerfile",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FunctionInfo:
    name: str
    line_number: int
    signature: str
    docstring: str
    visibility: str = "public"  # public, private, protected

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "line": self.line_number,
            "signature": self.signature,
            "docstring": self.docstring,
            "visibility": self.visibility,
        }


@dataclass
class ClassInfo:
    name: str
    line_number: int
    docstring: str
    bases: list[str]
    methods: list[FunctionInfo]
    visibility: str = "public"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "line": self.line_number,
            "docstring": self.docstring,
            "bases": self.bases,
            "methods": [m.to_dict() for m in self.methods],
            "visibility": self.visibility,
        }


@dataclass
class ImportInfo:
    module: str
    names: list[str]
    line_number: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "names": self.names,
            "line": self.line_number,
        }


@dataclass
class FileInfo:
    path: Path
    relative_path: str
    language: str
    total_lines: int
    code_lines: int
    blank_lines: int
    comment_lines: int
    docstring: str
    imports: list[ImportInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    # For non-class-based languages
    raw_functions: list[FunctionInfo] = field(default_factory=list)
    # For config/markup files
    structure: dict[str, Any] = field(default_factory=dict)
    # File-level notes (e.g., Django views, React components)
    feature_tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "language": self.language,
            "lines": self.total_lines,
            "code_lines": self.code_lines,
            "blank_lines": self.blank_lines,
            "comment_lines": self.comment_lines,
            "docstring": self.docstring,
            "imports": [i.to_dict() for i in self.imports],
            "classes": [c.to_dict() for c in self.classes],
            "functions": [f.to_dict() for f in self.functions],
            "raw_functions": [f.to_dict() for f in self.raw_functions],
            "structure": self.structure,
            "feature_tags": self.feature_tags,
        }


@dataclass
class AtlasResult:
    project_name: str
    project_path: str
    generated_at: str
    total_files: int
    total_lines: int
    languages: dict[str, int]  # language -> file count
    files: list[FileInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "project_path": self.project_path,
            "generated_at": self.generated_at,
            "total_files": self.total_files,
            "total_lines": self.total_lines,
            "languages": self.languages,
            "files": [f.to_dict() for f in self.files],
        }


# ---------------------------------------------------------------------------
# Python AST scanner
# ---------------------------------------------------------------------------

class PythonScanner:
    """Extracts structured information from Python source using AST."""

    @staticmethod
    def _get_docstring(node: ast.AST) -> str:
        doc = ast.get_docstring(node)
        return doc or ""

    @staticmethod
    def _get_decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        return [ast.unparse(d) if hasattr(ast, "unparse") else "" for d in getattr(node, "decorator_list", [])]

    @staticmethod
    def _is_private(name: str) -> bool:
        return name.startswith("_") and not name.startswith("__")

    @staticmethod
    def _is_dunder(name: str) -> bool:
        return name.startswith("__") and name.endswith("__")

    @staticmethod
    def _get_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        try:
            sig = ast.get_source_segment
            # Fallback: build from ast node
            args = [a.arg for a in node.args.args]
            vararg = f"*{node.args.vararg.arg}" if node.args.vararg else ""
            kwarg = f"**{node.args.kwarg.arg}" if node.args.kwarg else ""
            defaults = node.args.defaults
            params = args[: len(args) - len(defaults)] if defaults else args
            defaults_start = len(params)
            params_with_defaults = []
            for i, arg in enumerate(args):
                if i >= defaults_start:
                    default = defaults[i - defaults_start]
                    try:
                        default_str = ast.unparse(default)
                    except Exception:
                        default_str = "?"
                    params_with_defaults.append(f"{arg}={default_str}")
                else:
                    params_with_defaults.append(arg)
            params_str = ", ".join(params_with_defaults)
            if vararg:
                params_str = f"{params_str}, {vararg}" if params_str else vararg
            if kwarg:
                params_str = f"{params_str}, {kwarg}" if params_str else kwarg
            async_kw = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
            return f"{async_kw}def {node.name}({params_str})"
        except Exception:
            return f"def {node.name}(...)"

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="Python",
            total_lines=0,
            code_lines=0,
            blank_lines=0,
            comment_lines=0,
            docstring="",
        )

        lines = content.splitlines()
        info.total_lines = len(lines)
        info.blank_lines = sum(1 for l in lines if not l.strip())
        info.comment_lines = sum(
            1 for l in lines if l.strip().startswith("#") or l.strip().startswith('"""') or l.strip().startswith("'''")
        )

        info.code_lines = info.total_lines - info.blank_lines

        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError:
            return info

        # Module docstring
        if (
            tree.body
            and isinstance(tree.body[0], (ast.Expr, ast.Constant))
            and isinstance(tree.body[0].value, (ast.Str, ast.Constant))
        ):
            if isinstance(tree.body[0].value, ast.Str):
                info.docstring = tree.body[0].value.s or ""
            elif isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str):
                info.docstring = tree.body[0].value.value or ""

        # Imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module = ""
                names = []
                for alias in node.names:
                    names.append(alias.asname or alias.name)
                    module = alias.name.split(".")[0]
                info.imports.append(ImportInfo(module=module, names=names, line_number=node.lineno or 0))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [a.asname or a.name for a in node.names]
                info.imports.append(ImportInfo(module=module, names=names, line_number=node.lineno or 0))

        # Classes and their methods
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = []
                for b in node.bases:
                    try:
                        bases.append(ast.unparse(b))
                    except Exception:
                        bases.append("")
                decorators = self._get_decorator_names(node)
                visibility = "private" if self._is_private(node.name) else "public"

                methods = []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        m = FunctionInfo(
                            name=item.name,
                            line_number=item.lineno or 0,
                            signature=self._get_signature(item),
                            docstring=self._get_docstring(item),
                            visibility="private" if self._is_private(item.name) else "public",
                        )
                        methods.append(m)

                cls = ClassInfo(
                    name=node.name,
                    line_number=node.lineno or 0,
                    docstring=self._get_docstring(node),
                    bases=bases,
                    methods=methods,
                    visibility=visibility,
                )
                info.classes.append(cls)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not self._is_dunder(node.name):
                    fn = FunctionInfo(
                        name=node.name,
                        line_number=node.lineno or 0,
                        signature=self._get_signature(node),
                        docstring=self._get_docstring(node),
                        visibility="private" if self._is_private(node.name) else "public",
                    )
                    info.functions.append(fn)

        # Feature tagging
        info.feature_tags = self._detect_features(info)

        return info

    def _detect_features(self, info: FileInfo) -> list[str]:
        tags = []
        imports = {imp.module: imp.names for imp in info.imports}

        # Web frameworks
        if "flask" in imports or any("flask" in str(v) for v in imports.values()):
            tags.append("Flask")
        if "fastapi" in imports or any("fastapi" in str(v) for v in imports.values()):
            tags.append("FastAPI")
        if "django" in imports or any("django" in str(v) for v in imports.values()):
            tags.append("Django")
        if "pytest" in imports or any("pytest" in str(v) for v in imports.values()):
            tags.append("pytest")
        if "unittest" in imports or any("unittest" in str(v) for v in imports.values()):
            tags.append("unittest")
        if "pydantic" in imports or any("pydantic" in str(v) for v in imports.values()):
            tags.append("Pydantic")
        if "sqlalchemy" in imports or any("sqlalchemy" in str(v) for v in imports.values()):
            tags.append("SQLAlchemy")
        if "requests" in imports or any("requests" in str(v) for v in imports.values()):
            tags.append("HTTP Client")
        if "aiohttp" in imports or "httpx" in imports:
            tags.append("Async HTTP")
        if "redis" in imports:
            tags.append("Redis")
        if "celery" in imports:
            tags.append("Celery")
        if "numpy" in imports or "pandas" in imports:
            tags.append("Data Science")

        return tags


# ---------------------------------------------------------------------------
# JavaScript / TypeScript scanner
# ---------------------------------------------------------------------------

class JavaScriptScanner:
    """Extracts structured information from JavaScript/TypeScript using regex."""

    # Named function: function myFunc() {
    JS_FUNC_NAMED = re.compile(r"function\s+(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE)
    # Export named function: export function myFunc() {
    JS_FUNC_EXPORT = re.compile(r"export\s+function\s+(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE)
    # Arrow function with name: const myFunc = (...) => {
    JS_ARROW_NAMED = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*\{", re.MULTILINE)
    # Arrow function with name: const myFunc = async (...) => {
    JS_ARROW_ASYNC = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*async\s+\([^)]*\)\s*=>\s*\{", re.MULTILINE)
    # Method shorthand: myMethod() {
    JS_METHOD = re.compile(r"^\s*(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE)
    # Class definition
    JS_CLASS_RE = re.compile(
        r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"
        r"(?:\s+extends\s+(\w+))?"
        r"(?:\s+implements\s+[\w, ]+)?\s*\{",
        re.MULTILINE,
    )
    JS_IMPORT_RE = re.compile(r"import\s+(?:{[^}]+}|(\w+)|(\*\s+as\s+\w+)|\*)\s*from\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
    TS_DECOR_RE = re.compile(r"@(\w+)", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        ext = file_path.suffix
        language = LANGUAGE_MAP.get(ext, "JavaScript")
        lines = content.splitlines()
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language=language,
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip() and not l.strip().startswith("//")]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=sum(1 for l in lines if l.strip().startswith("//")),
            docstring="",
        )

        # Extract module docstring (first block comment)
        doc_match = re.search(r"/\*\*[\s\S]*?\*/", content)
        if doc_match:
            info.docstring = re.sub(r"[\s/*#]+", " ", doc_match.group()).strip()

        # Imports
        for match in self.JS_IMPORT_RE.finditer(content):
            module = match.group(3) or ""
            names = []
            if match.group(1):
                names.append(match.group(1))
            if match.group(2):
                names.extend(match.group(2).split())
            info.imports.append(ImportInfo(module=module.split("/")[-1], names=names, line_number=content[: match.start()].count("\n") + 1))

        # Classes
        for match in self.JS_CLASS_RE.finditer(content):
            name = match.group(1)
            bases = [match.group(2)] if match.group(2) else []
            line = content[: match.start()].count("\n") + 1
            # Find methods inside the class
            methods = self._extract_js_methods(content, match.end())
            info.classes.append(
                ClassInfo(
                    name=name,
                    line_number=line,
                    docstring="",
                    bases=bases,
                    methods=methods,
                    visibility="private" if name.startswith("_") else "public",
                )
            )

        # Functions: export function name() {
        for match in self.JS_FUNC_EXPORT.finditer(content):
            name = match.group(1)
            if not name.startswith("_"):
                line = content[: match.start()].count("\n") + 1
                info.raw_functions.append(
                    FunctionInfo(
                        name=name,
                        line_number=line,
                        signature=f"export function {name}(...)",
                        docstring="",
                        visibility="private" if name.startswith("_") else "public",
                    )
                )

        # Functions: function name() {
        for match in self.JS_FUNC_NAMED.finditer(content):
            name = match.group(1)
            if not name.startswith("_") and not any(content[max(0, match.start() - 20) : match.start()].endswith(k) for k in ["export", "async"]):
                line = content[: match.start()].count("\n") + 1
                info.raw_functions.append(
                    FunctionInfo(
                        name=name,
                        line_number=line,
                        signature=f"function {name}(...)",
                        docstring="",
                        visibility="private" if name.startswith("_") else "public",
                    )
                )

        # Arrow functions: const name = (...) => {
        for match in self.JS_ARROW_NAMED.finditer(content):
            name = match.group(1)
            if not name.startswith("_"):
                line = content[: match.start()].count("\n") + 1
                info.raw_functions.append(
                    FunctionInfo(
                        name=name,
                        line_number=line,
                        signature=f"const {name} = (...) => {{...}}",
                        docstring="",
                        visibility="private" if name.startswith("_") else "public",
                    )
                )

        # Async arrow functions: const name = async (...) => {
        for match in self.JS_ARROW_ASYNC.finditer(content):
            name = match.group(1)
            if not name.startswith("_"):
                line = content[: match.start()].count("\n") + 1
                info.raw_functions.append(
                    FunctionInfo(
                        name=name,
                        line_number=line,
                        signature=f"const {name} = async (...) => {{...}}",
                        docstring="",
                        visibility="private" if name.startswith("_") else "public",
                    )
                )

        info.feature_tags = self._detect_features(info)
        return info

    def _extract_js_methods(self, content: str, start: int) -> list[FunctionInfo]:
        methods = []
        method_re = re.compile(r"(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE)
        depth = 1
        pos = start
        while pos < len(content) and depth > 0:
            char = content[pos]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            pos += 1
        class_body = content[start:pos]
        for m in method_re.finditer(class_body):
            name = m.group(1)
            if name not in {"if", "else", "for", "while", "switch", "try", "catch"}:
                line = class_body[: m.start()].count("\n") + start
                methods.append(
                    FunctionInfo(
                        name=name,
                        line_number=line,
                        signature=f"{name}(...)",
                        docstring="",
                        visibility="private" if name.startswith("_") else "public",
                    )
                )
        return methods

    def _detect_features(self, info: FileInfo) -> list[str]:
        tags = []
        content = str(info.relative_path)
        imports_str = " ".join(" ".join(i.names) for i in info.imports)

        if "react" in imports_str.lower() or "React" in imports_str:
            tags.append("React")
        if "vue" in imports_str.lower():
            tags.append("Vue")
        if "angular" in imports_str.lower():
            tags.append("Angular")
        if "next" in imports_str.lower() or "next" in content.lower():
            tags.append("Next.js")
        if "nuxt" in imports_str.lower():
            tags.append("Nuxt")
        if "express" in imports_str.lower():
            tags.append("Express")
        if "jest" in imports_str.lower():
            tags.append("Jest")
        if "vitest" in imports_str.lower():
            tags.append("Vitest")
        if "axios" in imports_str.lower():
            tags.append("Axios")
        if "tailwind" in imports_str.lower():
            tags.append("Tailwind CSS")
        if "node" in imports_str.lower():
            tags.append("Node.js")
        if "redux" in imports_str.lower() or "zustand" in imports_str.lower():
            tags.append("State Management")
        if "@tanstack" in imports_str or "tanstack" in imports_str:
            tags.append("TanStack")

        return tags


# ---------------------------------------------------------------------------
# Rust scanner
# ---------------------------------------------------------------------------

class RustScanner:
    """Extracts structured information from Rust source files."""

    FN_RE = re.compile(r"(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)
    IMPL_RE = re.compile(r"(?:pub\s+)?impl(?:<[^>]+>)?\s+(?:\w+::)?(\w+)", re.MULTILINE)
    STRUCT_RE = re.compile(r"(?:pub\s+)?struct\s+(\w+)(?:<[^>]+>)?", re.MULTILINE)
    ENUM_RE = re.compile(r"(?:pub\s+)?enum\s+(\w+)", re.MULTILINE)
    MOD_RE = re.compile(r"(?:pub\s+)?mod\s+(\w+)", re.MULTILINE)
    USE_RE = re.compile(r"use\s+([\w:]+)", re.MULTILINE)
    PUB_RE = re.compile(r"(pub)\s+", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        lines = content.splitlines()
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="Rust",
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip() and not l.strip().startswith("//")]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=sum(1 for l in lines if l.strip().startswith("//")),
            docstring="",
        )

        # Module docstring
        doc_lines = []
        for line in lines[:10]:
            stripped = line.strip()
            if stripped.startswith("///") or stripped.startswith("/*"):
                doc_lines.append(strip(stripped, "#/*"))
            elif stripped.startswith("//!") or stripped.startswith("/*!"):
                doc_lines.append(strip(stripped, "//!/*!"))
            elif doc_lines:
                break
        if doc_lines:
            info.docstring = " ".join(doc_lines)

        # Imports
        for match in self.USE_RE.finditer(content):
            module = match.group(1).split("::")[0]
            line = content[: match.start()].count("\n") + 1
            info.imports.append(ImportInfo(module=module, names=[match.group(1)], line_number=line))

        # Functions
        for match in self.FN_RE.finditer(content):
            name = match.group(1)
            if not name.startswith("_") and name not in {"if", "match", "while"}:
                line = content[: match.start()].count("\n") + 1
                info.raw_functions.append(
                    FunctionInfo(name=name, line_number=line, signature=f"fn {name}(...)", docstring="", visibility="private" if name.startswith("_") else "public")
                )

        # Structs
        info.feature_tags.append("Struct")
        return info


# ---------------------------------------------------------------------------
# Java / C / C++ scanner
# ---------------------------------------------------------------------------

class JavaLikeScanner:
    """Scanner for Java, C, C#, and similar brace-delimited languages."""

    CLASS_RE = re.compile(r"(?:public|private|protected|abstract|final|\s)+(?:class|interface|enum|struct)\s+(\w+)", re.MULTILINE)
    METHOD_RE = re.compile(
        r"(?:public|private|protected|static|\s)+"
        r"(?:[\w<>,\s]+\s+)?(\w+)\s*\(",
        re.MULTILINE,
    )
    IMPORT_RE = re.compile(r"import\s+([\w.]+)", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str, language: str) -> FileInfo:
        lines = content.splitlines()
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language=language,
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip() and not l.strip().startswith("//")]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=sum(1 for l in lines if l.strip().startswith("//")),
            docstring="",
        )

        # Imports
        for match in self.IMPORT_RE.finditer(content):
            module = match.group(1).split(".")[-1]
            line = content[: match.start()].count("\n") + 1
            info.imports.append(ImportInfo(module=module, names=[match.group(1)], line_number=line))

        # Classes
        for match in self.CLASS_RE.finditer(content):
            name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            methods = self._extract_methods(content, match.end())
            info.classes.append(
                ClassInfo(
                    name=name,
                    line_number=line,
                    docstring="",
                    bases=[],
                    methods=methods,
                    visibility="private" if name[0].islower() else "public",
                )
            )

        # Standalone functions/methods
        for match in self.METHOD_RE.finditer(content):
            name = match.group(1)
            if name not in {"if", "else", "for", "while", "switch", "try", "catch", "class", "interface", "enum"}:
                line = content[: match.start()].count("\n") + 1
                info.raw_functions.append(
                    FunctionInfo(name=name, line_number=line, signature=f"{name}(...)", docstring="", visibility="private" if name[0].islower() else "public")
                )

        return info

    def _extract_methods(self, content: str, start: int) -> list[FunctionInfo]:
        methods = []
        depth = 0
        for i, char in enumerate(content[start:], start=start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    break
        return []


# ---------------------------------------------------------------------------
# Go scanner
# ---------------------------------------------------------------------------

class GoScanner:
    """Scanner for Go source files."""

    FUNC_RE = re.compile(r"func\s+(\w+)\s*\(", re.MULTILINE)
    FUNC_RECEIVER_RE = re.compile(r"func\s+\(([^)]+)\)\s+(\w+)\s*\(", re.MULTILINE)
    IMPORT_RE = re.compile(r"import\s+(?:\(([^)]+)\)|\"([^\"]+)\")", re.MULTILINE)
    PACKAGE_RE = re.compile(r"package\s+(\w+)", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        lines = content.splitlines()
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="Go",
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip() and not l.strip().startswith("//")]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=sum(1 for l in lines if l.strip().startswith("//")),
            docstring="",
        )

        # Package docstring
        doc_lines = []
        for line in lines:
            if line.strip().startswith("//"):
                doc_lines.append(line.strip().lstrip("//").strip())
            elif doc_lines:
                break
        if doc_lines:
            info.docstring = " ".join(doc_lines)

        # Imports
        for match in self.IMPORT_RE.finditer(content):
            group = match.group(1) or match.group(2) or ""
            for imp in re.findall(r'"([^"]+)"', group):
                info.imports.append(ImportInfo(module=imp.split("/")[-1], names=[imp], line_number=content[: match.start()].count("\n") + 1))

        # Functions with receiver (methods)
        for match in self.FUNC_RECEIVER_RE.finditer(content):
            receiver = match.group(1)
            name = match.group(2)
            line = content[: match.start()].count("\n") + 1
            info.raw_functions.append(
                FunctionInfo(
                    name=f"{receiver}.{name}",
                    line_number=line,
                    signature=f"func ({receiver}) {name}(...)",
                    docstring="",
                    visibility="private" if receiver[0].islower() else "public",
                )
            )

        # Standalone functions
        for match in self.FUNC_RE.finditer(content):
            if ")" not in match.group(0)[:-1]:
                name = match.group(1)
                if name not in {"if", "for", "switch"}:
                    line = content[: match.start()].count("\n") + 1
                    info.raw_functions.append(
                        FunctionInfo(name=name, line_number=line, signature=f"func {name}(...)", docstring="", visibility="private" if name[0].islower() else "public")
                    )

        return info


# ---------------------------------------------------------------------------
# HTML / Markup scanner
# ---------------------------------------------------------------------------

class MarkupScanner:
    """Scanner for HTML, Vue, Svelte, XML markup files."""

    TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9.-]*)(?:\s[^>]*)?>", re.MULTILINE)
    ID_CLASS_RE = re.compile(r'\s(?:id|class)="([^"]+)"', re.MULTILINE)
    SCRIPT_RE = re.compile(r"<script[^>]*>([\s\S]*?)</script>", re.MULTILINE)
    STYLE_RE = re.compile(r"<style[^>]*>([\s\S]*?)</style>", re.MULTILINE)
    VUE_COMP_RE = re.compile(r"export\s+default\s+\{", re.MULTILINE)
    SVELTE_EXPORT_RE = re.compile(r"<script[^>]*>\s*([\s\S]*?)</script>", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        ext = file_path.suffix
        language = LANGUAGE_MAP.get(ext, "HTML")
        lines = content.splitlines()

        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language=language,
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip() and not l.strip().startswith("<!--")]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=sum(1 for l in lines if l.strip().startswith("<!--")),
            docstring="",
            structure={},
        )

        # Extract title from <title> or <h1>
        title_match = re.search(r"<title>([^<]+)</title>", content, re.IGNORECASE)
        if title_match:
            info.docstring = f"Page title: {title_match.group(1)}"
        h1_match = re.search(r"<h1[^>]*>([^<]+)</h1>", content, re.IGNORECASE)
        if h1_match:
            info.docstring = f"Heading: {h1_match.group(1)}"

        # Count tags
        tags = {}
        for match in self.TAG_RE.finditer(content):
            tag = match.group(1).lower()
            tags[tag] = tags.get(tag, 0) + 1

        info.structure = {"tags": dict(sorted(tags.items(), key=lambda x: -x[1])[:20]), "has_script": "<script" in content, "has_style": "<style" in content}

        # Detect framework
        if ext == ".vue":
            info.feature_tags.append("Vue Component")
        elif ext == ".svelte":
            info.feature_tags.append("Svelte Component")
        elif "<html" in content.lower():
            info.feature_tags.append("Web Page")
        elif "<svg" in content.lower():
            info.feature_tags.append("SVG Graphic")
        elif "<manifest" in content.lower():
            info.feature_tags.append("Web App Manifest")

        return info


# ---------------------------------------------------------------------------
# CSS scanner
# ---------------------------------------------------------------------------

class CSSScanner:
    """Scanner for CSS, SCSS, Sass, and Less files."""

    RULE_RE = re.compile(r"([.#@]?[\w-]+)\s*\{", re.MULTILINE)
    VAR_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+);", re.MULTILINE)
    MIXIN_RE = re.compile(r"@mixin\s+(\w+)", re.MULTILINE)
    INCLUDE_RE = re.compile(r"@include\s+(\w+)", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        ext = file_path.suffix
        language = LANGUAGE_MAP.get(ext, "CSS")
        lines = content.splitlines()

        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language=language,
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip() and not l.strip().startswith("//") and not l.strip().startswith("/*")]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=sum(1 for l in lines if l.strip().startswith("/*")),
            docstring="",
            structure={},
        )

        rules = {}
        for match in self.RULE_RE.finditer(content):
            selector = match.group(1)
            if selector not in rules:
                rules[selector] = 0
            rules[selector] += 1

        info.structure = {"selectors": dict(sorted(rules.items(), key=lambda x: -x[1])[:30]), "custom_properties": [m.group(1) for m in self.VAR_RE.finditer(content)]}

        if ext in {".scss", ".sass"}:
            info.feature_tags.append("SCSS")
        elif ext == ".less":
            info.feature_tags.append("Less")

        return info


# ---------------------------------------------------------------------------
# Config file scanners
# ---------------------------------------------------------------------------

class ConfigScanner:
    """Scanner for JSON, YAML, TOML, and config files."""

    def scan_json(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="JSON",
            total_lines=content.count("\n") + 1,
            code_lines=len([l for l in content.splitlines() if l.strip()]),
            blank_lines=0,
            comment_lines=0,
            docstring="",
            structure={},
        )
        try:
            import json as _json

            data = _json.loads(content)
            info.structure = self._summarize(data, max_depth=3)
        except Exception:
            pass
        return info

    def scan_yaml(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="YAML",
            total_lines=content.count("\n") + 1,
            code_lines=len([l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]),
            blank_lines=0,
            comment_lines=sum(1 for l in content.splitlines() if l.strip().startswith("#")),
            docstring="",
            structure={},
        )

        # Extract top-level keys
        top_keys = []
        current_key = None
        for line in content.splitlines():
            if re.match(r"^(\w[\w-]*):", line):
                current_key = re.match(r"^(\w[\w-]*):", line).group(1)
                top_keys.append(current_key)
            elif line.strip().startswith("- ") and current_key:
                info.structure.setdefault(f"{current_key}_items", []).append(line.strip()[2:].strip())

        info.structure["top_level_keys"] = top_keys
        return info

    def scan_toml(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="TOML",
            total_lines=content.count("\n") + 1,
            code_lines=len([l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]),
            blank_lines=0,
            comment_lines=sum(1 for l in content.splitlines() if l.strip().startswith("#")),
            docstring="",
            structure={},
        )

        sections = re.findall(r"^\[([^\]]+)\]", content, re.MULTILINE)
        info.structure["sections"] = sections
        return info

    def _summarize(self, data: Any, max_depth: int = 3, current_depth: int = 0) -> Any:
        if current_depth >= max_depth:
            return type(data).__name__
        if isinstance(data, dict):
            return {k: self._summarize(v, max_depth, current_depth + 1) for k, v in list(data.items())[:20]}
        elif isinstance(data, list):
            return [self._summarize(data[0], max_depth, current_depth + 1)] if data else []
        else:
            return type(data).__name__


# ---------------------------------------------------------------------------
# Shell script scanner
# ---------------------------------------------------------------------------

class ShellScanner:
    """Scanner for Shell/Bash scripts."""

    FUNC_RE = re.compile(r"(?:function\s+)?(\w+)\s*\(\)\s*(?:\{|)", re.MULTILINE)
    SHEBANG_RE = re.compile(r"^#!\s*/([\w/]+)", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        lines = content.splitlines()
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="Shell",
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip() and not l.strip().startswith("#")]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=sum(1 for l in lines if l.strip().startswith("#")),
            docstring="",
        )

        # Shebang docstring
        for line in lines[:5]:
            if line.strip().startswith("##"):
                info.docstring += " " + line.strip().lstrip("#").strip()

        # Functions
        for match in self.FUNC_RE.finditer(content):
            name = match.group(1)
            if name and name not in {"if", "then", "else", "fi", "do", "done", "for", "while", "case", "esac"}:
                line = content[: match.start()].count("\n") + 1
                info.raw_functions.append(
                    FunctionInfo(name=name, line_number=line, signature=f"{name}()", docstring="", visibility="private" if name.startswith("_") else "public")
                )

        return info


# ---------------------------------------------------------------------------
# Markdown scanner
# ---------------------------------------------------------------------------

class MarkdownScanner:
    """Scanner for Markdown files — extracts structure and code blocks."""

    HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    CODE_BLOCK_RE = re.compile(r"```(\w*)\n[\s\S]*?```", re.MULTILINE)
    LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)", re.MULTILINE)
    TASK_RE = re.compile(r"^\s*[-*+]\s*\[[ xX]\]\s+(.+)$", re.MULTILINE)

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        lines = content.splitlines()
        info = FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="Markdown",
            total_lines=len(lines),
            code_lines=0,
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=0,
            docstring="",
            structure={},
        )

        # Headings
        headings = []
        for match in self.HEADING_RE.finditer(content):
            level = len(match.group(1))
            text = match.group(2).strip()
            headings.append({"level": level, "text": text})

        # Code blocks
        code_blocks = []
        for match in self.CODE_BLOCK_RE.finditer(content):
            lang = match.group(1) or "text"
            block_lines = match.group(0).count("\n")
            code_blocks.append({"language": lang, "lines": block_lines})

        # Links
        links = []
        for match in self.LINK_RE.finditer(content):
            links.append({"text": match.group(1), "url": match.group(2)})

        info.structure = {
            "headings": headings,
            "code_blocks": code_blocks,
            "links": links[:20],
            "task_count": len(self.TASK_RE.findall(content)),
        }
        info.code_lines = sum(cb["lines"] for cb in code_blocks)

        # Feature tags
        content_lower = content.lower()
        if "python" in content_lower:
            info.feature_tags.append("Python")
        if "javascript" in content_lower or "typescript" in content_lower:
            info.feature_tags.append("JavaScript/TypeScript")
        if "docker" in content_lower:
            info.feature_tags.append("Docker")
        if "github" in content_lower or "gitlab" in content_lower:
            info.feature_tags.append("CI/CD")
        if "api" in content_lower:
            info.feature_tags.append("API")
        if "readme" in relative_path.lower():
            info.feature_tags.append("README")
        if "changelog" in relative_path.lower():
            info.feature_tags.append("Changelog")
        if "contributing" in relative_path.lower():
            info.feature_tags.append("Contributing Guide")

        return info


# ---------------------------------------------------------------------------
# Plain text scanner
# ---------------------------------------------------------------------------

class PlainTextScanner:
    """Fallback scanner for unknown file types."""

    def scan(self, content: str, file_path: Path, relative_path: str) -> FileInfo:
        lines = content.splitlines()
        return FileInfo(
            path=file_path,
            relative_path=relative_path,
            language="Plain Text",
            total_lines=len(lines),
            code_lines=len([l for l in lines if l.strip()]),
            blank_lines=sum(1 for l in lines if not l.strip()),
            comment_lines=0,
            docstring="",
        )


# ---------------------------------------------------------------------------
# Main Code Atlas generator
# ---------------------------------------------------------------------------

def strip(s: str, chars: str) -> str:
    for c in chars:
        s = s.strip(c)
    return s


class CodeAtlas:
    """Main orchestrator — walks a project and generates the code atlas."""

    def __init__(self, root_path: Path | str, excluded_roots: list[Path | str] | None = None) -> None:
        self.root = Path(root_path).resolve()
        self.excluded_roots = [Path(item).resolve() for item in (excluded_roots or [])]
        self.config_scanner = ConfigScanner()
        self.python_scanner = PythonScanner()
        self.js_scanner = JavaScriptScanner()
        self.rust_scanner = RustScanner()
        self.javalike_scanner = JavaLikeScanner()
        self.go_scanner = GoScanner()
        self.markup_scanner = MarkupScanner()
        self.css_scanner = CSSScanner()
        self.shell_scanner = ShellScanner()
        self.md_scanner = MarkdownScanner()
        self.plain_scanner = PlainTextScanner()
        self._lang_cache: dict[str, str] = {}

    def _is_under_excluded_root(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        for root in self.excluded_roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _should_exclude_path(self, path: Path) -> bool:
        if self._is_under_excluded_root(path):
            return True
        name = path.name
        if name in EXCLUDE_DIRS:
            return True
        if path.suffix.lower() in EXCLUDE_EXTENSIONS:
            return True
        for pattern in EXCLUDE_FILES:
            if pattern.startswith("*"):
                if name.endswith(pattern[1:]):
                    return True
            elif name == pattern:
                return True
        return False

    def _scan_file(self, file_path: Path, relative_path: str) -> FileInfo | None:
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            return None

        if not content.strip():
            return None

        ext = file_path.suffix.lower()

        # Python
        if ext == ".py":
            return self.python_scanner.scan(content, file_path, relative_path)

        # JavaScript / TypeScript
        if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            return self.js_scanner.scan(content, file_path, relative_path)

        # Rust
        if ext == ".rs":
            return self.rust_scanner.scan(content, file_path, relative_path)

        # Java, C, C++, C#, PHP, Ruby, Swift, Kotlin, Scala, etc.
        if ext in {".java", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".cs", ".php", ".rb", ".swift", ".kt", ".kts", ".scala", ".groovy", ".dart"}:
            lang_map = {".java": "Java", ".c": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".h": "C Header", ".hpp": "C++ Header", ".cs": "C#", ".php": "PHP", ".rb": "Ruby", ".swift": "Swift", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy", ".dart": "Dart"}
            return self.javalike_scanner.scan(content, file_path, relative_path, lang_map.get(ext, "Unknown"))

        # Go
        if ext == ".go":
            return self.go_scanner.scan(content, file_path, relative_path)

        # HTML / Vue / Svelte / XML
        if ext in {".html", ".htm", ".vue", ".svelte", ".xml"}:
            return self.markup_scanner.scan(content, file_path, relative_path)

        # CSS / SCSS / Sass / Less
        if ext in {".css", ".scss", ".sass", ".less"}:
            return self.css_scanner.scan(content, file_path, relative_path)

        # JSON
        if ext == ".json":
            return self.config_scanner.scan_json(content, file_path, relative_path)

        # YAML
        if ext in {".yaml", ".yml"}:
            return self.config_scanner.scan_yaml(content, file_path, relative_path)

        # TOML
        if ext == ".toml":
            return self.config_scanner.scan_toml(content, file_path, relative_path)

        # Shell
        if ext in {".sh", ".bash", ".bat", ".ps1"}:
            return self.shell_scanner.scan(content, file_path, relative_path)

        # Markdown
        if ext == ".md":
            return self.md_scanner.scan(content, file_path, relative_path)

        # Dockerfile (no extension but special name)
        if file_path.name.lower() in {"dockerfile", "makefile", "procfile", "jenkinsfile"}:
            return self.plain_scanner.scan(content, file_path, relative_path)

        return None

    def scan(self) -> AtlasResult:
        result = AtlasResult(
            project_name=self.root.name,
            project_path=str(self.root),
            generated_at=utc_now(),
            total_files=0,
            total_lines=0,
            languages={},
            files=[],
        )

        # Collect all files first
        all_files: list[tuple[Path, str]] = []
        for root, dirs, files in os.walk(self.root):
            root_path = Path(root)
            # Prune excluded dirs in-place
            dirs[:] = [
                d
                for d in dirs
                if d not in EXCLUDE_DIRS
                and not d.startswith(".")
                and not self._should_exclude_path(root_path / d)
            ]

            for fname in files:
                file_path = root_path / fname
                if self._should_exclude_path(file_path):
                    continue
                relative = str(file_path.relative_to(self.root))
                all_files.append((file_path, relative))

        # Sort by relative path for deterministic output
        all_files.sort(key=lambda x: x[1])

        for file_path, relative in all_files:
            info = self._scan_file(file_path, relative)
            if info is None:
                continue

            result.files.append(info)
            result.total_lines += info.total_lines

            lang = info.language
            result.languages[lang] = result.languages.get(lang, 0) + 1

        result.total_files = len(result.files)
        return result

    def generate_markdown(self, result: AtlasResult) -> str:
        """Render the atlas as a readable Markdown document."""
        lines = [
            "# Code Atlas",
            "",
            f"**Project:** {result.project_name}",
            f"**Path:** `{result.project_path}`",
            f"**Generated:** {result.generated_at}",
            f"**Total Files:** {result.total_files} ({result.total_lines:,} lines)",
            "",
            "## Languages",
            "",
        ]

        for lang, count in sorted(result.languages.items(), key=lambda x: -x[1]):
            lines.append(f"- {lang}: {count} file{'s' if count != 1 else ''}")

        lines.extend(["", "## Table of Contents", ""])

        # Build TOC from files grouped by directory
        by_dir: dict[str, list[FileInfo]] = {}
        for fi in result.files:
            parts = fi.relative_path.split("/")
            if len(parts) > 1:
                dir_name = parts[0]
            else:
                dir_name = "(root)"
            by_dir.setdefault(dir_name, []).append(fi)

        for dir_name in sorted(by_dir.keys()):
            lines.append(f"### {dir_name}/")
            for fi in sorted(by_dir[dir_name], key=lambda x: x.relative_path):
                anchor = fi.relative_path.replace("/", "-").replace(".", "-")
                lines.append(f"- [`{fi.relative_path}`](#{anchor}) — {fi.language} ({fi.total_lines}L)")

        lines.append("")
        lines.append("---")
        lines.append("")

        # Detailed file entries
        for fi in result.files:
            lines.append(f"### `{fi.relative_path}`")
            lines.append("")
            lines.append(f"**Language:** {fi.language}  ")
            lines.append(f"**Lines:** {fi.total_lines} (code: {fi.code_lines}, blank: {fi.blank_lines}, comments: {fi.comment_lines})")

            if fi.docstring:
                lines.append(f"**Summary:** {fi.docstring}")

            if fi.feature_tags:
                lines.append(f"**Tags:** {', '.join(fi.feature_tags)}")

            # Imports
            if fi.imports:
                lines.append("")
                lines.append("**Imports:**")
                for imp in fi.imports[:15]:
                    names = ", ".join(imp.names[:5]) + ("..." if len(imp.names) > 5 else "")
                    lines.append(f"- `{imp.module}` — {names} (line {imp.line_number})")

            # Structure (for config files)
            if fi.structure:
                lines.append("")
                lines.append("**Structure:**")
                self._render_structure(lines, fi.structure, depth=1)

            # Classes
            if fi.classes:
                lines.append("")
                for cls in fi.classes:
                    vis = "(private) " if cls.visibility == "private" else ""
                    lines.append(f"**Class `{cls.name}`** {vis}(line {cls.line_number})")
                    if cls.docstring:
                        lines.append(f": {cls.docstring}")
                    if cls.bases:
                        lines.append(f"  - Extends: `{'`, `'.join(cls.bases)}`")
                    if cls.methods:
                        lines.append("  - Methods:")
                        for m in cls.methods[:10]:
                            mvis = "(private) " if m.visibility == "private" else ""
                            lines.append(f"    - `{m.name}` {mvis}(line {m.line_number})")
                            if m.docstring:
                                lines.append(f"      : {m.docstring[:80]}{'...' if len(m.docstring) > 80 else ''}")
                        if len(cls.methods) > 10:
                            lines.append(f"    - ... and {len(cls.methods) - 10} more methods")

            # Top-level functions (Python)
            if fi.functions:
                lines.append("")
                lines.append("**Functions:**")
                for fn in fi.functions[:15]:
                    vis = "(private) " if fn.visibility == "private" else ""
                    lines.append(f"- `{fn.name}` {vis}(line {fn.line_number})")
                    if fn.docstring:
                        lines.append(f"  : {fn.docstring[:100]}{'...' if len(fn.docstring) > 100 else ''}")
                if len(fi.functions) > 15:
                    lines.append(f"- ... and {len(fi.functions) - 15} more functions")

            # Raw functions (other languages)
            if fi.raw_functions:
                lines.append("")
                lines.append("**Functions:**")
                shown = 0
                for fn in fi.raw_functions:
                    if fn.name in {"if", "else", "for", "while", "switch", "try", "catch", "case", "return"}:
                        continue
                    vis = "(private) " if fn.visibility == "private" else ""
                    lines.append(f"- `{fn.name}` {vis}(line {fn.line_number})")
                    shown += 1
                    if shown >= 20:
                        remaining = len(fi.raw_functions) - shown
                        if remaining > 0:
                            lines.append(f"- ... and {remaining} more")
                        break

            lines.append("")
            lines.append("---")
            lines.append("")

        # Summary statistics
        lines.append("## Summary Statistics")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|---------|-------|")
        lines.append(f"| Total Files | {result.total_files} |")
        lines.append(f"| Total Lines | {result.total_lines:,} |")
        lines.append(f"| Languages | {len(result.languages)} |")

        lines.append("")
        lines.append("### Files by Language")
        lines.append("")
        lines.append("| Language | Files |")
        lines.append(f"|----------|-------:|")
        for lang, count in sorted(result.languages.items(), key=lambda x: -x[1]):
            lines.append(f"| {lang} | {count} |")

        # Top 20 largest files
        largest = sorted(result.files, key=lambda x: x.total_lines, reverse=True)[:20]
        if largest:
            lines.append("")
            lines.append("### Largest Files")
            lines.append("")
            lines.append("| File | Language | Lines |")
            lines.append("|------|----------|------:|")
            for fi in largest:
                lines.append(f"| `{fi.relative_path}` | {fi.language} | {fi.total_lines:,} |")

        lines.append("")
        lines.append(f"*Generated by obsmcp Code Atlas on {result.generated_at}*")

        return "\n".join(lines)

    def _render_structure(self, lines: list[str], structure: Any, depth: int) -> None:
        indent = "  " * depth
        if isinstance(structure, dict):
            for key, value in list(structure.items())[:15]:
                if isinstance(value, (dict, list)) and value:
                    lines.append(f"{indent}- **{key}:**")
                    self._render_structure(lines, value, depth + 1)
                else:
                    lines.append(f"{indent}- **{key}:** {value}")
        elif isinstance(structure, list) and structure:
            for item in structure[:10]:
                if isinstance(item, (dict, list)):
                    self._render_structure(lines, item, depth)
                else:
                    lines.append(f"{indent}- {item}")
            if len(structure) > 10:
                lines.append(f"{indent}- ... ({len(structure) - 10} more)")


def generate_atlas(root_path: Path | str) -> AtlasResult:
    """Convenience function to generate a code atlas."""
    atlas = CodeAtlas(root_path)
    return atlas.scan()


def generate_atlas_markdown(root_path: Path | str) -> str:
    """Convenience function to generate a code atlas as Markdown."""
    atlas = CodeAtlas(root_path)
    result = atlas.scan()
    return atlas.generate_markdown(result)
