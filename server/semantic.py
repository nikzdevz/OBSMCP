from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .code_atlas import AtlasResult, ClassInfo, FileInfo, FunctionInfo
from .utils import file_fingerprint, read_text_with_retry, slugify, utc_now
from .llm_client import generate_llm_description


def _normalize_relpath(value: str) -> str:
    return value.replace("\\", "/")


def _first_sentence(value: str) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        return ""
    for sep in [". ", "\n", "; "]:
        if sep in text:
            return text.split(sep, 1)[0].strip().rstrip(".")
    return text[:180].rstrip(".")


def _name_to_phrase(name: str) -> str:
    words = [item for item in re.split(r"[._\-]+", name) if item]
    if not words:
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name) or [name]
    return " ".join(word.lower() for word in words)


def _stable_entity_key(entity_type: str, file_path: str, name: str, line_number: int = 0) -> str:
    if entity_type == "module":
        return f"module:{file_path}"
    if entity_type == "feature":
        return f"feature:{slugify(name, max_length=64)}"
    return f"{entity_type}:{file_path}:{name}:{line_number}"


def _symbol_path(file_path: str, name: str | None = None) -> str:
    return f"{file_path}::{name}" if name else file_path


def _make_summary_hint(entity_type: str, name: str, docstring: str, file_path: str, feature_tags: list[str]) -> str:
    if docstring:
        return _first_sentence(docstring)
    if entity_type == "module":
        tag_text = ", ".join(feature_tags[:3]) or "project"
        return f"Module `{file_path}` provides {tag_text.lower()} behavior."
    if entity_type == "class":
        return f"Class `{name}` groups {_name_to_phrase(name)} behavior."
    if entity_type == "function":
        return f"Function `{name}` handles {_name_to_phrase(name)}."
    return f"Feature `{name}` groups related project behavior."


