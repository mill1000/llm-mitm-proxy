"""Logging setup: human-readable text logs on stderr.

Applied in the app lifespan (idempotent). uvicorn's dictConfig leaves the root
logger without a handler, so without this the app's INFO logs (e.g. the
llm_proxy.ws connection trace) would be silently dropped.
"""

from __future__ import annotations

import logging

from .config import Settings

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Apply LOG_LEVEL to the root logger (idempotent)."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    # The handler filters at the configured level too: a child logger set lower
    # (e.g. by a test) must not print through the root handler above that level.
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    # force=True so repeated app creation (tests) re-applies the settings cleanly.
    logging.basicConfig(level=level, handlers=[handler], force=True)
