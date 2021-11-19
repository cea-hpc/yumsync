# standard imports
from contextlib import contextmanager

try:
    from urllib2 import urlopen
except ImportError:
    # Python3
    from urllib.request import urlopen

try:
    import urlparse
except ImportError:
    # Python3
    from urllib.parse import urlparse

import copy
import os
import bisect
from fnmatch import fnmatch
import shutil
import sys
import tempfile
import time
# third-party imports
import createrepo_c as createrepo
import dnf, libdnf, rpm
import six
import yumsync.util as util
import logging

from yumsync import progress

from threading import Lock

class MetadataBuildError(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)

class PackageDownloadError(Exception):
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)

class YumRepo(object):

    def __init__(self, repoid, base_dir, opts=None):
        # make sure good defaults
        if opts is None:
            opts = {}
        opts = self._set_default_opts(opts)
        self._validate_opts(opts)
        self._validate_type(base_dir, 'base_dir', str)

        self.id = repoid
        self.checksum = opts['checksum']
        self.combine = opts['combined_metadata'] if opts['version'] else None
        self.delete = opts['delete']
        self.gpgkey = opts['gpgkey']
        self.link_type = opts['link_type']
        self.local_dir = opts['local_dir']
        self.baseurl = opts['baseurl']
        self.mirrorlist = opts['mirrorlist']
        self.incl_pkgs = opts['includepkgs']
        self.excl_pkgs = opts['excludepkgs']
        self.stable = opts['stable']
        self.version = time.strftime(opts['version']) if opts['version'] else None
        self.srcpkgs = opts['srcpkgs']
        self.newestonly = opts['newestonly']
        self.labels = opts['labels']
        self._dnfcache_file = util.TemporaryDirectory(prefix='yumsync-', suffix='-dnfcache')
        self._dnfcache = self._dnfcache_file.name

        # root directory for repo and packages
        self.dir = os.path.join(base_dir, self._friendly(self.id))
        self.package_dir = os.path.join(self.dir, 'packages')
        # version directory for repo and packages
        self.version_dir = os.path.join(self.dir, self.version) if self.version else None
        self.version_package_dir = os.path.join(self.version_dir, 'packages') if self.version_dir else None
        # log directory for repo
        self.log_dir = self.version_dir if self.version_dir else self.dir
        # public directroy for repo
        self.public_dir = os.path.join(base_dir, 'public', self._sanitize(self.id))

        # set default callbacks
        self.__repo_callback_obj = None
        self.__yum_callback_obj = None

        # set repo placeholders
        self._packages = []
        self._package_headers = {}
        self._comps = None
        self._repomd = None

    def setup(self):
        # set actual repo object
        self.__repo_obj = self._get_repo_obj(self.id, self.local_dir, self.baseurl, self.mirrorlist)
        self.__repo_obj.includepkgs = self.incl_pkgs
        self.__repo_obj.excludepkgs = self.excl_pkgs

    @staticmethod
    def _validate_type(obj, obj_name, *obj_types):
        valid_types = list(obj_types)
        if obj_name is None:
            obj_name = 'object'
        if len(valid_types) < 1:
            raise ValueError('no valid types were passed in for {}'.format(obj_name))
        if None in obj_types:
            valid_types.remove(None)
            valid_types.append(type(None))
        if not isinstance(obj, tuple(valid_types)):
            valid_str = ', '.join([t.__name__ for t in valid_types])
            raise TypeError('{} is {}; must be {}'.format(obj_name, type(obj).__name__, valid_str))

    @staticmethod
    def _validate_url(url):
        if not (url.startswith('http://') or url.startswith('https://') or url.startswith('file://')):
            raise ValueError('Unsupported URL format "{}"'.format(url))

    @staticmethod
    def _set_default_opts(opts=None):
        if not isinstance(opts, dict):
            opts = {}
        if 'baseurl' not in opts:
            opts['baseurl'] = None
        if 'checksum' not in opts:
            opts['checksum'] = None
        if 'combined_metadata' not in opts:
            opts['combined_metadata'] = None
        if 'delete' not in opts:
            opts['delete'] = None
        if 'excludepkgs' not in opts:
            opts['excludepkgs'] = None
        if 'gpgkey' not in opts:
            opts['gpgkey'] = None
        if 'includepkgs' not in opts:
            opts['includepkgs'] = None
        if 'link_type' in opts and isinstance(opts['link_type'], str):
            opts['link_type'] = opts['link_type'].lower()
        if 'link_type' not in opts or (opts['link_type'] != 'symlink' and opts['link_type'] != 'hardlink' and opts['link_type'] != 'individual_symlink'):
            opts['link_type'] = 'symlink'
        if 'local_dir' not in opts:
            opts['local_dir'] = None
        if 'mirrorlist' not in opts:
            opts['mirrorlist'] = None
        if 'stable' not in opts:
            opts['stable'] = None
        if not isinstance(opts['stable'], str) and opts['stable'] is not None:
            opts['stable'] = str(opts['stable'])
        if 'version' not in opts:
            opts['version'] = '%Y/%m/%d'
        if 'srcpkgs' not in opts:
            opts['srcpkgs'] = None
        if 'newestonly' not in opts:
            opts['newestonly'] = None
        if 'labels' not in opts:
            opts['labels'] = {}
        return opts

    @classmethod
    def _validate_opts(cls, opts):
        cls._validate_type(opts['baseurl'], 'baseurl', str, None)
        if isinstance(opts['baseurl'], str):
            cls._validate_url(opts['baseurl'])
        cls._validate_type(opts['checksum'], 'checksum', str, None)
        cls._validate_type(opts['combined_metadata'], 'combined_metadata', bool, None)
        cls._validate_type(opts['delete'], 'delete', bool, None)
        cls._validate_type(opts['excludepkgs'], 'excludepkgs', str, list, None)
        if isinstance(opts['excludepkgs'], list):
            for e in opts['excludepkgs']:
                cls._validate_type(e, 'excludepkgs (in list)', str)
        cls._validate_type(opts['gpgkey'], 'gpgkey', str, list, None)
        if isinstance(opts['gpgkey'], list):
            for g in opts['gpgkey']:
                cls._validate_type(g, 'gpgkey (in list)', str)
                cls._validate_url(g)
        elif opts['gpgkey'] is str:
            cls._validate_url(opts['gpgkey'])
        cls._validate_type(opts['includepkgs'], 'includepkgs', str, list, None)
        if isinstance(opts['includepkgs'], list):
            for i in opts['includepkgs']:
                cls._validate_type(i, 'includepkgs (in list)', str)
        cls._validate_type(opts['link_type'], 'link_type', str)
        cls._validate_type(opts['local_dir'], 'local_dir', str, list, None)
        cls._validate_type(opts['mirrorlist'], 'mirrorlist', str, None)
        if opts['mirrorlist'] is not None:
            cls._validate_url(opts['mirrorlist'])
        cls._validate_type(opts['stable'], 'stable', str, None)
        cls._validate_type(opts['version'], 'version', str, None)
        cls._validate_type(opts['srcpkgs'], 'srcpkgs', bool, None)
        cls._validate_type(opts['newestonly'], 'newestonly', bool, None)
        cls._validate_type(opts['labels'], 'labels', dict)
        for label, value in six.iteritems(opts['labels']):
            cls._validate_type(label, 'label_name_{}'.format(label), str)
            cls._validate_type(value, 'label_value_{}'.format(label), str)

    @staticmethod
    def _sanitize(text):
        return text.strip().strip('/')

    @classmethod
    def _friendly(cls, text):
        return cls._sanitize(text).replace('/', '_')

    def _get_repo_obj(self, repoid, localdir=None, baseurl=None, mirrorlist=None):
        base = dnf.Base()
        base.conf.cachedir = self._dnfcache
        base.conf.debuglevel = 0
        base.conf.errorlevel = 3
        repo = dnf.repo.Repo(repoid.replace('/', '_'), base.conf)
        repo.baseurl = None
        repo.metalink = None
        repo.mirrorlist = None
        repo.module_hotfixes = True

        if baseurl is not None:
            repo.baseurl = baseurl
        elif mirrorlist is not None:
            repo.mirrorlist = mirrorlist
        elif (localdir, baseurl, mirrorlist) is (None, None, None):
            raise ValueError('One or more baseurls, mirrorlist or localdir required')
        repo.enable()
        return repo

    def set_repo_callback(self, callback):
        self.__repo_callback_obj = callback

    def set_yum_callback(self, callback):
        self.__yum_callback_obj = callback

    def _set_path(self, path):
        repo = copy.copy(self.__repo_obj)
        try:
            repo.pkgdir = path
        except dnf.RepoError:
            pass
        return repo

    def setup_directories(self):
        if self.local_dir and self.link_type == 'symlink':
            if not os.path.islink(self.package_dir) and os.path.isdir(self.package_dir):
                shutil.rmtree(self.package_dir)

            assert isinstance(self.local_dir, (list, str))
            if isinstance(self.local_dir, list):
                for idx, local_dir in enumerate(self.local_dir):
                    subdir = os.path.join(self.package_dir, "repo_{}".format(idx))
                    util.symlink(subdir, local_dir)
            elif isinstance(self.local_dir, str):
                util.symlink(self.package_dir, self.local_dir)
        else:
            if os.path.islink(self.package_dir):
                os.unlink(self.package_dir)
            util.make_dir(self.package_dir)

        if self.version_dir:
            if os.path.islink(self.version_package_dir) or os.path.isfile(self.version_package_dir):
                os.unlink(self.version_package_dir)
            elif os.path.isdir(self.version_package_dir):
                shutil.rmtree(self.version_package_dir)
            if self.link_type == 'symlink':
                util.symlink(self.version_package_dir, os.path.relpath(self.package_dir, self.version_dir))
            elif self.link_type == 'individual_symlink':
                util.make_dir(self.version_package_dir)
                if isinstance(self.local_dir, list):
                    dirs = self.local_dir
                    single = False
                elif isinstance(self.local_dir, str):
                    dirs = [self.local_dir]
                    single = True
                for idx, _dir in enumerate(dirs):
                    pkg_dir = self.version_package_dir
                    if not single:
                        pkg_dir = os.path.join(self.version_package_dir, "repo_{}".format(idx))
                        util.make_dir(os.path.join(pkg_dir))
                    for _file in self._find_rpms(_dir):
                        util.symlink(os.path.join(pkg_dir, _file), os.path.join(_dir, _file))
            else: # hardlink
                util.make_dir(self.version_package_dir)

    def download_gpgkey(self):
        if self.gpgkey:
            gpgkey_paths = []
            if isinstance(self.gpgkey, list):
                gpgkey_iter = self.gpgkey
            else:
                gpgkey_iter = [self.gpgkey]
            for gpgkey in gpgkey_iter:
                try:
                    keyname = os.path.basename(urlparse(gpgkey).path)
                    key_path = os.path.join(self.dir, keyname)
                    if not os.path.exists(key_path):
                        key_data = urlopen(gpgkey)
                        with open(key_path, 'w') as f:
                            f.write(key_data.read())
                        key_data.close()
                        self._callback('gpgkey_download', os.path.basename(key_path))
                    else:
                        self._callback('gpgkey_exists', os.path.basename(key_path))
                    gpgkey_paths.append(key_path)
                except Exception as e:
                    self._callback('gpgkey_error', str(e))
            return gpgkey_paths
        return None

    def prepare_packages(self):
        self.download_packages()
        self.prune_packages()
        self.version_packages()

    def download_packages(self):
        if self.local_dir:
            self._download_local_packages()
        else:
            self._download_remote_packages()

    def _validate_packages(self, directory, packages):
        if hasattr(rpm, "RPMVSF_MASK_NOSIGNATURES"):
            no_signature_check_mask = rpm.RPMVSF_MASK_NOSIGNATURES
        else:
            no_signature_check_mask = rpm.RPMVSF_NODSAHEADER | rpm.RPMVSF_NORSAHEADER | rpm.RPMVSF_NODSA | rpm.RPMVSF_NORSA
        ts = rpm.TransactionSet("/", no_signature_check_mask)
        if isinstance(packages, str):
            self._callback('pkg_exists', packages)
            package, hdr = self._validate_package(ts, directory, packages)
            if hdr:
                return package, hdr
            return package, None
        elif isinstance(packages, list):
            valid = []
            for pkg in packages:
                package, hdr = self._validate_package(ts, directory, pkg)
                if hdr:
                    valid.append((package, hdr))
                    self._callback('pkg_exists', pkg)
            return valid
        else:
            return None

    @staticmethod
    def _validate_package(ts, directory, package):
        try:
            pkg_path = os.path.join(directory, package)
            with open(pkg_path, 'rb') as pkg:
                return package, ts.hdrFromFdno(pkg)
        except:
            return package, None

    def _find_rpms(self, local_dir):
        matches = []
        progress_counter = 0
        include_globs = []
        exclude_globs = []

        if self.__repo_obj.includepkgs is not None:
            if isinstance(self.__repo_obj.includepkgs, list) or isinstance(self.__repo_obj.includepkgs, libdnf.module.VectorString):
                include_globs = self.__repo_obj.includepkgs
            elif isinstance(self.__repo_obj.includepkgs, str):
                include_globs = [self.__repo_obj.includepkgs]
        if self.__repo_obj.excludepkgs is not None:
            if isinstance(self.__repo_obj.excludepkgs, list) or isinstance(self.__repo_obj.excludepkgs, libdnf.module.VectorString):
                exclude_globs = self.__repo_obj.excludepkgs
            elif isinstance(self.__repo_obj.excludepkgs, str):
                exclude_globs = [self.__repo_obj.excludepkgs]

        for root, dirnames, filenames in os.walk(local_dir, topdown=False, followlinks=True):
            for filename in filenames:
                if not filename.endswith('.rpm'):
                    continue
                skip = False
                keep = True
                for glob in exclude_globs:
                    if fnmatch(filename, glob):
                        skip = True
                        break
                if skip:
                    continue
                for glob in include_globs:
                    if fnmatch(filename, glob):
                        keep = True
                        break
                    else:
                        keep = False
                if not keep:
                    continue
                bisect.insort(matches, os.path.relpath(os.path.join(root, filename), local_dir))

        return matches

    def _download_local_packages(self):
        try:
            packages = {}
            nb_packages = 0
            self._callback('repo_init', nb_packages, True)
            if isinstance(self.local_dir, str):
                files = self._find_rpms(self.local_dir)
                packages = {(None, self.local_dir): self._validate_packages(self.local_dir, files)}
                nb_packages += len(packages[(None, self.local_dir)])
            elif isinstance(self.local_dir, list):
                packages = {}
                files = {}
                for idx, local_dir in enumerate(self.local_dir):
                    files[(idx, local_dir)] = self._find_rpms(local_dir)
                    nb_packages += len(files[(idx,local_dir)])
                    self._callback('repo_init', nb_packages, True)
                for local_dir_idx, rpm_files in six.iteritems(files):
                    packages[local_dir_idx] = self._validate_packages(local_dir_idx[1], rpm_files)
            self._callback('repo_init', nb_packages, True)

            for _dir, _files in six.iteritems(packages):
                for _file, _hdr in _files:
                    if _dir[0] is not None and isinstance(_dir[0], int):
                        package_dir = os.path.join(self.package_dir, "repo_{}".format(_dir[0]))
                        file_path = os.path.join("repo_{}".format(_dir[0]), _file)
                    else:
                        package_dir = self.package_dir
                        file_path = _file
                    self._packages.append(file_path)
                    self._package_headers[file_path] = _hdr
                    if self.link_type == 'hardlink':
                        status = util.hardlink(os.path.join(_dir[1], _file), os.path.join(package_dir, _file))
                        if status:
                            size = os.path.getsize(os.path.join(_dir[1], _file))
                            self._callback('link_local_pkg', _file, size)

            self._callback('repo_complete')
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            self._callback('repo_error', str(e))
            raise PackageDownloadError(str(e))

    def _download_remote_packages(self):
        self._callback('repo_init', 0, True)
        yb = dnf.Base()
        yb.conf.cachedir = self._dnfcache
        yb.conf.debuglevel = 0
        yb.conf.errorlevel = 3
        repo = self._set_path(self.package_dir)
        yb.repos.add(repo)
        yb.fill_sack()
        p_query = yb.sack.query().available()
        if self.newestonly:
            p_query = p_query.latest()
        packages = list(p_query)
        # Inform about number of packages total in the repo.
        # Check if the packages are already downloaded. This is probably a bit
        # expensive, but the alternative is simply not knowing, which is
        # horrible for progress indication.
        if packages:
            self._callback('repo_init', len(packages), True)
            for po in packages:
                local = po.localPkg()
                self._packages.append(os.path.basename(local))
                if os.path.exists(local):
                    self._callback('pkg_exists', os.path.basename(local))
            try:
                yb.download_packages(packages, progress=progress.DownloadProgress(self._callback))
            except (KeyboardInterrupt, SystemExit):
                return
            except dnf.exceptions.DownloadError as e:
                self._callback('repo_error', str(e))
            except Exception as e:
                self._callback('repo_error', str(e))
                raise PackageDownloadError(str(e))
        self._callback('repo_complete')

    def deduplicate_rpm(self):
        nevra_index = set()
        print(len(self._packages))
        for pkg_path, pkg_header in six.iteritems(self._package_headers):
            if pkg_header is not None:
                nevra = "{}-{}-{}-{}.{}".format(
                    pkg_header['name'],
                    pkg_header['epoch'],
                    pkg_header['version'],
                    pkg_header['release'],
                    pkg_header['arch'])
                if nevra in nevra_index:
                    self._packages.remove(pkg_path)
                else:
                    nevra_index.add(nevra)

    def prune_packages(self):
        # exit if we don't have packages
        if not self._packages or len(self._packages) == 0:
            return
        packages_filenames = list(map(lambda x: os.path.basename(x), self._packages))
        unique_filenames = set(packages_filenames)
        if len(unique_filenames) != len(packages_filenames):
            # Some RPMs have the same names, prune them
            self.deduplicate_rpm()

        if self.delete:
            if not self.version or (self.link_type != 'symlink' and self.link_type != 'individual_symlink'):
                for _file in os.listdir(self.package_dir):
                    if _file not in self._packages:
                        os.unlink(os.path.join(self.package_dir, _file))
                        self._callback('delete_pkg', _file)
        else:
            packages_to_validate = sorted(list(set(os.listdir(self.package_dir)) - set(self._packages)))
            self._packages.extend(self._validate_packages(self.package_dir, packages_to_validate))

    def version_packages(self):
        # exit if we don't have packages
        if not self._packages or len(self._packages) == 0:
            return
        if self.version and self.link_type == 'hardlink':
            for pkg in self._packages:
                source_file = os.path.join(self.package_dir, pkg)
                target_file = os.path.join(self.version_package_dir, pkg)
                util.hardlink(source_file, target_file)

    def get_md_data(self):
        if self.local_dir:
            # If it's a local_dir, don't bother merging metadata.
            # Only pick the first available metadata (if any)
            self._repomd = {
                ("modules", "modules.yaml"): "",
                ("group", "comps.xml"): "",
            }
            repomds = []
            if isinstance(self.local_dir, str):
                repo_dirs = [ self.local_dir ]
            elif isinstance(self.local_dir, list):
                repo_dirs = [ l for l in self.local_dir ]
            for repo_dir in repo_dirs:
                if not os.path.exists(os.path.join(repo_dir, 'repodata')):
                    continue
                yb = dnf.Base()
                yb.conf.cachedir = self._dnfcache
                yb.conf.debuglevel = 0
                yb.conf.errorlevel = 3
                repo = dnf.repo.Repo("yumsync_temp_md_repo", dnf.Base().conf)
                repo.metalink = None
                repo.mirrorlist = None
                repo.baseurl = "file://{}".format(repo_dir)
                yb.repos.add(repo)
                yb.fill_sack()
                repomds.append({
                    ("modules", "modules.yaml"): repo.get_metadata_content('modules'),
                    ("group", "comps.xml"): repo.get_metadata_content('group_gz'),
                })
            # Combine modular MD of each repo
            # Only keep the first "comps" we find
            for repomd in repomds:
                self._repomd[("modules", "modules.yaml")] += repomd.get(("modules", "modules.yaml"), "")
                if self._repomd.get(("group", "comps.xml"), "") == "":
                    self._repomd[("group", "comps.xml")] = repomd.get(("group", "comps.xml"), "")
        else:
            self._repomd = {
                ("modules", "modules.yaml"): self.__repo_obj.get_metadata_content('modules'),
                ("group", "comps.xml"): self.__repo_obj.get_metadata_content('group_gz'),
            }

        if self._repomd:
            # Filter out empty metadata
            for k, v in six.iteritems(self._repomd.copy()):
                if len(v) != 0:
                    continue
                self._repomd.pop(k)
            self._callback('repo_group_data', 'available')
        else:
            self._callback('repo_group_data', 'unavailable')

    def build_metadata(self):
        staging = tempfile.mkdtemp(prefix='yumsync-', suffix='-metadata')

        if self.checksum == 'sha' or self.checksum == 'sha1':
            sumtype = 'sha'
        else:
            sumtype = 'sha256'

        repodata_path = os.path.join(staging, 'repodata')
        os.mkdir(repodata_path)

        # Prepare metadata files
        repomd_path  = os.path.join(repodata_path, "repomd.xml")
        pri_xml_path = os.path.join(repodata_path, "primary.xml.gz")
        fil_xml_path = os.path.join(repodata_path, "filelists.xml.gz")
        oth_xml_path = os.path.join(repodata_path, "other.xml.gz")
        pri_db_path  = os.path.join(repodata_path, "primary.sqlite")
        fil_db_path  = os.path.join(repodata_path, "filelists.sqlite")
        oth_db_path  = os.path.join(repodata_path, "other.sqlite")

        # Related python objects
        pri_xml = createrepo.PrimaryXmlFile(pri_xml_path)
        fil_xml = createrepo.FilelistsXmlFile(fil_xml_path)
        oth_xml = createrepo.OtherXmlFile(oth_xml_path)
        pri_db  = createrepo.PrimarySqlite(pri_db_path)
        fil_db  = createrepo.FilelistsSqlite(fil_db_path)
        oth_db  = createrepo.OtherSqlite(oth_db_path)

        # Set package list
        if self.local_dir and self.link_type == "individual_symlink" and self.version_dir:
            pkg_list = [
                (
                    os.path.join(self.version_package_dir,"{}".format(pkg)),
                    os.path.join("packages","{}".format(pkg))
                ) for pkg in self._packages]
        else:
            pkg_list = [
                (
                    os.path.join(self.package_dir,"{}".format(pkg)),
                    os.path.join("packages","{}".format(pkg))
                )for pkg in self._packages]

        pri_xml.set_num_of_pkgs(len(pkg_list))
        fil_xml.set_num_of_pkgs(len(pkg_list))
        oth_xml.set_num_of_pkgs(len(pkg_list))

        # Process all packages in // if possible
        self.metadata_progress = 0
        self.total_pkgs = len(pkg_list)
        metadata_mutex = Lock()

        def collect_result(future):
            self.metadata_progress += 1
            self._callback('repo_metadata', int((self.metadata_progress+1)*100//self.total_pkgs))

        def process_pkg(filename, href):
            pkg = createrepo.package_from_rpm(filename)
            pkg.location_href = href
            return pkg

        try:
            from concurrent.futures import ThreadPoolExecutor
            parallelize = True
        except:
            parallelize = False

        if parallelize:
            with ThreadPoolExecutor(max_workers=self._workers) as executor:
                futures = []
                for filename in pkg_list:
                    future = executor.submit(process_pkg, filename[0], filename[1])
                    future.add_done_callback(collect_result)
                    futures.append(future)
                for future in futures:
                    try:
                        pkg = future.result(10)
                    except Exception as exc:
                        logging.exception("Thread generated an exception")
                    else:
                        pri_xml.add_pkg(pkg)
                        fil_xml.add_pkg(pkg)
                        oth_xml.add_pkg(pkg)
                        pri_db.add_pkg(pkg)
                        fil_db.add_pkg(pkg)
                        oth_db.add_pkg(pkg)
        else:
            for idx, filename in enumerate(pkg_list):
                process_pkg(filename[0], filename[1])
                collect_result(None)

        pri_xml.close()
        fil_xml.close()
        oth_xml.close()

        # Note: DBs are still open! We have to calculate checksums of xml files
        # and insert them to the databases first!

        self._callback('repo_metadata', 'building')
        # Prepare repomd.xml
        repomd = createrepo.Repomd()

        # Order is important !
        repomdrecords = (("primary",      pri_xml_path, pri_db, False),
                         ("filelists",    fil_xml_path, fil_db, False),
                         ("other",        oth_xml_path, oth_db, False),
                         ("primary_db",   pri_db_path,  None,   True),
                         ("filelists_db", fil_db_path,  None,   True),
                         ("other_db",     oth_db_path,  None,   True))

        for name, path, db_to_update, compress in repomdrecords:
            record = createrepo.RepomdRecord(name, path)
            if compress:
                record.compress_and_fill(createrepo.SHA256, createrepo.XZ_COMPRESSION)
            else:
                record.fill(createrepo.SHA256)

            if (db_to_update):
                db_to_update.dbinfo_update(record.checksum)
                db_to_update.close()

            repomd.set_record(record)

        if self._repomd:
            for md_type, md_content in six.iteritems(self._repomd):
                md_file = os.path.join(repodata_path, md_type[1])
                with open(md_file, 'w') as f:
                    f.write(md_content)
                record = createrepo.RepomdRecord(md_type[0], md_file)
                record.fill(createrepo.SHA256)
                repomd.set_record(record)

        open(repomd_path, "w").write(repomd.xml_dump())

        return staging

    def build_file_list(self):
        if os.path.exists(os.path.join(self.log_dir, 'filelist')):
            os.unlink(os.path.join(self.log_dir, 'filelist'))
        with open(os.path.join(self.log_dir, 'filelist'), 'w') as f:
            for pkg in self._packages:
                f.write('packages/{}\n'.format(pkg))


    def prepare_metadata(self):
        self.get_md_data()
        self._callback('repo_metadata', 'building')

        try:
            self.build_file_list()
            staging = self.build_metadata()
        except Exception as e:
            self._callback('repo_error', str(e))
            raise MetadataBuildError(str(e))

        repodata_dir = os.path.join(self.dir, 'repodata')
        if os.path.exists(repodata_dir):
            shutil.rmtree(repodata_dir)
        if not self.version or self.combine:
            shutil.copytree(os.path.join(staging, 'repodata'), repodata_dir)

        if self.version:
            repodata_dir = os.path.join(self.version_dir, 'repodata')
            if os.path.exists(repodata_dir):
                shutil.rmtree(repodata_dir)
            shutil.copytree(os.path.join(staging, 'repodata'), repodata_dir)

        # cleanup temporary metadata
        shutil.rmtree(staging)

        self._callback('repo_metadata', 'complete')

    def create_links(self):
        if self.version:
            util.symlink(os.path.join(self.dir, 'latest'), self.version)
            self._callback('repo_link_set', 'latest', self.version)
            if self.stable:
                util.symlink(os.path.join(self.dir, 'stable'), self.stable)
                self._callback('repo_link_set', 'stable', self.stable)
            elif os.path.lexists(os.path.join(self.dir, 'stable')):
                os.unlink(os.path.join(self.dir, 'stable'))
            for label, version in six.iteritems(self.labels):
                util.symlink(os.path.join(self.dir, label), version)
                self._callback('repo_link_set', label, version)

        else:
            if os.path.lexists(os.path.join(self.dir, 'latest')):
                os.unlink(os.path.join(self.dir, 'latest'))
            if os.path.lexists(os.path.join(self.dir, 'stable')):
                os.unlink(os.path.join(self.dir, 'stable'))

    def sync(self, workers=1):
        self.setup()
        self._workers = workers
        try:
            self.setup_directories()
            self.download_gpgkey()
            self.prepare_packages()
            self.prepare_metadata()
            self.create_links()
        except MetadataBuildError:
            self._callback('repo_error', 'MetadataBuildError')
            return False
        except PackageDownloadError:
            self._callback('repo_error', 'PackageDownloadError')
            return False

    def __str__(self):
        raw_info = {}
        if self.checksum:
            raw_info['checksum'] = self.checksum
        if self.combine is not None:
            raw_info['combine'] = self.combine
        if self.delete is not None:
            raw_info['delete'] = self.delete
        if self.gpgkey:
            raw_info['gpgkey'] = self.gpgkey
        if self.link_type:
            raw_info['link_type'] = self.link_type
        if self.local_dir:
            raw_info['local_dir'] = str(self.local_dir)
        if self.stable:
            raw_info['stable'] = self.stable
        if self.version:
            raw_info['version'] = self.version
        if self.srcpkgs is not None:
            raw_info['srcpkgs'] = self.srcpkgs
        if self.newestonly is not None:
            raw_info['newestonly'] = self.newestonly
        if self.labels is not []:
            raw_info['labels'] = str(self.labels)
        friendly_info = ['{}({})'.format(k, raw_info[k]) for k in sorted(raw_info)]
        return '{}: {}'.format(self.id, ', '.join(friendly_info))

    def _callback(self, event, *args):
        logging.debug("{}: Send event {} with args {}".format(type(self), event, args))
        if self.__repo_callback_obj and hasattr(self.__repo_callback_obj, event):
            method = getattr(self.__repo_callback_obj, event)
            method(self.id, *args)