def _read_snippet(root_path: Path, file_path: str, line_number: int = 0, radius: int = 18) -> str:
    absolute = root_path / file_path
    if not absolute.exists():
        return ""
    try:
        lines = read_text_with_retry(absolute, encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    if line_number <= 0:
        return "\n".join(lines[: min(len(lines), radius * 2)])
    start = max(0, line_number - radius - 1)
    end = min(len(lines), line_number + radius)
    return "\n".join(lines[start:end])


def _infer_side_effects(snippet: str) -> str:
    lowered = snippet.lower()
    effects: list[str] = []
    if any(token in lowered for token in ["write_text", "write_json", "unlink(", "remove-item", "delete", "replace("]):
        effects.append("writes or deletes local files")
    if any(token in lowered for token in ["subprocess", "popen(", "run(", "start-process", "taskkill"]):
        effects.append("launches or manages subprocesses")
    if any(token in lowered for token in ["sqlite", "execute(", "insert into", "update ", "delete from"]):
        effects.append("reads or mutates SQLite-backed state")
    if any(token in lowered for token in ["fastapi", "uvicorn", "@app.", "http", "request", "response"]):
        effects.append("handles HTTP or MCP traffic")
    if any(token in lowered for token in ["obsidian", "vault", ".md", "markdown"]):
        effects.append("updates Obsidian or Markdown artifacts")
    if any(token in lowered for token in ["os.walk", "rglob(", "scan("]):
        effects.append("scans the project filesystem")
    return "; ".join(dict.fromkeys(effects)) if effects else "No important side effects inferred from the current code slice."


def _infer_risks(snippet: str, related_files: list[str], signature: str) -> str:
    lowered = snippet.lower()
    risks: list[str] = []
    if "delete" in lowered or "remove-item" in lowered:
        risks.append("destructive filesystem behavior requires path safety")
    if any(token in lowered for token in ["execute(", "insert into", "update ", "delete from"]):
        risks.append("schema or data changes can affect continuity state")
    if any(token in lowered for token in ["subprocess", "popen(", "start-process"]):
        risks.append("process-launch behavior can vary across Windows shells")
    if len(related_files) > 4:
        risks.append("multiple related files raise coordination risk during handoff")
    if signature and len(signature) > 120:
        risks.append("large signature suggests broad responsibilities")
    return "; ".join(dict.fromkeys(risks)) if risks else "Low risk based on current structural signals."


@dataclass
class SemanticEntity:
    entity_key: str
    entity_type: str
    name: str
    file_path: str
    symbol_path: str
    signature: str
    line_number: int
    feature_tags: list[str]
    source_files: list[str]
    source_fingerprint: str
    summary_hint: str
    metadata: dict[str, Any]

    def to_index_row(self) -> dict[str, Any]:
        return {
            "entity_key": self.entity_key,
            "entity_type": self.entity_type,
            "name": self.name,
            "file_path": self.file_path,
            "symbol_path": self.symbol_path,
            "signature": self.signature,
            "line_number": self.line_number,
            "feature_tags": self.feature_tags,
            "source_files": self.source_files,
            "source_fingerprint": self.source_fingerprint,
            "summary_hint": self.summary_hint,
            "metadata": self.metadata,
            "updated_at": utc_now(),
        }


class SemanticIndex:
    def __init__(self, root_path: Path, result: AtlasResult) -> None:
        self.root_path = root_path
        self.result = result
        self.file_map: dict[str, FileInfo] = {_normalize_relpath(item.relative_path): item for item in result.files}
        self.entities = self._build_entities()
        self.entity_map = {item.entity_key: item for item in self.entities}

    def _fingerprint_for(self, relative_path: str) -> str:
        absolute = self.root_path / relative_path
        if not absolute.exists():
            return "missing"
        return file_fingerprint(absolute)["fingerprint"]

    def _module_entity(self, file_info: FileInfo) -> SemanticEntity:
        relpath = _normalize_relpath(file_info.relative_path)
        return SemanticEntity(
            entity_key=_stable_entity_key("module", relpath, relpath),
            entity_type="module",
            name=Path(relpath).name,
            file_path=relpath,
            symbol_path=_symbol_path(relpath),
            signature="",
            line_number=1,
            feature_tags=list(file_info.feature_tags),
            source_files=[relpath],
            source_fingerprint=self._fingerprint_for(relpath),
            summary_hint=_make_summary_hint("module", relpath, file_info.docstring, relpath, file_info.feature_tags),
            metadata={
                "docstring": file_info.docstring,
                "language": file_info.language,
                "imports": [item.to_dict() for item in file_info.imports],
                "class_count": len(file_info.classes),
                "function_count": len(file_info.functions) + len(file_info.raw_functions),
                "structure": file_info.structure,
            },
        )

    def _class_entity(self, file_info: FileInfo, class_info: ClassInfo) -> SemanticEntity:
        relpath = _normalize_relpath(file_info.relative_path)
        signature = f"class {class_info.name}({', '.join(class_info.bases)})" if class_info.bases else f"class {class_info.name}"
        return SemanticEntity(
            entity_key=_stable_entity_key("class", relpath, class_info.name, class_info.line_number),
            entity_type="class",
            name=class_info.name,
            file_path=relpath,
            symbol_path=_symbol_path(relpath, class_info.name),
            signature=signature,
            line_number=class_info.line_number,
            feature_tags=list(dict.fromkeys(file_info.feature_tags + ["Class"])),
            source_files=[relpath],
            source_fingerprint=self._fingerprint_for(relpath),
            summary_hint=_make_summary_hint("class", class_info.name, class_info.docstring, relpath, file_info.feature_tags),
            metadata={
                "docstring": class_info.docstring,
                "bases": class_info.bases,
                "methods": [item.to_dict() for item in class_info.methods],
            },
        )

    def _method_entity(self, file_info: FileInfo, class_info: ClassInfo, method_info: FunctionInfo) -> SemanticEntity:
        relpath = _normalize_relpath(file_info.relative_path)
        method_name = f"{class_info.name}.{method_info.name}"
        if method_info.signature:
            signature_tail = method_info.signature[4:] if method_info.signature.startswith("def ") else method_info.signature
            signature = f"{class_info.name}.{signature_tail}"
        else:
            signature = method_name
        return SemanticEntity(
            entity_key=_stable_entity_key("function", relpath, method_name, method_info.line_number),
            entity_type="function",
            name=method_info.name,
            file_path=relpath,
            symbol_path=_symbol_path(relpath, method_name),
            signature=signature,
            line_number=method_info.line_number,
            feature_tags=list(dict.fromkeys(file_info.feature_tags + ["Function", "Method"])),
            source_files=[relpath],
            source_fingerprint=self._fingerprint_for(relpath),
            summary_hint=_make_summary_hint("function", method_name, method_info.docstring, relpath, file_info.feature_tags),
            metadata={
                "docstring": method_info.docstring,
                "visibility": method_info.visibility,
                "class_name": class_info.name,
                "method_name": method_info.name,
                "bases": class_info.bases,
            },
        )

    def _function_entity(self, file_info: FileInfo, function_info: FunctionInfo) -> SemanticEntity:
        relpath = _normalize_relpath(file_info.relative_path)
        return SemanticEntity(
            entity_key=_stable_entity_key("function", relpath, function_info.name, function_info.line_number),
            entity_type="function",
            name=function_info.name,
            file_path=relpath,
            symbol_path=_symbol_path(relpath, function_info.name),
            signature=function_info.signature or function_info.name,
            line_number=function_info.line_number,
            feature_tags=list(dict.fromkeys(file_info.feature_tags + ["Function"])),
            source_files=[relpath],
            source_fingerprint=self._fingerprint_for(relpath),
            summary_hint=_make_summary_hint("function", function_info.name, function_info.docstring, relpath, file_info.feature_tags),
            metadata={
                "docstring": function_info.docstring,
                "visibility": function_info.visibility,
            },
        )

    def _feature_entities(self) -> list[SemanticEntity]:
        by_feature: dict[str, list[FileInfo]] = {}
        for file_info in self.result.files:
            for tag in file_info.feature_tags:
                by_feature.setdefault(tag, []).append(file_info)
        entities: list[SemanticEntity] = []
        for tag, files in sorted(by_feature.items()):
            relpaths = sorted({_normalize_relpath(item.relative_path) for item in files})
            combined = "|".join(self._fingerprint_for(path) for path in relpaths)
            entities.append(
                SemanticEntity(
                    entity_key=_stable_entity_key("feature", relpaths[0] if relpaths else "feature", tag),
                    entity_type="feature",
                    name=tag,
                    file_path=relpaths[0] if relpaths else "",
                    symbol_path=tag,
                    signature="",
                    line_number=1,
                    feature_tags=[tag],
                    source_files=relpaths,
                    source_fingerprint=combined,
                    summary_hint=f"Feature `{tag}` spans {len(relpaths)} file(s).",
                    metadata={
                        "files": relpaths,
                        "language_count": len({item.language for item in files}),
                    },
                )
            )
        return entities

    def _build_entities(self) -> list[SemanticEntity]:
        entities: list[SemanticEntity] = []
        for file_info in self.result.files:
            entities.append(self._module_entity(file_info))
            for class_info in file_info.classes:
                entities.append(self._class_entity(file_info, class_info))
                for method_info in class_info.methods:
                    entities.append(self._method_entity(file_info, class_info, method_info))
            for function_info in [*file_info.functions, *file_info.raw_functions]:
                entities.append(self._function_entity(file_info, function_info))
        entities.extend(self._feature_entities())
        return entities

    def build_index_payload(self) -> dict[str, Any]:
        file_fingerprints = []
        for relative_path in sorted(self.file_map.keys()):
            absolute = self.root_path / relative_path
            if absolute.exists():
                payload = file_fingerprint(absolute)
                payload["file_path"] = relative_path
                file_fingerprints.append(payload)
        return {
            "entities": [item.to_index_row() for item in self.entities],
            "file_fingerprints": file_fingerprints,
            "counts": {
                "modules": len([item for item in self.entities if item.entity_type == "module"]),
                "functions": len([item for item in self.entities if item.entity_type == "function"]),
                "classes": len([item for item in self.entities if item.entity_type == "class"]),
                "features": len([item for item in self.entities if item.entity_type == "feature"]),
            },
        }

    def get_module(self, module_path: str) -> SemanticEntity | None:
        normalized = _normalize_relpath(module_path)
        key = _stable_entity_key("module", normalized, normalized)
        return self.entity_map.get(key)

    def get_symbol_candidates(self, symbol_name: str, module_path: str | None = None, entity_types: list[str] | None = None) -> list[SemanticEntity]:
        normalized_module = _normalize_relpath(module_path) if module_path else None
        allowed = set(entity_types or ["function", "class"])
        candidates = [
            item
            for item in self.entities
            if item.entity_type in allowed and item.name == symbol_name and (normalized_module is None or item.file_path == normalized_module)
        ]
        return sorted(candidates, key=lambda item: (item.file_path, item.line_number))

    def get_feature(self, feature_name: str) -> SemanticEntity | None:
        target = feature_name.lower()
        partial: SemanticEntity | None = None
        for item in self.entities:
            if item.entity_type != "feature":
                continue
            lowered = item.name.lower()
            if lowered == target:
                return item
            if partial is None and (target in lowered or lowered in target):
                partial = item
        return partial

    def search(self, query: str, limit: int = 10) -> list[SemanticEntity]:
        normalized = query.lower()
        scored: list[tuple[int, SemanticEntity]] = []
        for entity in self.entities:
            haystack = " ".join(
                [
                    entity.name,
                    entity.file_path,
                    entity.symbol_path,
                    entity.summary_hint,
                    " ".join(entity.feature_tags),
                ]
            ).lower()
            if normalized not in haystack:
                continue
            score = 0
            if entity.name.lower() == normalized:
                score += 10
            if entity.file_path.lower() == normalized:
                score += 9
            if normalized in entity.name.lower():
                score += 5
            if normalized in entity.file_path.lower():
                score += 3
            scored.append((score, entity))
        scored.sort(key=lambda item: (-item[0], item[1].entity_type, item[1].file_path, item[1].line_number))
        return [entity for _, entity in scored[:limit]]

    def related_symbols(self, entity: SemanticEntity, limit: int = 8) -> list[SemanticEntity]:
        related: list[SemanticEntity] = []
        for candidate in self.entities:
            if candidate.entity_key == entity.entity_key:
                continue
            if candidate.file_path == entity.file_path and candidate.entity_type in {"module", "function", "class"}:
                related.append(candidate)
                continue
            if set(candidate.feature_tags) & set(entity.feature_tags):
                related.append(candidate)
        unique: dict[str, SemanticEntity] = {}
        for item in related:
            unique.setdefault(item.entity_key, item)
        return list(unique.values())[:limit]

    def render_summary_markdown(self) -> str:
        lines = [
            "# Symbol Index Summary",
            "",
            f"- Modules: {len([item for item in self.entities if item.entity_type == 'module'])}",
            f"- Functions: {len([item for item in self.entities if item.entity_type == 'function'])}",
            f"- Classes: {len([item for item in self.entities if item.entity_type == 'class'])}",
            f"- Features: {len([item for item in self.entities if item.entity_type == 'feature'])}",
            "",
        ]
        return "\n".join(lines)


def build_semantic_index(root_path: Path | str, result: AtlasResult) -> SemanticIndex:
    return SemanticIndex(Path(root_path), result)


def generate_semantic_description(
    entity: dict[str, Any],
    index: SemanticIndex,
    store: Any,
    project_root: Path,
    force_llm: bool = False,
    allow_llm: bool = True,
    response_contract: str | None = None,
) -> dict[str, Any]:
    file_path = entity["file_path"]
    snippet = _read_snippet(project_root, file_path, entity.get("line_number", 0))
    metadata = entity.get("metadata", {})
    docstring = metadata.get("docstring", "")
    language = metadata.get("language", "")
    related_task_rows = store.get_tasks_for_files([file_path], limit=5) if file_path else []
    related_decision_rows = store.get_related_decisions_for_files([file_path], limit=5) if file_path else []
    related_symbols = index.related_symbols(index.entity_map[entity["entity_key"]], limit=6)
    related_files = list(dict.fromkeys([file_path, *metadata.get("files", []), *store.get_recent_file_activity(limit=8)]))

    # Build context string for LLM
    context_parts = []
    if related_decision_rows:
        context_parts.append("Related decisions: " + "; ".join(f"{d['title']}: {d['decision']}" for d in related_decision_rows[:3]))
    if related_task_rows:
        context_parts.append("Related tasks: " + ", ".join(f"{t['id']} {t['title']}" for t in related_task_rows[:3]))
    context_str = " | ".join(context_parts) if context_parts else None

    # Try LLM generation first if force_llm or docstring present
    llm_description: dict[str, Any] | None = None
    if allow_llm and (force_llm or docstring):
        llm_description = generate_llm_description(entity, snippet, context_str, response_contract=response_contract)

    if entity["entity_type"] == "module":
        purpose = llm_description["purpose"] if llm_description else (docstring or f"Implements {_name_to_phrase(Path(file_path).stem)} behavior in the `{Path(file_path).parent}` area of the project.")
        why = llm_description["why_it_exists"] if llm_description else (f"Exists to provide {', '.join(entity.get('feature_tags') or [language or 'project'])} behavior for the codebase.")
        how = llm_description["how_it_is_used"] if llm_description else (f"Contains {metadata.get('class_count', 0)} class(es), {metadata.get('function_count', 0)} function(s), and is a likely entry point for edits touching `{file_path}`.")
        inputs_outputs = llm_description["inputs_outputs"] if llm_description else "Primary inputs and outputs are exposed through the module's public classes, functions, configuration structure, and imported integrations."
        side_effects = llm_description["side_effects"] if llm_description else _infer_side_effects(snippet)
        risks = llm_description["risks"] if llm_description else _infer_risks(snippet, related_files, "")
        detected_language = llm_description["language"] if llm_description else language
    elif entity["entity_type"] == "class":
        bases = metadata.get("bases", [])
        purpose = llm_description["purpose"] if llm_description else (docstring or f"Encapsulates {_name_to_phrase(entity['name'])} behavior inside `{file_path}`.")
        why = llm_description["why_it_exists"] if llm_description else (f"Exists to centralize state and methods for `{entity['name']}` so related responsibilities stay grouped.")
        how = llm_description["how_it_is_used"] if llm_description else (f"Used through its methods in `{file_path}` and nearby symbols; base classes: {', '.join(bases) if bases else 'none'}.")
        inputs_outputs = llm_description["inputs_outputs"] if llm_description else (f"Constructor and method surface follows `{entity['signature']}` and the class methods recorded in the Code Atlas.")
        side_effects = llm_description["side_effects"] if llm_description else _infer_side_effects(snippet)
        risks = llm_description["risks"] if llm_description else _infer_risks(snippet, related_files, entity.get("signature", ""))
        detected_language = llm_description["language"] if llm_description else language
    elif entity["entity_type"] == "function":
        class_name = metadata.get("class_name")
        callable_name = f"{class_name}.{entity['name']}" if class_name else entity["name"]
        purpose = llm_description["purpose"] if llm_description else (docstring or f"Handles {_name_to_phrase(callable_name)} within `{file_path}`.")
        if llm_description:
            why = llm_description["why_it_exists"]
            how = llm_description["how_it_is_used"]
        elif class_name:
            why = f"Exists to keep `{entity['name']}` behavior attached to the `{class_name}` state and lifecycle."
            how = f"Called through `{class_name}` instances when the project needs `{_name_to_phrase(entity['name'])}` behavior."
        else:
            why = f"Exists to isolate one callable unit of {_name_to_phrase(entity['name'])} behavior."
            how = f"Called from `{file_path}` or related symbols when the project needs `{_name_to_phrase(entity['name'])}` behavior."
        inputs_outputs = llm_description["inputs_outputs"] if llm_description else (f"Signature: `{entity['signature'] or callable_name}`.")
        side_effects = llm_description["side_effects"] if llm_description else _infer_side_effects(snippet)
        risks = llm_description["risks"] if llm_description else _infer_risks(snippet, related_files, entity.get("signature", ""))
        detected_language = llm_description["language"] if llm_description else language
    else:
        files = metadata.get("files", [])
        purpose = llm_description["purpose"] if llm_description else (f"Aggregates the `{entity['name']}` feature across {len(files)} file(s).")
        why = llm_description["why_it_exists"] if llm_description else (f"Exists to give agents a compact feature-level view without rereading every implementation file.")
        how = llm_description["how_it_is_used"] if llm_description else (f"Used as a semantic entry point for files tagged `{entity['name']}`.")
        inputs_outputs = llm_description["inputs_outputs"] if llm_description else "Inputs are the tagged implementation files; outputs are the grouped behaviors and workflows they enable."
        side_effects = llm_description["side_effects"] if llm_description else _infer_side_effects(snippet)
        risks = llm_description["risks"] if llm_description else _infer_risks(snippet, related_files, "")
        detected_language = llm_description["language"] if llm_description else language

    description = {
        "entity_type": entity["entity_type"],
        "entity_key": entity["entity_key"],
        "name": entity["name"],
        "file": file_path,
        "signature": entity.get("signature", ""),
        "purpose": _first_sentence(purpose) or purpose,
        "why_it_exists": _first_sentence(why) or why,
        "how_it_is_used": _first_sentence(how) or how,
        "inputs_outputs": _first_sentence(inputs_outputs) or inputs_outputs,
        "side_effects": _first_sentence(side_effects) or side_effects,
        "risks": _first_sentence(risks) or risks,
        "language": detected_language,
        "llm_generated": llm_description is not None,
        "llm_model": llm_description.get("llm_model") if llm_description else None,
        "llm_latency_ms": llm_description.get("llm_latency_ms") if llm_description else None,
        "related_files": [item for item in related_files if item][:8],
        "related_decisions": [
            {"id": item["id"], "title": item["title"], "decision": item["decision"], "created_at": item["created_at"]}
            for item in related_decision_rows
        ],
        "related_tasks": [
            {"id": item["id"], "title": item["title"], "status": item["status"], "priority": item["priority"]}
            for item in related_task_rows
        ],
        "related_symbols": [
            {"entity_key": item.entity_key, "entity_type": item.entity_type, "name": item.name, "file_path": item.file_path}
            for item in related_symbols
        ],
        "source_fingerprint": entity["source_fingerprint"],
        "generated_at": utc_now(),
        "verified_at": utc_now(),
        "freshness": "fresh",
        "metadata": {
            "feature_tags": entity.get("feature_tags", []),
            "summary_hint": entity.get("summary_hint", ""),
        },
    }
    return description


def generate_llm_fallback_description(
    entity: dict[str, Any],
    snippet: str,
    response_contract: str | None = None,
) -> dict[str, Any] | None:
    """
    Standalone LLM description generation that doesn't require the full index/store pipeline.
    Used by the async LLM enrichment path.
    """
    return generate_llm_description(entity, snippet, context=None, response_contract=response_contract)


def render_semantic_note(description: dict[str, Any]) -> str:
    file_value = description.get("file") or description.get("file_path", "")
    lines = [
        f"# {description['entity_type'].title()} Knowledge: {description['name']}",
        "",
        f"- Key: `{description['entity_key']}`",
        f"- File: `{file_value}`",
        f"- Generated: {description['generated_at']}",
        f"- Freshness: {description.get('freshness', 'unknown')}",
        "",
        "## Purpose",
        "",
        description["purpose"],
        "",
        "## Why It Exists",
        "",
        description["why_it_exists"],
        "",
        "## How It Is Used",
        "",
        description["how_it_is_used"],
        "",
        "## Inputs and Outputs",
        "",
        description["inputs_outputs"],
        "",
        "## Side Effects",
        "",
        description["side_effects"],
        "",
        "## Risks",
        "",
        description["risks"],
        "",
        "## Related Files",
        "",
    ]
    lines.extend([f"- `{item}`" for item in description.get("related_files", [])] or ["- None"])
    lines.extend(["", "## Related Symbols", ""])
    lines.extend(
        [f"- `{item['entity_type']}` `{item['name']}` in `{item['file_path']}`" for item in description.get("related_symbols", [])]
        or ["- None"]
    )
    lines.extend(["", "## Related Decisions", ""])
    lines.extend(
        [f"- [{item['id']}] {item['title']}: {item['decision']}" for item in description.get("related_decisions", [])]
        or ["- None"]
    )
    lines.extend(["", "## Related Tasks", ""])
    lines.extend(
        [f"- `{item['id']}` {item['title']} [{item['status']}] priority={item['priority']}" for item in description.get("related_tasks", [])]
        or ["- None"]
    )
    lines.append("")
    return "\n".join(lines)
