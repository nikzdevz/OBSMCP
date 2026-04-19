"""LLM-powered semantic description of source files.

Reads the Anthropic API key from standard Claude config locations (Claude
Desktop / Claude CLI) with a fallback to ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.llm")

SEMANTIC_DESCRIPTION_PROMPT = """\
You are an expert software architect analyzing a source code file.
Provide a concise semantic description (2-4 sentences) of what this file does
and its role in the codebase. Focus on: what problem does it solve, what are
its key exports, and how does it relate to the rest of the codebase.

File: {file_path}
Language: {language}

Source (first 2000 chars):
{content}
"""


def _claude_config_paths() -> list[Path]:
    if platform.system() == "Windows":
        return [
            Path(os.path.expandvars(r"%APPDATA%\Claude\claude_desktop_config.json")),
            Path(os.path.expandvars(r"%USERPROFILE%\.claude\config.json")),
        ]
    return [
        Path("~/.claude/config.json").expanduser(),
        Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser(),
    ]


def get_anthropic_api_key(config: Config | None = None) -> str:
    for path in _claude_config_paths():
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("api_key"):
                    return str(data["api_key"])
            except (OSError, json.JSONDecodeError):
                continue
    env_var = config.llm_api_key_env if config else "ANTHROPIC_API_KEY"
    return os.environ.get(env_var, "")


async def describe_file(
    file_path: str,
    language: str,
    content: str | None,
    config: Config,
) -> str | None:
    api_key = get_anthropic_api_key(config)
    if not api_key:
        return None
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        logger.warning("anthropic package not installed — semantic descriptions disabled")
        return None

    if content is None:
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="ignore")[:2000]
        except OSError:
            content = ""

    prompt = SEMANTIC_DESCRIPTION_PROMPT.format(
        file_path=file_path, language=language, content=content[:2000]
    )
    client = anthropic.AsyncAnthropic(api_key=api_key, base_url=config.llm_base_url or None)
    try:
        response = await client.messages.create(
            model=config.llm_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM call failed: %s", exc)
        return None
    parts = response.content or []
    if not parts:
        return None
    first = parts[0]
    text = getattr(first, "text", None)
    return text.strip() if isinstance(text, str) else None
