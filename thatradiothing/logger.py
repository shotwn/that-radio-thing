"""Application logging.

Console output is configured by logzero's default handler; a rotating file
handler is attached so the last few MB of logs are always on disk. Both
handlers share the level set by the ``LOG_LEVEL`` env var (default INFO).

Any code that imports ``logzero.logger`` (including our own handlers here)
writes to both console and file automatically.
"""

import logging
import os
import pprint

import logzero

_LOG_LEVEL_NAME = os.getenv('LOG_LEVEL', 'INFO').upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)

_LOG_FILE = os.getenv('LOG_FILE', 'thatradiothing.log')
_LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', str(5 * 1024 * 1024)))
_LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', '5'))

logzero.loglevel(_LOG_LEVEL)
if _LOG_FILE:
    logzero.logfile(
        _LOG_FILE,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        loglevel=_LOG_LEVEL,
    )


def format(thing):
    return pprint.pformat(thing)


def info(thing):
    logzero.logger.info(format(thing))


def debug(thing):
    logzero.logger.debug(format(thing))


def error(thing):
    logzero.logger.error(format(thing))
