"""Process entrypoint: settings → logging → bot + scheduler + web server in one process.

Owner: the integration build agent (wires ``telegram/bot.py``, ``proactive/scheduler.py`` and
``web/server.py``; run the loop with ``uvloop.run(...)`` per research/09 §1 item 10). The skeleton
only validates configuration.
"""

from __future__ import annotations

import sys

import structlog

from strikt.config import get_settings
from strikt.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log = structlog.get_logger(__name__)
    missing = settings.missing_for_runtime()
    if missing:
        log.error("missing_settings", names=missing)
        sys.exit(2)
    raise NotImplementedError("implemented by build stage: run bot, scheduler and web server")
