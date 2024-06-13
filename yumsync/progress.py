import sys
import datetime
import itertools
from blessings import Terminal
import logging
import six
import dnf

logger = logging.getLogger(__name__)

class ErrorCatcher(logging.Handler):
    def __init__(self, callback, levelno):
        self.callback = callback
        self.levelno = levelno
        super().__init__(level=self.levelno)

    def emit(self, record):
        # If there is a newline, cut it out and keep only the first line
        msg = record.getMessage().split('\n')[0]
        self.callback("{} - {}:{}".format(record.name, record.filename, record.lineno), msg)

class Progress(object):
    """ Handle progress indication using callbacks.

    This class will create an object that stores information about a
    running yumsync process. It stores information about each repository
    being synced, including total packages, completed packages, and
    the status of the repository metadata. This makes it possible to
    display aggregated status of multiple repositories during a sync.
    """
    repos = {}
    totals = {
        'numpkgs': 0,
        'dlpkgs': 0,
        'md_complete': 0,
        'md_total': 0,
        'errors':0
    }
    errors = []

    def __init__(self):
        """ records the time the sync started.
            and initialise blessings terminal """
        self.start = datetime.datetime.now()
        self.linecount = 0
        if sys.stdout.isatty():
            self.term = Terminal()
            sys.stdout.write(self.term.clear())

    def __del__(self):
        """ destructor - need to reset the terminal ."""
        self.clear_term()

    def clear_term(self):
        if sys.stdout.isatty():
            sys.stdout.write(self.term.clear)
            sys.stdout.flush()

    def log_handler(self, levelno):
        return ErrorCatcher(self.add_error, levelno)

    def add_error(self, spec, message):
        self.totals['errors'] += 1
        self.errors.append((spec, message))

    def update(self, repo_id, set_total=None, pkgs_downloaded=None,
               pkg_exists=None, repo_metadata=None, repo_error=None, pkgs_deleted=None):
        """ Handles updating the object itself.

        This method will be called any time the number of packages in
        a repository becomes known, when any package finishes downloading,
        when repository metadata begins indexing and when it completes.
        """
        if not repo_id in self.repos:
            self.repos[repo_id] = {'numpkgs':0, 'dlpkgs':0, 'repomd':0}
            self.totals['md_total'] += 1
        if set_total:
            self.repos[repo_id]['numpkgs'] = set_total
            self.totals['numpkgs'] = 0
            for _, repo in six.iteritems(self.repos):
                self.totals['numpkgs'] += repo['numpkgs']
        if pkgs_downloaded:
            self.repos[repo_id]['dlpkgs'] += pkgs_downloaded
            self.totals['dlpkgs'] += pkgs_downloaded
        if pkgs_deleted:
            self.repos[repo_id]['dlpkgs'] -= pkgs_deleted
            self.totals['dlpkgs'] -= pkgs_deleted
        if repo_metadata:
            if repo_metadata == 'complete':
                self.totals['md_complete'] += 1
            self.repos[repo_id]['repomd'] = repo_metadata
        if repo_error:
            self.totals['errors'] += 1
            if self.repos[repo_id]['repomd'] != 'complete':
                self.totals['md_total'] -= 1
            self.errors.append((repo_id, repo_error))

        if sys.stdout.isatty():
            self.formatted()

    def color(self, string, color=None):
        if color and hasattr(self.term, color):
            return '{}{}{}'.format(getattr(self.term, color),
                                   string,
                                   self.term.normal)
        return string

    @classmethod
    def pct(cls, current, total):
        """ Calculate a percentage. """
        if total == 0:
            return "0"
        val = current / float(total) * 100
        formatted = '{:0.1f}%'.format(val)
        return formatted

    def elapsed(self):
        """ Calculate and return elapsed time.

        This function does dumb rounding by just plucking off anything past a
        dot "." in a time delta between two datetime.datetime()'s.
        """
        return str(datetime.datetime.now() - self.start).split('.')[0]

    def format_header(self):
        repos = self.repos.keys()
        max_repo = len(max(repos, key=len))

        repo = '{:<{}s}'.format('Repository', max_repo)
        done = '{:>5s}'.format('Done')
        total = '{:>5s}'.format('Total')
        complete = 'Packages'
        metadata = 'Metadata'
        header_str = '{}  {}/{}  {}  {}'.format(repo, done, total, complete, metadata)

        return header_str, len(repo), len(done), len(total), len(complete), len(metadata)

    @classmethod
    def format_line(cls, reponame, package_counts, percent, repomd):
        """ Return a string formatted for output.

        Since there is a common column layout in the progress indicator, we can
        we can implement the printf-style formatter in a function.
        """
        return '{}  {}  {}  {}'.format(reponame, package_counts, percent, repomd)

    def represent_repo_pkgs(self, repo_id, a, b):
        """ Format the ratio of packages in a repository. """
        numpkgs = self.repos[repo_id]['numpkgs']
        dlpkgs = self.repos[repo_id]['dlpkgs']
        return self.represent_pkgs(dlpkgs, numpkgs, a, b)

    def represent_total_pkgs(self, a, b):
        """ Format the total number of packages in all repositories. """
        numpkgs = self.totals['numpkgs']
        dlpkgs = self.totals['dlpkgs']
        return self.represent_pkgs(dlpkgs, numpkgs, a, b)

    @classmethod
    def represent_pkgs(cls, dlpkgs, numpkgs, a, b):
        """ Represent a package ratio.

        This will display nothing if the number of packages is 0 or unknown, or
        typical done/total if total is > 0.
        """
        if numpkgs == 0:
            return '{:^{}s}'.format('-', a + b + 1)
        else:
            return '{0:>{2}}/{1:<{3}}'.format(dlpkgs, numpkgs, a, b)


    def represent_repo_percent(self, repo_id, length):
        """ Display the percentage of packages downloaded in a repository. """
        numpkgs = self.repos[repo_id]['numpkgs']
        dlpkgs = self.repos[repo_id]['dlpkgs']
        return self.represent_percent(dlpkgs, numpkgs, length)

    def represent_repomd_percent(self, repo_id, length):
        """ Display the percentage of packages downloaded in a repository. """
        numpkgs = self.repos[repo_id]['numpkgs']
        dlpkgs = self.repos[repo_id]['repomd']
        return self.represent_percent(dlpkgs, numpkgs, length)

    def represent_total_percent(self, length):
        """ Display the overall percentage of downloaded packages. """
        numpkgs = self.totals['numpkgs']
        dlpkgs = self.totals['dlpkgs']
        return self.represent_percent(dlpkgs, numpkgs, length)

    def represent_total_metadata_percent(self, length):
        """ Display the overall percentage of metadata completion. """
        a = self.totals['md_total']
        b = self.totals['md_complete']
        return self.represent_percent(b, a, length)

    def represent_percent(self, dlpkgs, numpkgs, length):
        """ Display a percentage of completion.

        If the number of packages is unknown, nothing is displayed. Otherwise,
        a number followed by the percent sign is displayed.
        """
        if dlpkgs == 0:
            return '{:^{}s}'.format('-', length)
        else:
            return '{:^{}s}'.format(self.pct(dlpkgs, numpkgs), length)

    def represent_repomd(self, repo_id, length):
        """ Display the current status of repository metadata. """
        if not self.repos[repo_id]['repomd']:
            return '{:^{}s}'.format('-', length)
        elif isinstance(self.repos[repo_id]['repomd'], str):
            return self.repos[repo_id]['repomd']
        elif isinstance(self.repos[repo_id]['repomd'], int):
            return self.represent_repomd_percent(repo_id, length)

    def represent_repo(self, repo_id, h1, h2, h3, h4, h5):
        """ Represent an entire repository in one line.

        This makes calls to the other methods of this class to create a
        formatted string, which makes nice columns.
        """

        repo = '{:<{}s}'.format(repo_id, h1)

        if 'error' in self.repos[repo_id]:
            repo = self.color(repo, 'red')
        else:
            repo = self.color(repo, 'blue')

        packages = self.represent_repo_pkgs(repo_id, h2, h3)
        percent = self.represent_repo_percent(repo_id, h4)
        metadata = self.represent_repomd(repo_id, h5)
        if percent == 'complete':
            percent = self.color(percent, 'green')
        if metadata == 'building' or ((isinstance(metadata, int) or metadata.isdigit()) and int(metadata) <= 100):
            if isinstance(metadata, str) and metadata.isdigit():
                metadata += "%"
            if isinstance(metadata, int):
                metadata = "{}%".format(metadata)
            metadata = self.color(metadata, 'yellow')
        elif metadata == 'complete':
            metadata = self.color(metadata, 'green')
        return self.format_line(repo, packages, percent, metadata)

    def represent_total(self, h1, h2, h3, h4, h5):
        total = self.color('{:>{}s}'.format('Total', h1), 'yellow')
        packages = self.represent_total_pkgs(h2, h3)
        percent = self.represent_total_percent(h4)
        metadata = self.represent_total_metadata_percent(h5)
        if percent == 'complete':
            percent = self.color(percent, 'green')
        if metadata == 'complete':
            metadata = self.color(metadata, 'green')

        return self.format_line(total, packages, percent, metadata)

    def emit(self, line=''):
        numlines = len(line.split('\n'))
        self.linecount += numlines
        with self.term.location(x=0, y=self.linecount - numlines):
            sys.stdout.write(line)
            sys.stdout.write(self.term.clear_eol())

    def formatted(self):
        """ Print all known progress data in a nicely formatted table.

        This method keeps track of what it has printed before, so that it can
        backtrack over the console screen, clearing out the previous flush and
        printing out a new one. This method is called any time any value is
        updated, which is what gives us that real-time feeling.

        Unfortunately, the YUM library calls print directly rather than just
        throwing exceptions and handling them in the presentation layer, so
        this means that yumsync's output will be slightly flawed if YUM prints
        something directly to the screen from a worker process.
        """

        # Remove repos with errors from totals
        if self.totals['errors'] > 0:
            for repo_id, error in self.errors:
                if repo_id in self.repos:
                    if not 'error' in self.repos[repo_id]:
                        self.totals['dlpkgs'] -= self.repos[repo_id]['dlpkgs']
                        self.totals['numpkgs'] -= self.repos[repo_id]['numpkgs']
                        self.repos[repo_id]['error'] = True

        self.linecount = 0  # reset line counter
        header, h1, h2, h3, h4, h5 = self.format_header()
        self.emit('-' * len(header))
        self.emit(self.color('{}'.format(header), 'green'))
        self.emit('-' * len(header))

        error_repos = []
        complete_repos = []
        metadata_repos = []
        other_repos = []

        for repo_id in sorted(self.repos):
            if 'error' in self.repos[repo_id]:
                error_repos.append(repo_id)
            elif self.repos[repo_id]['repomd'] == 'complete':
                complete_repos.append(repo_id)
            elif self.repos[repo_id]['repomd']:
                metadata_repos.append(repo_id)
            else:
                other_repos.append(repo_id)

        for repo_id in sorted(self.repos):
            self.emit(self.represent_repo(repo_id, h1, h2, h3, h4, h5))

        self.emit('-' * len(header))
        self.emit(self.represent_total(h1, h2, h3, h4, h5))
        self.emit('-' * len(header))

        # Append errors to output if any found.
        if self.totals['errors'] > 0:
            self.emit(self.color('Errors ({}, last 20 are printed here):'.format(self.totals['errors']), 'red'))
            for repo_id, error in self.errors[-20:]:
                self.emit(self.color('{}: {}'.format(repo_id, error), 'red'))

        with self.term.location(x=0, y=self.linecount):
            sys.stdout.write(self.term.clear_eos())

        sys.stdout.flush()

