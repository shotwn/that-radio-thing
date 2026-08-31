"""Application logging.

Console output is configured by logzero's default handler; a rotating file
handler is attached so the last few MB of logs are always on disk. Both
handlers share the level set by the ``LOG_LEVEL`` env var (default INFO).

Any code that imports ``logzero.logger`` (including our own handlers here)
writes to both console and file automatically.

This module configures logging as an import side effect, which means it runs
before any error handling exists. It therefore never raises: a malformed size
variable falls back to its default, and an unusable ``LOG_FILE`` degrades to
console-only logging. Losing the file handler must not take the radio off the
air, and the container's ``LOG_FILE`` default (``/app/logs``) does not exist
when the suite runs on a developer machine.
"""

import logging
import os
import pprint
from pathlib import Path

import logzero


def _positive_int(name: str, default: int) -> int:
    """Read a positive integer env var, falling back on any malformed value."""

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


_LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)

_LOG_FILE = os.getenv("LOG_FILE", "thatradiothing.log")
_LOG_MAX_BYTES = _positive_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
_LOG_BACKUP_COUNT = _positive_int("LOG_BACKUP_COUNT", 5)

logzero.loglevel(_LOG_LEVEL)
if _LOG_FILE:
    try:
        Path(_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        logzero.logfile(
            _LOG_FILE,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            loglevel=_LOG_LEVEL,
        )
    except OSError:
        logzero.logger.warning(
            "Falling back to console-only logging: LOG_FILE %s is not writable", _LOG_FILE
        )


def format_value(thing):
    """Return a readable representation suitable for diagnostic logging."""

    return pprint.pformat(thing)


def info(thing):
    """Log *thing* at info level after formatting nested values."""

    logzero.logger.info(format_value(thing))


def debug(thing):
    """Log *thing* at debug level after formatting nested values."""

    logzero.logger.debug(format_value(thing))


def error(thing):
    """Log *thing* at error level after formatting nested values."""

    logzero.logger.error(format_value(thing))
