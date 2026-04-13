"""
obsmcp Structured Observability Layer

Provides:
- StructuredLogger: JSON-formatted logs with correlation IDs
- Context variables: request_id, session_id, project_path, task_id
- Span context for OpenTelemetry integration (Phase 3 prep)
- Decorators for automatic function tracing

Usage:
    from .observability import get_logger, set_request_context, span

    logger = get_logger("obsmcp")
    set_request_context(request_id="req-123", session_id="SESSION-abc")
    logger.info("tool_started", tool_name="describe_module")
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from logging.handlers import RotatingFileHandler

# ------------------------------------------------------------------------------
# Context Variables (thread-safe, request-scoped)
# ------------------------------------------------------------------------------


class _ObsmcpContext:
    """Thread-local and async-safe correlation context."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, str] = {}

    def set(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._data.pop(key, None)
            else:
                self._data[key] = value

    def get(self, key: str) -> str:
        with self._lock:
            return self._data.get(key, "")

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# Module-level singleton — shared across all loggers
_ctx = _ObsmcpContext()


def set_request_context(
    request_id: str | None = None,
    session_id: str | None = None,
    project_path: str | None = None,
    task_id: str | None = None,
) -> None:
    """Set correlation context for the current request/thread."""
    if request_id is not None:
        _ctx.set("request_id", request_id)
    if session_id is not None:
        _ctx.set("session_id", session_id)
    if project_path is not None:
        _ctx.set("project_path", project_path)
    if task_id is not None:
        _ctx.set("task_id", task_id)


def clear_request_context() -> None:
    """Clear all correlation context."""
    _ctx.clear()


def get_request_context() -> dict[str, str]:
    """Get all current correlation context."""
    return _ctx.all()


def generate_request_id() -> str:
    """Generate a short unique request ID."""
    return str(uuid.uuid4())[:8]


# ------------------------------------------------------------------------------
# JSON Formatter
# ------------------------------------------------------------------------------


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class JSONFormatter:
    """Formatter that emits one JSON object per log record."""

    def __init__(self, include_traceback: bool = False) -> None:
        self.include_traceback = include_traceback

    def format(self, record: logging.LogRecord) -> str:
        ctx = _ctx.all()
        payload: dict[str, Any] = {
            "timestamp": _utc_now(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": ctx.get("request_id"),
            "session_id": ctx.get("session_id"),
            "project_path": ctx.get("project_path"),
            "task_id": ctx.get("task_id"),
        }

        # Add extra fields from the LogRecord
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "pathname", "process", "processName", "relativeCreated",
                "stack_info", "exc_info", "exc_text", "thread", "threadName",
                "taskName", "request_id", "session_id", "project_path", "task_id",
            }:
                if value is not None:
                    payload[key] = value

        # Attach traceback if present
        if record.exc_info and self.include_traceback:
            payload["traceback"] = traceback.format_exception(*record.exc_info)

        # Remove empty correlation fields
        payload = {k: v for k, v in payload.items() if v or k in ("level", "timestamp", "logger", "message")}

        return json.dumps(payload, ensure_ascii=True, default=str)


# ------------------------------------------------------------------------------
# Structured Logger
# ------------------------------------------------------------------------------


class StructuredLogger:
    """
    A logger wrapper that always emits structured JSON records with correlation context.

    Usage:
        logger = StructuredLogger("obsmcp", log_dir=Path("logs"))
        logger.info("tool_completed", tool_name="describe_module", duration_ms=342.1)
        logger.error("tool_failed", tool_name="session_open", error_type="InternalError")
        logger.warning("session_stale", session_id="SESSION-123", idle_seconds=3600)
    """

    def __init__(
        self,
        name: str = "obsmcp",
        log_dir: Path | None = None,
        level: int = logging.INFO,
        json_format: bool = True,
        json_output_path: str | None = None,
        include_traceback: bool = False,
    ) -> None:
        self.name = name
        self._json_format = json_format
        self._json_output_path = json_output_path
        self._include_traceback = include_traceback

        self._base_logger = logging.getLogger(name)
        self._base_logger.setLevel(level)
        self._handlers: list[logging.Handler] = list(self._base_logger.handlers)

        # JSON file handler (dedicated structured log)
        if json_output_path and log_dir:
            json_path = Path(json_output_path)
            if not json_path.is_absolute():
                json_path = (log_dir / json_output_path).resolve()
            json_handler = RotatingFileHandler(
                json_path,
                maxBytes=5_000_000,
                backupCount=5,
                encoding="utf-8",
            )
            json_handler.setFormatter(JSONFormatter(include_traceback=include_traceback))
            json_handler.setLevel(logging.DEBUG)
            self._base_logger.addHandler(json_handler)
            self._handlers.append(json_handler)

    def _emit(self, level: int, event: str, **kwargs: Any) -> None:
        ctx = _ctx.all()
        extra: dict[str, Any] = {
            "event": event,
            "request_id": ctx.get("request_id"),
            "session_id": ctx.get("session_id"),
            "project_path": ctx.get("project_path"),
            "task_id": ctx.get("task_id"),
            **kwargs,
        }
        self._base_logger.log(level, event, extra=extra)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.CRITICAL, event, **kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=kwargs.pop("exc_info", True), **kwargs)


