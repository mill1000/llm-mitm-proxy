"""Logging setup: one timestamped text format on stderr for app and libraries.

Applied on Context creation (idempotent). Uvicorn runs with ``log_config=None``
and a WARNING floor, so its records propagate through this handler with the
same format (see ``app._run``).

Levels: ``verbose`` < ``debug`` < ``info`` < ``warning`` < ``error``
(``--log-level``). ``VERBOSE`` (5) sits below DEBUG for the noisiest library
chatter: the ``httpx2``/``httpcore2`` loggers are filtered so their INFO
request summaries show at DEBUG and their DEBUG connection chatter only at
VERBOSE.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .config import Settings

VERBOSE = 5
logging.addLevelName(VERBOSE, "VERBOSE")

_TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_LIB_LOGGERS = ("httpx2", "httpcore2")


class _Formatter(logging.Formatter):
    """ISO8601 timestamps (``2026-09-10T14:17:59.104``), not asctime's comma style."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}"


def _is_lib_http(record: logging.LogRecord) -> bool:
    return record.name in _LIB_LOGGERS or record.name.startswith(tuple(n + "." for n in _LIB_LOGGERS))


def configure_logging(settings: Settings) -> None:
    """Apply LOG_LEVEL to the root logger and gate noisy libraries (idempotent)."""
    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO

    def _lib_filter(record: logging.LogRecord) -> bool:
        # httpx2/httpcore2: INFO request summaries at DEBUG or below, DEBUG
        # connection chatter at VERBOSE or below. WARNING and above pass.
        if _is_lib_http(record):
            if record.levelno < logging.INFO:
                return level <= VERBOSE
            if record.levelno == logging.INFO:
                return level <= logging.DEBUG
        return True

    handler = logging.StreamHandler()
    # The handler filters at the configured level too: a child logger set lower
    # (e.g. by a test) must not print through the root handler above that level.
    handler.setLevel(level)
    handler.setFormatter(_Formatter(_TEXT_FORMAT))
    handler.addFilter(_lib_filter)
    # force=True so repeated app creation (tests) re-applies the settings cleanly.
    logging.basicConfig(level=level, handlers=[handler], force=True)
