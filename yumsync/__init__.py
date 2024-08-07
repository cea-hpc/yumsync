import os
import sys
import multiprocessing
import signal

try:
    import urlparse
except (ImportError, ModuleNotFoundError):
    # Python3
    from urllib.parse import urlparse

from yumsync import util, progress
from yumsync.log import log
from yumsync.metadata import __version__

try:
    import copy_reg
except (ImportError, ModuleNotFoundError):
    # Python3
    import copyreg as copy_reg

import types

import logging
logger = logging.getLogger(__name__)

import six
import signal

import traceback

def pickle_method(method):
    func_name = method.im_func.__name__
    obj = method.im_self
    cls = method.im_class
    return unpickle_method, (func_name, obj, cls)

def unpickle_method(func_name, obj, cls):
    for cls in cls.mro():
        try:
            func = cls.__dict__[func_name]
        except KeyError:
            pass
        else:
            break
    return func.__get__(obj, cls)

copy_reg.pickle(types.MethodType, pickle_method, unpickle_method)

def init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)

def sync(repos=None, callback=None, processes=None, workers=1, multiprocess=True, temp_log_hdlr=None):
    """ Mirror repositories with configuration data from multiple sources.

    Handles all input validation and higher-level logic before passing control
    on to threads for doing the actual syncing. One thread is created per
    repository to alleviate the impact of slow mirrors on faster ones.
    """

    if repos is None:
        repos = []

    # Don't multiprocess when asked
    if multiprocess == False:
        logging.info("Sync process in mono-process mode, handling repos one by one")
        for repo in repos:
            logging.info("Syncing repo {}".format(repo.id))
            repo.sync()
            logging.info("Done syncing repo {}".format(repo.id))
        sys.exit(0)

    # Unset console handlers, as the progress UI will take over
    if temp_log_hdlr:
        logging.debug("Removing console logging handler, progress UI to take over")
        logging.root.removeHandler(temp_log_hdlr)

    prog = progress.Progress()  # callbacks talk to this object
    logging.root.addHandler(prog.log_handler(logging.WARNING))

    manager = multiprocessing.Manager()
    queue = manager.Queue()
    pool = multiprocessing.Pool(processes=processes, initializer=init_worker, initargs=[], maxtasksperchild=1)
    process_results = []

    def signal_handler(_signum, _frame):
        """ Inner method for terminating threads on signal events.

        This method uses os.kill() to send a SIGKILL directly to the process ID
        because the child processes are running blocking calls that will likely
        take a long time to complete.
        """
        log('Caught exit signal - aborting')
        pool.terminate()
        sys.exit(1) # safe to do exit() here because we are a worker

    # Catch user-cancelled or killed signals to terminate threads.
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    def err_callback(exc):
        logger.error("A process ended with error, traceback follows")
        logger.error("\n".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    for repo in repos:
        logger.debug("Setup callback and async job for repo {}".format(repo.id))
        prog.update(repo.id) # Add the repo to the progress object
        yumcallback = progress.YumProgress(repo.id, queue, callback)
        repocallback = progress.ProgressCallback(queue, callback)

        repo.set_yum_callback(yumcallback)
        repo.set_repo_callback(repocallback)

        if six.PY2:
            process_results.append(pool.apply_async(repo.sync, kwds={"workers": workers, "queue": queue}))
        elif six.PY3:
            process_results.append(pool.apply_async(repo.sync, kwds={"workers": workers, "queue": queue}, error_callback=err_callback))

    try:
        while len(process_results) > 0:
            # If data is waiting in the queue from the workers, process it. This
            # needs to be done in the current scope so that one progress object may
            # hold all of the results. (This might be easier with Python 3's
            # nonlocal keyword).
            while not queue.empty():
                event = queue.get()
                if isinstance(event, logging.LogRecord):
                    logging.getLogger(event.name).handle(event)
                    continue
                logger.info("Process queue event {}".format(event))
                if not 'action' in event:
                    continue
                if event['action'] == 'repo_init' and 'data' in event:
                    prog.update(event['repo_id'], set_total=event['data'][0])
                elif event['action'] == 'download_end' and 'data' in event:
                    prog.update(event['repo_id'], pkgs_downloaded=event['data'][0])
                elif event['action'] == 'repo_metadata' and 'data' in event:
                    prog.update(event['repo_id'], repo_metadata=event['data'][0])
                elif event['action'] == 'repo_error' and 'data' in event:
                    prog.update(event['repo_id'], repo_error=event['data'][0])
                elif event['action'] == 'pkg_exists':
                    prog.update(event['repo_id'], pkgs_downloaded=1)
                elif event['action'] == 'link_local_pkg':
                    prog.update(event['repo_id'], pkgs_downloaded=1)
                elif event['action'] == 'repo_complete':
                    pass # should already know this, but handle it anyways.
                elif event['action'] == 'delete_pkg':
                    prog.update(event['repo_id'], pkgs_deleted=1)
                elif event['action'] == 'repo_group_data':
                    pass
            for proc in process_results:
                if proc.ready():
                    if proc.successful():
                        logger.info("A Process ended, removing from waiting list")
                    else:
                        prog.update(event['repo_id'], repo_error=event['data'][0])
                        logger.error("A Process ended with error, removing from waiting list")
                    process_results.remove(proc)
    except KeyboardInterrupt:
        pool.terminate()
        pool.join()

    prog.clear_term()

    # Return tuple (#repos, #fail, elapsed time)
    return (len(repos), prog.totals['errors'], prog.elapsed())
