import logging
import logzero
import pprint

logging.basicConfig(level=logging.WARNING)


def format(thing):
    return pprint.pformat(thing)


def info(thing):
    logzero.logger.info(format(thing))


def debug(thing):
    logzero.logger.debug(format(thing))


def error(thing):
    logzero.logger.error(format(thing))
