"""structlog setup: JSON in production, pretty console in development. Secrets are redacted."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from strogo.config import LogFormat

_SECRET_MARKERS = ("token", "secret", "api_key", "apikey", "password", "authorization", "cookie")
_NOISY_LOGGERS = ("httpx2", "httpx", "httpcore", "aiogram.event", "apscheduler", "asyncio")


def redact_secrets(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Mask any key that looks like a credential. Never logs a token, whatever the caller did."""
    for key in list(event_dict):
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS) and event_dict[key]:
            event_dict[key] = "***"
    return event_dict


def configure_logging(level: str = "INFO", fmt: LogFormat = "pretty") -> None:
    """Configure structlog and route stdlib logging through the same renderer."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
    ]
    renderer: Any
    if fmt == "json":
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Shortcut so modules write ``log = get_logger(__name__)``."""
    return structlog.get_logger(name) if name else structlog.get_logger()
