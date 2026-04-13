from __future__ import annotations

import base64
import functools
import json
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


DEFAULT_OPUSMAX_BASE_URL = "https://api.opusmax.pro"
DEFAULT_TIMEOUT = 30.0
IMAGE_ANALYSIS_TIMEOUT = 60.0
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_QUERY_CHARS = 1000
MAX_PROMPT_CHARS = 4000
MAX_RESULTS_LIMIT = 10
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


@functools.lru_cache(maxsize=1)
def _read_claude_settings_env() -> dict[str, str]:
    claude_dir = Path.home() / ".claude"
    merged: dict[str, str] = {}
    for candidate in (claude_dir / "settings.json", claude_dir / "settings.local.json"):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        env = payload.get("env")
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if isinstance(key, str) and isinstance(value, str):
                merged[key] = value
    return merged


def _resolve_setting(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    settings_env = _read_claude_settings_env()
    for name in names:
        value = str(settings_env.get(name, "")).strip()
        if value:
            return value
    return ""


@dataclass
class ProviderCallResult:
    data: dict[str, Any]
    latency_ms: float
    raw: Any = None


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, int]
    latency_ms: float
    raw: Any = None


class OpusMaxBaseProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        configured_base = base_url or _resolve_setting("OPUS_MAX_BASE_URL", "ANTHROPIC_BASE_URL") or DEFAULT_OPUSMAX_BASE_URL
        self.api_key = api_key or _resolve_setting("OPUS_MAX_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        self.base_url = configured_base.rstrip("/")
        self.default_timeout = default_timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
            }
        )

    @property
    def api_root(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url[:-3]
        return self.base_url

    @property
    def compat_v1_root(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url
        return f"{self.base_url}/v1"

    def _require_api_key(self) -> None:
        if not self.api_key:
            raise ValueError("OpusMax API key is not configured. Set OPUS_MAX_API_KEY or ANTHROPIC_AUTH_TOKEN.")

    def _post_json(self, url: str, payload: dict[str, Any], timeout: float | None = None) -> ProviderCallResult:
        self._require_api_key()
        start = time.perf_counter()
        response = self._session.post(url, json=payload, timeout=timeout or self.default_timeout)
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        if not 200 <= response.status_code < 300:
            body = response.text.strip()
            snippet = body[:400] if body else "<empty body>"
            raise RuntimeError(f"OpusMax request failed ({response.status_code}): {snippet}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("OpusMax response was not a JSON object.")
        base_resp = data.get("base_resp")
        if isinstance(base_resp, dict):
            status_code = int(base_resp.get("status_code", 0) or 0)
            status_msg = str(base_resp.get("status_msg", "") or "").strip()
            if status_code and status_code not in {200, 201}:
                raise RuntimeError(f"OpusMax tool error ({status_code}): {status_msg or 'unknown error'}")
        return ProviderCallResult(data=data, latency_ms=latency_ms, raw=data)


class OpusMaxTextProvider(OpusMaxBaseProvider):
    MODEL_HAIKU = "haiku-4.5"
    MODEL_SONNET = "sonnet-4.6"

    SYSTEM_PROMPT = """You are a code analyst. Given a code entity, produce a detailed but concise semantic description with these exact fields:

purpose: One-sentence purpose of this entity (be specific, not generic).
why_it_exists: One sentence explaining the specific problem or gap this entity fills.
how_it_is_used: One sentence on how callers or other code uses this entity.
inputs_outputs: One sentence on key inputs and outputs / side effects.
side_effects: One sentence on any I/O, filesystem, network, or state-mutating behavior.
risks: One sentence on any reliability, security, or coordination risks.
language: One word (Python, TypeScript, Rust, Go, Java, etc.)

Respond ONLY with a valid JSON object with these exact keys: purpose, why_it_exists, how_it_is_used, inputs_outputs, side_effects, risks, language. No markdown, no code fences, no extra text."""

    def _call_api(
        self,
        model: str,
        user_message: str,
        timeout: float | None = None,
        response_contract: str | None = None,
    ) -> LLMResponse | None:
        system_prompt = self.SYSTEM_PROMPT
        if response_contract:
            system_prompt = f"{system_prompt}\n\n{response_contract.strip()}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 512,
            "temperature": 0.2,
        }
        try:
            result = self._post_json(f"{self.compat_v1_root}/chat/completions", payload, timeout=timeout)
        except Exception:
            return None
        choices = result.data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message", {})
        content = message.get("content", "")
        usage = result.data.get("usage", {})
        return LLMResponse(
            text=str(content).strip(),
            model=model,
            usage={
                "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "output_tokens": int(usage.get("completion_tokens", 0) or 0),
            },
            latency_ms=result.latency_ms,
            raw=result.raw,
        )

    def generate_description(
        self,
        entity: dict[str, Any],
        snippet: str,
        context: str | None = None,
        response_contract: str | None = None,
    ) -> dict[str, Any] | None:
        lines = [
            f"Entity type: {entity.get('entity_type', 'unknown')}",
            f"Name: {entity.get('name', 'unknown')}",
            f"File: {entity.get('file_path', 'unknown')}",
        ]
        sig = entity.get("signature", "")
        if sig:
            lines.append(f"Signature: {sig}")
        tags = entity.get("feature_tags", [])
        if tags:
            lines.append(f"Feature tags: {', '.join(tags)}")
        lines.append("")
        lines.append("Source snippet:")
        lines.append(snippet[:800])
        if context:
            lines.append("")
            lines.append(f"Context: {context[:400]}")
        user_message = "\n".join(lines)

        resp = self._call_api(self.MODEL_HAIKU, user_message, response_contract=response_contract)
        if not resp:
            resp = self._call_api(self.MODEL_SONNET, user_message, response_contract=response_contract)
        if not resp:
            return None

        try:
            parsed = json.loads(resp.text)
        except json.JSONDecodeError:
            return None

        return {
            "purpose": parsed.get("purpose", ""),
            "why_it_exists": parsed.get("why_it_exists", ""),
            "how_it_is_used": parsed.get("how_it_is_used", ""),
            "inputs_outputs": parsed.get("inputs_outputs", ""),
            "side_effects": parsed.get("side_effects", ""),
            "risks": parsed.get("risks", ""),
            "language": parsed.get("language", entity.get("metadata", {}).get("language", "unknown")),
            "llm_model": resp.model,
            "llm_latency_ms": resp.latency_ms,
            "llm_input_tokens": resp.usage["input_tokens"],
            "llm_output_tokens": resp.usage["output_tokens"],
            "llm_generated": True,
        }


class OpusMaxToolProvider(OpusMaxBaseProvider):
    def _extract_results(self, payload: dict[str, Any]) -> list[Any]:
        for key in ("results", "items", "data", "organic"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _extract_summary(self, payload: dict[str, Any]) -> str:
        for key in ("summary", "answer", "result", "analysis", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def web_search(self, query: str, max_results: int | None = None) -> dict[str, Any]:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValueError("query is required")
        if len(cleaned_query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must be {MAX_QUERY_CHARS} characters or fewer.")
        if max_results is not None and not 1 <= int(max_results) <= MAX_RESULTS_LIMIT:
            raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_LIMIT}.")
        request_id = f"ws_{uuid.uuid4().hex[:10]}"
        payload: dict[str, Any] = {"query": cleaned_query}
        if max_results is not None:
            payload["max_results"] = int(max_results)
        result = self._post_json(f"{self.api_root}/tools/web_search", payload)
        return {
            "request_id": request_id,
            "provider": "opusmax",
            "endpoint": "/tools/web_search",
            "query": cleaned_query,
            "latency_ms": result.latency_ms,
            "results": self._extract_results(result.data),
            "summary": self._extract_summary(result.data),
            "raw": result.data,
        }

    def _guess_mime_type(self, path: Path, mime_type: str | None = None) -> str:
        if mime_type:
            guessed = mime_type.strip().lower()
        else:
            guessed = (mimetypes.guess_type(path.name)[0] or "").lower()
        if guessed not in SUPPORTED_IMAGE_MIME_TYPES:
            raise ValueError("Only JPEG, PNG, and WebP image inputs are supported.")
        return guessed

    def _file_to_data_url(self, image_path: str, mime_type: str | None = None) -> tuple[str, dict[str, Any]]:
        path = Path(image_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise ValueError(f"Image path does not exist: {image_path}")
        size_bytes = path.stat().st_size
        if size_bytes > MAX_IMAGE_BYTES:
            raise ValueError(f"Image file exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit.")
        detected_mime = self._guess_mime_type(path, mime_type=mime_type)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return (
            f"data:{detected_mime};base64,{encoded}",
            {"kind": "path", "path": str(path), "mime_type": detected_mime, "size_bytes": size_bytes},
        )

    def _coerce_image_url(
        self,
        *,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        mime_type: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if image_path:
            return self._file_to_data_url(image_path, mime_type=mime_type)
        if image_url:
            candidate = image_url.strip()
            if not candidate:
                raise ValueError("image_url cannot be empty")
            if candidate.startswith(("http://", "https://", "data:")):
                return candidate, {"kind": "url" if candidate.startswith("http") else "data_url"}
            path_candidate = Path(candidate).expanduser()
            if path_candidate.exists():
                return self._file_to_data_url(str(path_candidate), mime_type=mime_type)
            raise ValueError("image_url must be an http(s) URL, a data URL, or an existing local file path.")
        if image_base64:
            encoded = image_base64.strip()
            if not encoded:
                raise ValueError("image_base64 cannot be empty")
            if len(encoded) > ((MAX_IMAGE_BYTES * 4) // 3) + 1024:
                raise ValueError("image_base64 payload exceeds the configured image size limit.")
            if encoded.startswith("data:"):
                return encoded, {"kind": "data_url"}
            normalized_mime = (mime_type or "image/png").strip().lower()
            if normalized_mime not in SUPPORTED_IMAGE_MIME_TYPES:
                raise ValueError("mime_type must be image/jpeg, image/png, or image/webp when image_base64 is provided.")
            return f"data:{normalized_mime};base64,{encoded}", {"kind": "base64", "mime_type": normalized_mime}
        raise ValueError("Provide one of: image_url, image_path, or image_base64.")

    def understand_image(
        self,
        prompt: str,
        *,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise ValueError("prompt is required")
        if len(cleaned_prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt must be {MAX_PROMPT_CHARS} characters or fewer.")
        request_id = f"img_{uuid.uuid4().hex[:10]}"
        resolved_image_url, source_meta = self._coerce_image_url(
            image_url=image_url,
            image_path=image_path,
            image_base64=image_base64,
            mime_type=mime_type,
        )
        payload = {
            "prompt": cleaned_prompt,
            "image_url": resolved_image_url,
        }
        result = self._post_json(f"{self.api_root}/tools/understand_image", payload, timeout=max(self.default_timeout, IMAGE_ANALYSIS_TIMEOUT))
        return {
            "request_id": request_id,
            "provider": "opusmax",
            "endpoint": "/tools/understand_image",
            "prompt": cleaned_prompt,
            "latency_ms": result.latency_ms,
            "image_source": source_meta,
            "analysis": self._extract_summary(result.data) or result.data,
            "raw": result.data,
        }


_text_provider: OpusMaxTextProvider | None = None
_tool_provider: OpusMaxToolProvider | None = None


def get_opusmax_text_provider() -> OpusMaxTextProvider:
    global _text_provider
    if _text_provider is None:
        _text_provider = OpusMaxTextProvider()
    return _text_provider


def get_opusmax_tool_provider() -> OpusMaxToolProvider:
    global _tool_provider
    if _tool_provider is None:
        _tool_provider = OpusMaxToolProvider()
    return _tool_provider
