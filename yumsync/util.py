# yumsync - A tool for mirroring and versioning YUM repositories.
# Copyright 2013 Ryan Uber <ru@ryanuber.com>. All rights reserved.
#
# MIT LICENSE
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import dnf, os, sys, tempfile, shutil
import xml.etree.ElementTree

try:
    from weakref import finalize
except ImportError:
    from yumsync.backports import finalize


def make_dir(path):
    """ Create a directory recursively, if it does not exist. """
    if not os.path.exists(path):
        os.makedirs(path)

# path = path to symlink
# target = path to real file
def symlink(path, target):
    """ Create a symbolic link.
    Determines if a link in the destination already exists, and if it does,
    updates its target. If the destination exists but is not a link, throws an
    exception. If the link does not exist, it is created.
    """
    if not os.path.islink(path):
        if os.path.exists(path):
            raise Exception('%s exists - Cannot create symlink' % path)
        path_dir = os.path.dirname(path)
        if not os.path.exists(path_dir):
            make_dir(path_dir)
    elif os.readlink(path) != target:
        os.unlink(path)
    if os.path.lexists(path):
        return False
    else:
        os.symlink(target, path)
        return True

def hardlink(source, target):
    " This method creates a hardlink ... "
    if source != target:
        # ensure source exists and collect stats
        if not os.path.exists(source):
            raise Exception('%s does not exist - Cannot create hardlink' % source)
        else:
            source_stat = os.stat(source)
        # create target dir if missing
        target_dir = os.path.dirname(target)
        if not os.path.exists(target_dir):
            make_dir(target_dir)
        # if target is a symlink, unlink
        if os.path.islink(target):
            os.unlink(target)
        # get dev and inode info for target
        if os.path.exists(target):
            target_dev = os.stat(target).st_dev
            target_inode = os.stat(target).st_ino
        else:
            target_dev = os.stat(target_dir).st_dev
            target_inode = None
        if target_dev != source_stat.st_dev:
            raise Exception('source device %s is not equal to target device %s - Cannot create hardlink' %
                            (source_stat.st_dev, target_dev))
        elif (target_inode is not None) and (target_inode != source_stat.st_ino):
            os.unlink(target)

        if os.path.exists(target):
            return False
        else:
            os.link(source, target)
            return True

# Reused from python3 stdlib for Python2/Python3 compat
class TemporaryDirectory(object):
    """Create and return a temporary directory.  This has the same
    behavior as mkdtemp but can be used as a context manager.  For
    example:
        with TemporaryDirectory() as tmpdir:
            ...
    Upon exiting the context, the directory and everything contained
    in it are removed.
    """

    def __init__(self, suffix=None, prefix=None, dir=None):
        self.name = tempfile.mkdtemp(suffix, prefix, dir)
        self._finalizer = finalize(
            self, self._cleanup, self.name,
            warn_message="Implicitly cleaning up {!r}".format(self))

    @classmethod
    def _rmtree(cls, name):
        def onerror(func, path, exc_info):
            if issubclass(exc_info[0], PermissionError):
                def resetperms(path):
                    try:
                        os.chflags(path, 0)
                    except AttributeError:
                        pass
                    os.chmod(path, 0o700)

                try:
                    if path != name:
                        resetperms(os.path.dirname(path))
                    resetperms(path)

                    try:
                        os.unlink(path)
                    # PermissionError is raised on FreeBSD for directories
                    except (IsADirectoryError, PermissionError):
                        cls._rmtree(path)
                except FileNotFoundError:
                    pass
            elif issubclass(exc_info[0], FileNotFoundError):
                pass
            else:
                raise

        shutil.rmtree(name, onerror=onerror)

    @classmethod
    def _cleanup(cls, name, warn_message):
        cls._rmtree(name)

    def __repr__(self):
        return "<{} {!r}>".format(self.__class__.__name__, self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, exc, value, tb):
        self.cleanup()

    def cleanup(self):
        if self._finalizer.detach():
            self._rmtree(self.name)

def get_local_repo_metadata_content(repo_path, md_type):
    if not os.path.exists(os.path.join(repo_path, 'repodata')):
        return None
    with TemporaryDirectory(prefix='yumsync-get_local_repo_metadata_content', suffix='-dnfcache') as dnf_cache_file:
      yb = dnf.Base()
      yb.conf.cachedir = dnf_cache_file
      yb.conf.debuglevel = 9
      yb.conf.errorlevel = 9
      repo = dnf.repo.Repo("yumsync_temp_md_repo", dnf.Base().conf)
      repo.metalink = None
      repo.mirrorlist = None
      repo.baseurl = "file://{}".format(repo_path)
      yb.repos.add(repo)
      yb.fill_sack()
      data = repo.get_metadata_content(md_type)
    if data is None or len(data) == 0:
        return None
    else:
        return data

def merge_xml_content(xml_strings):
    # Remove empty/None items
    xml_strings = [ s for s in xml_strings if s ]

    # Return the input data if there is nothing to merge
    if len(xml_strings) == 0:
        return None
    if len(xml_strings) == 1:
        return xml_strings[0]

    # Parse first item as the base object...
    data = xml.etree.ElementTree.fromstring(xml_strings[0])

    # And extend it with the next ones
    for xml_string in xml_strings[1:]:
        data.extend(xml.etree.ElementTree.fromstring(xml_string))

    return xml.etree.ElementTree.tostring(data)
