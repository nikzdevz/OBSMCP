from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import hashlib
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .observability import JSONFormatter


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, max_length: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned[:max_length] or "item"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_text_with_retry(
    path: Path,
    *,
    encoding: str = "utf-8",
    errors: str | None = None,
    attempts: int = 12,
    delay_seconds: float = 0.05,
    default: str | None = None,
) -> str:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            if errors is None:
                return path.read_text(encoding=encoding)
            return path.read_text(encoding=encoding, errors=errors)
        except FileNotFoundError:
            if default is not None:
                return default
            raise
        except PermissionError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    if default is not None:
        return default
    if last_error is not None:
        raise last_error
    raise FileNotFoundError(path)


def write_text_atomic(path: Path, content: str, encoding: str = "utf-8") -> None:
    ensure_parent(path)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding=encoding, dir=path.parent) as handle:
        handle.write(content)
        temp_name = handle.name
    temp_path = Path(temp_name)
    for _ in range(8):
        try:
            temp_path.replace(path)
            return
        except PermissionError:
            time.sleep(0.1)
    try:
        with path.open("w", encoding=encoding) as handle:
            handle.write(content)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


def read_json_with_retry(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        text = read_text_with_retry(path, default=None)
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def file_fingerprint(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    stat = file_path.stat()
    digest = hashlib.sha1()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "file_path": str(file_path),
        "fingerprint": digest.hexdigest(),
        "file_size": stat.st_size,
        "modified_at": modified_at,
        "scanned_at": utc_now(),
    }


def configure_logging(
    log_dir: Path,
    debug: bool = False,
    json_output: bool = False,
    json_output_path: str | None = None,
    include_traceback: bool = False,
    console_output: bool = True,
) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("obsmcp")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_dir / "obsmcp.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)

    error_handler = RotatingFileHandler(
        log_dir / "obsmcp-error.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.ERROR)

    logger.addHandler(file_handler)
    logger.addHandler(error_handler)

    # Optional structured JSON log file
    if json_output and json_output_path:
        json_handler = RotatingFileHandler(
            log_dir / json_output_path,
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        json_handler.setFormatter(JSONFormatter(include_traceback=include_traceback))
        json_handler.setLevel(logging.DEBUG)
        logger.addHandler(json_handler)

    if console_output and not env_bool("OBSMCP_NO_CONSOLE", False):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        logger.addHandler(console_handler)
    return logger


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
