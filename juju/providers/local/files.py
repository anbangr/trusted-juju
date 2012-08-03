from getpass import getuser
import os
from StringIO import StringIO
import yaml

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.error import ConnectionRefusedError
from twisted.web.client import getPage

from juju.errors import ProviderError, FileNotFound
from juju.lib.upstart import UpstartService
from juju.providers.common.files import FileStorage


SERVER_URL_KEY = "local-storage-url"


class StorageServer(object):

    def __init__(self, juju_unit_namespace, storage_dir=None,
                 host=None, port=None, logfile=None):
        """Management facade for a web server on top of the provider storage.

        :param juju_unit_namespace: For disambiguation.
        :param host: Host interface to bind to.
        :param port: Port to bind to.
        :param logfile: Path to store log output.
        """
        if storage_dir:
            storage_dir = os.path.abspath(storage_dir)
        self._storage_dir = storage_dir
        self._host = host
        self._port = port
        self._logfile = logfile

        self._service = UpstartService(
            "juju-%s-file-storage" % juju_unit_namespace, use_sudo=True)
        self._service.set_description(
            "Juju file storage for %s" % juju_unit_namespace)
        self._service_args = [
            "twistd",
            "--nodaemon",
            "--uid", str(os.getuid()),
            "--gid", str(os.getgid()),
            "--logfile", logfile,
            "--pidfile=",
            "-d", self._storage_dir,
            "web",
            "--port", "tcp:%s:interface=%s" % (self._port, self._host),
            "--path", self._storage_dir]

    @inlineCallbacks
    def is_serving(self):
        try:
            storage = LocalStorage(self._storage_dir)
            yield getPage((yield storage.get_url(SERVER_URL_KEY)))
            returnValue(True)
        except ConnectionRefusedError:
            returnValue(False)

    @inlineCallbacks
    def start(self):
        """Start the storage server.

        Also stores the storage server url directly into provider storage.
        """
        assert self._storage_dir, "no storage_dir set"
        assert self._host, "no host set"
        assert self._port, "no port set"
        assert None not in self._service_args, "unset params"
        assert os.path.exists(self._storage_dir), "Invalid storage directory"
        try:
            with open(self._logfile, "a"):
                pass
        except IOError:
            raise AssertionError("logfile not writable by this user")


        storage = LocalStorage(self._storage_dir)
        yield storage.put(
            SERVER_URL_KEY,
            StringIO(yaml.safe_dump(
                {"storage-url": "http://%s:%s/" % (self._host, self._port)})))

        self._service.set_command(" ".join(self._service_args))
        yield self._service.start()

    def get_pid(self):
        return self._service.get_pid()

    def stop(self):
        """Stop the storage server."""
        return self._service.destroy()


class LocalStorage(FileStorage):

    @inlineCallbacks
    def get_url(self, key):
        """Get a network url to a local provider storage.

        The command line tools directly utilize the disk backed
        storage. The agents will use the read only web interface
        provided by the StorageServer to download resources, as
        in the local provider scenario they won't always have
        direct disk access.
        """
        try:
            storage_data = (yield self.get(SERVER_URL_KEY)).read()
        except FileNotFound:
            storage_data = ""

        if not storage_data or not "storage-url" in storage_data:
            raise ProviderError("Storage not initialized")
        url = yaml.load(storage_data)["storage-url"]
        returnValue(url + key)
