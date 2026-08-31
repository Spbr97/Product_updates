"""Structured logging.

One ``configure_logging`` call at process start (API, CLI, worker) wires structlog and
the stdlib root logger together. Everything else calls :func:`get_logger` and binds
context, so a single check produces correlated events:

    check.started -> fetch.result -> change.detected -> rule.matched
                  -> notification.created -> notification.sent -> check.finished

A redaction processor removes anything that looks like a secret before rendering, so a
stray ``log.info("...", api_key=...)`` cannot leak.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

#: Keys whose values are replaced with ``***`` before rendering.
_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[-_ ]?key|authorization|cookie|credential)",
    re.IGNORECASE,
)
_REDACTED = "***"

#: Canonical event names. Using these constants keeps log queries stable.
EVENT_CHECK_STARTED = "check.started"
EVENT_CHECK_FINISHED = "check.finished"
EVENT_FETCH_RESULT = "fetch.result"
EVENT_CHANGE_DETECTED = "change.detected"
EVENT_RULE_MATCHED = "rule.matched"
EVENT_NOTIFICATION_CREATED = "notification.created"
EVENT_NOTIFICATION_SENT = "notification.sent"
EVENT_NOTIFICATION_FAILED = "notification.failed"
EVENT_JOB_SCHEDULED = "job.scheduled"
EVENT_JOB_RECONCILED = "job.reconciled"


def _redact_secrets(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Blank out values whose key looks sensitive, at any nesting depth."""
    for key, value in list(event_dict.items()):
        if _SECRET_KEY_PATTERN.search(str(key)):
            event_dict[key] = _REDACTED
        elif isinstance(value, dict):
            event_dict[key] = _redact_mapping(value)
    return event_dict


def _redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            _REDACTED
            if _SECRET_KEY_PATTERN.search(str(key))
            else _redact_mapping(value)
            if isinstance(value, dict)
            else value
        )
        for key, value in mapping.items()
    }


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog and the stdlib root logger.

    Idempotent: calling it again reconfigures cleanly, which matters for tests and for
    the CLI, where the log level can be overridden per invocation.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_secrets,
    ]

    renderer: Processor
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.processors.JSONRenderer()
        shared.append(structlog.processors.dict_tracebacks)

    # The stdlib factory (rather than PrintLogger) is what makes ``add_logger_name`` work
    # and lets our events share one handler with uvicorn, SQLAlchemy, and APScheduler.
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, apscheduler) through the same handler.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())

    # These are noisy at INFO and say nothing we do not already log ourselves.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for a module."""
    return structlog.get_logger(name)


def bind_context(**values: Any) -> None:
    """Bind values onto every subsequent log line in this context (thread/task)."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    """Drop all bound context values."""
    structlog.contextvars.clear_contextvars()
