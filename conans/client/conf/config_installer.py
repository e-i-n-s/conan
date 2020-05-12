import json
import os
import shutil
import stat
from datetime import datetime
from dateutil.tz import gettz

from contextlib import contextmanager
from six.moves.urllib.parse import urlparse

from conans import load
from conans.client import tools
from conans.client.cache.remote_registry import load_registry_txt,\
    migrate_registry_file
from conans.client.tools import Git
from conans.client.tools.files import unzip
from conans.errors import ConanException
from conans.util.files import mkdir, rmdir, walk, save, touch
from conans.client.cache.cache import ClientCache


def _hide_password(resource):
    """
    Hide password from url/file path

    :param resource: string with url or file path
    :return: resource with hidden password if present
    """
    password = urlparse(resource).password
    return resource.replace(password, "<hidden>") if password else resource


def _handle_remotes(cache, remote_file):
    # FIXME: Should we encourage to pass the remotes in json?
    remotes, _ = load_registry_txt(load(remote_file))
    cache.registry.define(remotes)


@contextmanager
def tmp_config_install_folder(cache):
    tmp_folder = os.path.join(cache.cache_folder, "tmp_config_install")
    # necessary for Mac OSX, where the temp folders in /var/ are symlinks to /private/var/
    tmp_folder = os.path.realpath(tmp_folder)
    rmdir(tmp_folder)
    mkdir(tmp_folder)
    try:
        yield tmp_folder
    finally:
        rmdir(tmp_folder)


def _process_git_repo(config, cache, output):
    output.info("Trying to clone repo: %s" % config.uri)
    with tmp_config_install_folder(cache) as tmp_folder:
        with tools.chdir(tmp_folder):
            try:
                args = config.args or ""
                git = Git(verify_ssl=config.verify_ssl, output=output)
                git.clone(config.uri, args=args)
                output.info("Repo cloned!")
            except Exception as e:
                raise ConanException("Can't clone repo: %s" % str(e))
        _process_folder(config, tmp_folder, cache, output)


def _process_zip_file(config, zippath, cache, output, tmp_folder, remove=False):
    unzip(zippath, tmp_folder, output=output)
    if remove:
        os.unlink(zippath)
    _process_folder(config, tmp_folder, cache, output)


def _handle_conan_conf(current_conan_conf, new_conan_conf_path):
    current_conan_conf.read(new_conan_conf_path)
    with open(current_conan_conf.filename, "w") as f:
        current_conan_conf.write(f)


def _filecopy(src, filename, dst):
    # https://github.com/conan-io/conan/issues/6556
    src = os.path.join(src, filename)
    dst = os.path.join(dst, filename)
    if os.path.exists(dst):
        os.chmod(dst, stat.S_IWRITE)  # make file writable
        os.remove(dst)
    shutil.copy(src, dst)


def _process_folder(config, folder, cache, output):
    if config.source_folder:
        folder = os.path.join(folder, config.source_folder)
    for root, dirs, files in walk(folder):
        dirs[:] = [d for d in dirs if d != ".git"]
        if ".git" in root:
            continue
        for f in files:
            if f == "settings.yml":
                output.info("Installing settings.yml")
                _filecopy(root, f, cache.cache_folder)
            elif f == "conan.conf":
                output.info("Processing conan.conf")
                _handle_conan_conf(cache.config, os.path.join(root, f))
            elif f == "remotes.txt":
                output.info("Defining remotes from remotes.txt")
                _handle_remotes(cache, os.path.join(root, f))
            elif f in ("registry.txt", "registry.json"):
                try:
                    os.remove(cache.remotes_path)
                except OSError:
                    pass
                finally:
                    _filecopy(root, f, cache.cache_folder)
                    migrate_registry_file(cache, output)
            elif f == "remotes.json":
                # Fix for Conan 2.0
                raise ConanException("remotes.json install is not supported yet. Use 'remotes.txt'")
            else:
                # This is ugly, should be removed in Conan 2.0
                if root == folder and f in ("README.md", "LICENSE.txt"):
                    output.info("Skip %s" % f)
                    continue
                relpath = os.path.relpath(root, folder)
                if config.target_folder:
                    target_folder = os.path.join(cache.cache_folder, config.target_folder,
                                                 relpath)
                else:
                    target_folder = os.path.join(cache.cache_folder, relpath)
                mkdir(target_folder)
                output.info("Copying file %s to %s" % (f, target_folder))
                _filecopy(root, f, target_folder)


def _process_download(config, cache, output, requester):
    with tmp_config_install_folder(cache) as tmp_folder:
        output.info("Trying to download  %s" % _hide_password(config.uri))
        zippath = os.path.join(tmp_folder, "config.zip")
        try:
            tools.download(config.uri, zippath, out=output, verify=config.verify_ssl,
                           requester=requester)
            _process_zip_file(config, zippath, cache, output, tmp_folder, remove=True)
        except Exception as e:
            raise ConanException("Error while installing config from %s\n%s" % (config.uri, str(e)))