# ------------------------------------------------------------------------------
# Global logger registry (lazy init)
# ------------------------------------------------------------------------------

_loggers: dict[str, StructuredLogger] = {}
_init_lock = threading.Lock()


def get_logger(
    name: str = "obsmcp",
    log_dir: Path | None = None,
    level: int = logging.INFO,
    json_format: bool = True,
    json_output_path: str | None = None,
) -> StructuredLogger:
    """Get or create a StructuredLogger instance."""
    key = f"{name}:{log_dir}:{level}:{json_format}:{json_output_path}"
    with _init_lock:
        if key not in _loggers:
            _loggers[key] = StructuredLogger(
                name=name,
                log_dir=log_dir,
                level=level,
                json_format=json_format,
                json_output_path=json_output_path,
            )
        return _loggers[key]


# ------------------------------------------------------------------------------
# Span / Trace context (Phase 3 OpenTelemetry prep)
# ------------------------------------------------------------------------------


@dataclass
class Span:
    """Lightweight span for timing and attribute tracking."""
    name: str
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()).replace("-", "")[:16])
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()).replace("-", "")[:8])
    parent_id: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    attributes: dict[str, Any] = field(default_factory=dict)
    ended: bool = False
    error: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_error(self, error: str | None) -> None:
        self.error = error

    @property
    def duration_ms(self) -> float:
        end = time.perf_counter() if not self.ended else self._ended_at
        return (end - self.started_at) * 1000

    @property
    def _ended_at(self) -> float:
        return time.perf_counter()


# Global span stack for nested spans
_span_stack: list[Span] = []
_span_lock = threading.RLock()


def current_span() -> Span | None:
    """Get the current active span."""
    with _span_lock:
        return _span_stack[-1] if _span_stack else None


@contextmanager
def span(name: str, **attributes: Any) -> Any:
    """
    Context manager for creating a traced span.

    Usage:
        with span("semantic.describe", entity_key="function:foo.py:bar") as s:
            result = do_work()
            if error:
                s.set_error("timeout")
            else:
                s.set_attribute("cached", True)

    The span emits a log record on entry and exit.
    """
    s = Span(name=name, parent_id=current_span().span_id if current_span() else None)
    for k, v in attributes.items():
        s.set_attribute(k, v)

    logger = get_logger("obsmcp.span")
    logger.debug("span_start", span_name=name, span_id=s.span_id, parent_id=s.parent_id, **attributes)

    with _span_lock:
        _span_stack.append(s)

    try:
        yield s
    except Exception as exc:
        s.set_error(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        s.ended = True
        with _span_lock:
            _span_stack.pop()
        logger.debug(
            "span_end",
            span_name=name,
            span_id=s.span_id,
            duration_ms=round(s.duration_ms, 2),
            error=s.error,
            **{k: v for k, v in s.attributes.items() if k not in ("span_name", "span_id", "duration_ms", "error")},
        )


def traced(name: str | None = None) -> Callable[[Callable], Callable]:
    """
    Decorator for automatic span tracing on any function.

    Usage:
        @traced("semantic.describe")
        def describe_module(module_path):
            ...
    """
    def decorator(func: Callable) -> Callable:
        span_name = name or f"{func.__module__}.{func.__qualname__}"

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, func=func.__qualname__):
                return func(*args, **kwargs)
        return wrapper
    return decorator
