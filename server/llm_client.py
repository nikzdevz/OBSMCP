"""Compatibility wrappers for semantic descriptions backed by OpusMax providers."""
from __future__ import annotations

import os
from typing import Any

from .opusmax_provider import OpusMaxTextProvider

OPUS_MAX_API_KEY = os.environ.get("OPUS_MAX_API_KEY", os.environ.get("ANTHROPIC_AUTH_TOKEN", ""))


class LLMClient(OpusMaxTextProvider):
    """Backward-compatible semantic description client."""


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def generate_llm_description(
    entity: dict[str, Any],
    snippet: str,
    context: str | None = None,
    response_contract: str | None = None,
) -> dict[str, Any] | None:
    """
    Convenience wrapper: get the global LLM client and generate a description.

    Returns None if the API key is not configured or all calls fail.
    """
    if not OPUS_MAX_API_KEY:
        return None
    return get_llm_client().generate_description(entity, snippet, context, response_contract=response_contract)