class _ConfigOrigin(object):
    def __init__(self, data):
        self.type = data.get("type")
        self.uri = data.get("uri")
        self.verify_ssl = data.get("verify_ssl")
        self.args = data.get("args")
        self.source_folder = data.get("source_folder")
        self.target_folder = data.get("target_folder")

    def __eq__(self, other):
        return (self.type == other.type and self.uri == other.uri and
                self.args == other.args and self.source_folder == other.source_folder
                and self.target_folder == other.target_folder)

    def __ne__(self, other):
        return not self.__eq__(other)

    def json(self):
        return {"type": self.type,
                "uri": self.uri,
                "verify_ssl": self.verify_ssl,
                "args": self.args,
                "source_folder": self.source_folder,
                "target_folder": self.target_folder}

    @staticmethod
    def from_item(uri, config_type, verify_ssl, args, source_folder, target_folder):
        config = _ConfigOrigin({})
        if config_type:
            config.type = config_type
        else:
            if uri.endswith(".git"):
                config.type = "git"
            elif os.path.isdir(uri):
                config.type = "dir"
            elif os.path.isfile(uri):
                config.type = "file"
            elif uri.startswith("http"):
                config.type = "url"
            else:
                raise ConanException("Unable to deduce type config install: %s" % uri)
        config.source_folder = source_folder
        config.target_folder = target_folder
        config.args = args
        config.verify_ssl = verify_ssl
        if os.path.exists(uri):
            uri = os.path.abspath(uri)
        config.uri = uri
        return config


def _process_config(config, cache, output, requester):
    if config.type == "git":
        _process_git_repo(config, cache, output)
    elif config.type == "dir":
        _process_folder(config, config.uri, cache, output)
    elif config.type == "file":
        with tmp_config_install_folder(cache) as tmp_folder:
            _process_zip_file(config, config.uri, cache, output, tmp_folder)
    elif config.type == "url":
        _process_download(config, cache, output, requester=requester)
    else:
        raise ConanException("Unable to process config install: %s" % config.uri)


def _save_configs(configs_file, configs):
    save(configs_file, json.dumps([config.json() for config in configs],
                                  indent=True))


def configuration_install(app, uri, verify_ssl, config_type=None,
                          args=None, source_folder=None, target_folder=None):
    cache, output, requester = app.cache, app.out, app.requester
    configs = []
    configs_file = cache.config_install_file
    if os.path.isfile(configs_file):
        try:
            configs = json.loads(load(configs_file))
        except Exception as e:
            raise ConanException("Error loading configs-install file: %s\n%s"
                                 % (configs_file, str(e)))
        configs = [_ConfigOrigin(config) for config in configs]
    if uri is None:
        if config_type or args or not verify_ssl:  # Not the defaults
            if not configs:
                raise ConanException("Called config install without arguments")
            # Modify the last one
            config = configs[-1]
            config.config_type = config_type or config.type
            config.args = args or config.args
            config.verify_ssl = verify_ssl or config.verify_ssl
            _process_config(config, cache, output, requester)
            _save_configs(configs_file, configs)
        else:
            if not configs:
                raise ConanException("Called config install without arguments")
            # Execute the previously stored ones
            for config in configs:
                output.info("Config install:  %s" % _hide_password(config.uri))
                _process_config(config, cache, output, requester)
            touch(cache.config_install_file)
    else:
        # Execute and store the new one
        config = _ConfigOrigin.from_item(uri, config_type, verify_ssl, args,
                                         source_folder, target_folder)
        _process_config(config, cache, output, requester)
        if config not in configs:
            configs.append(config)
        else:
            configs = [(c if c != config else config) for c in configs]
        _save_configs(configs_file, configs)


def is_config_install_scheduled(api):
    """ Validate if the next config install is scheduled to occur now

        When config_install_interval is not configured, config install should not run
        When config_install_interval is configured, config install will respect the delta from:
            last conan install execution (sched file) + config_install_interval value < now

    :param api: Conan API instance
    :return: True, if it should occur now. Otherwise, False.
    """
    cache = ClientCache(api.cache_folder, api.out)
    interval = cache.config.config_install_interval
    config_install_file = cache.config_install_file
    if interval is not None:
        if os.path.exists(config_install_file):
            timestamp = os.path.getmtime(config_install_file)
            sched = datetime.fromtimestamp(timestamp, tz=gettz())
            sched += interval
            now = datetime.now(gettz())
            return now > sched
        else:
            raise ConanException("config_install_interval defined, but no config_install file")