class YumProgress(object):
    """ Creates an object for passing to YUM for status updates.

    YUM allows you to pass in your own callback object, which urlgrabber will
    use directly by calling some methods on it. Here we have an object that can
    be prepared with a repository ID, so that we can know which repository it
    is that is making the calls back.
    """
    def __init__(self, repo_id, queue, usercallback):
        """ Create the instance and set prepared config """
        self.repo_id = repo_id
        self.queue = queue
        self.package = None
        self.usercallback = usercallback

    def callback(self, method, *args):
        """ Abstracted callback function to reduce boilerplate.

        This is actually quite useful, because it checks that the method exists
        on the callback object before trying to invoke it, making all methods
        optional.
        """
        logger.debug('{}: Got callback {}({})'.format(type(self), method, args))
        if self.usercallback and hasattr(self.usercallback, method):
            method = getattr(self.usercallback, method)
            try:
                method(self.repo_id, *args)
            except:
                pass

    def start(self, filename=None, url=None, basename=None, size=None, text=None):
        """ Called by urlgrabber when a file download starts.

        All we use this for is storing the name of the file being downloaded so
        we can check that it is an RPM later on.
        """
        if basename:
            self.package = basename
            self.callback('download_start', filename, url, basename, size, text)

    def update(self, size):
        """ Called during the course of a download.

        Yumsync does not use this for anything, but we'll be a good neighbor and
        pass the data on to the user callback.
        """
        self.callback('download_update', size)

    def end(self, size):
        """ Called by urlgrabber when it completes a download.

        Here we have to check the file name saved earlier to make sure it is an
        RPM we are getting the event for.
        """
        if self.package.endswith('.rpm'):
            self.queue.put({'repo_id':self.repo_id, 'action':'download_end', 'data':[1]})
        self.callback('download_end', self.package, size)

