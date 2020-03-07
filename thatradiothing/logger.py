import logzero
import pprint

def format(thing):
    return pprint.pformat(thing)

def info(thing):
    logzero.logger.info(format(thing))
