import os
import sys
import time
import logging
logger = logging.getLogger(__name__)

start = int(time.time())

def log(msg, header=False, log_dir=None, force=False):
    output_str = "==> %s" % msg if header else msg

    logger.info(output_str)