class ProgressCallback(object):
    """ Register our own callback for progress indication.

    This class allows yumsync to stuff a user callback into an object before
    forking a thread, so that we don't have to keep making calls to multiple
    callbacks everywhere.
    """
    def __init__(self, queue, usercallback):
        """ Create a new progress object.

        This method allows the main process to pass its multiprocessing.Queue()
        object in so we can talk back to it.
        """
        self.queue = queue
        self.usercallback = usercallback

    def callback(self, repo_id, event, *args):
        """ Abstracts calling the user callback. """
        logger.debug('{}: Got event for repo {}: {}'.format(type(self), repo_id, event))
        if self.usercallback and hasattr(self.usercallback, event):
            method = getattr(self.usercallback, event)
            try:
                method(repo_id, *args)
            except:
                pass

    def send(self, repo_id, action, *args):
        """ Send an event to the main queue for processing.

        This gives us the ability to pass data back to the parent process,
        which is mandatory to do aggregated progress indication. This method
        also calls the user callback, if any is defined.
        """
        self.queue.put({'repo_id': repo_id, 'action': action, 'data': args})
        self.callback(repo_id, action, *args)

    def repo_metadata(self, repo_id, status):
        """ Update the status of metadata creation. """
        self.send(repo_id, 'repo_metadata', status)

    def repo_group_data(self, repo_id, status):
        """ Update the status of group data creation. """
        self.send(repo_id, 'repo_group_data', status)

    def repo_init(self, repo_id, numpkgs, islocal=False):
        """ Share the total packages in a repository, when known. """
        self.send(repo_id, 'repo_init', numpkgs, islocal)

    def gpgkey_exists(self, repo_id, keyname):
        """ Called when a gpg key already exists """
        self.send(repo_id, 'gpgkey_exists', keyname)

    def gpgkey_download(self, repo_id, keyname):
        """ Called when a gpg key is downloaded """
        self.send(repo_id, 'gpgkey_download', keyname)

    def gpgkey_error(self, repo_id, error):
        """ Called when a gpg key has an error """
        self.send(repo_id, 'gpgkey_error', error)

    def repo_link_set(self, repo_id, link_type, target):
        """ Called when a repo link is created """
        self.send(repo_id, 'repo_link_set', link_type, target)

    def repo_complete(self, repo_id):
        """ Called when a repository completes downloading all packages. """
        self.send(repo_id, 'repo_complete')

    def repo_error(self, repo_id, error):
        """ Called when a repository throws an exception. """
        self.send(repo_id, 'repo_error', error)

    def pkg_exists(self, repo_id, pkgname):
        """ Called when a download will be skipped because it already exists """
        self.send(repo_id, 'pkg_exists', pkgname)

    def delete_pkg(self, repo_id, pkgname):
        """ Called when a package is deleted from a repository """
        self.send(repo_id, 'delete_pkg', pkgname)

    def link_local_pkg(self, repo_id, pkgname, size):
        """ Called when a package is linked from a local repository """
        self.send(repo_id, 'link_local_pkg', pkgname, size)

class DownloadProgress(dnf.callback.DownloadProgress):
    def __init__(self, callback, repoid):
        self.callback = callback
        self.repoid = repoid

    def start(self, total_files, total_size, total_drpms=0):
        logger.debug("Starting to download a batch of {} files of repo '{}'".format(total_files, self.repoid))
        self.callback('repo_init', total_files)

    def progress(self, payload, done):
        pass

    def end(self, payload, status, msg):
        file_name = payload.__str__()
        states = {
            2: "STATUS_ALREADY_EXISTS",
            4: "STATUS_DRPM",
            1: "STATUS_FAILED",
            3: "STATUS_MIRROR",
            None: "STATUS_OK"
        }
        logging.debug("Done downloading {} of repo '{}', status={}".format(file_name, self.repoid, states.get(status, status)))
        if status == dnf.callback.STATUS_OK:
            logger.debug("Successfully downloaded {}".format(file_name))
            self.callback('pkg_exists', file_name)
        else:
            if status != dnf.callback.STATUS_FAILED:
                logger.debug("Download status of {} is {}: {}".format(file_name, status, msg))
            else:
                logger.error("Download of {} errored: {}".format(file_name, status, msg))
