import os
import signal
from StringIO  import StringIO
import subprocess
import yaml

from twisted.internet.defer import inlineCallbacks, succeed
from twisted.web.client import getPage

from juju.errors import ProviderError, ServiceError
from juju.lib.lxc.tests.test_lxc import uses_sudo
from juju.lib.testing import TestCase
from juju.lib.upstart import UpstartService
from juju.providers.local.files import (
    LocalStorage, StorageServer, SERVER_URL_KEY)
from juju.state.utils import get_open_port


class WebFileStorageTest(TestCase):

    @inlineCallbacks
    def setUp(self):
        yield super(WebFileStorageTest, self).setUp()
        self._storage_path = self.makeDir()
        self._logfile = self.makeFile()
        self._storage = LocalStorage(self._storage_path)
        self._port = get_open_port()
        self._server = StorageServer(
            "ns1", self._storage_path, "localhost", self._port, self._logfile)

    @inlineCallbacks
    def wait_for_server(self, server):
        while not (yield server.is_serving()):
            yield self.sleep(0.1)

    def test_start_missing_args(self):
        server = StorageServer("ns1", self._storage_path)
        return self.assertFailure(server.start(), AssertionError)

    def test_start_invalid_directory(self):
        os.rmdir(self._storage_path)
        return self.assertFailure(self._server.start(), AssertionError)

    @inlineCallbacks
    def test_upstart(self):
        subprocess_calls = []

        def intercept_args(args, **kwargs):
            subprocess_calls.append(args)
            self.assertEquals(args[0], "sudo")
            if args[1] == "cp":
                return real_check_call(args[1:], **kwargs)
            return 0

        real_check_call = self.patch(subprocess, "check_call", intercept_args)
        init_dir = self.makeDir()
        self.patch(UpstartService, "init_dir", init_dir)

        # Mock out the repeated checking for unstable pid, after an initial
        # stop/waiting to induce the actual start
        getProcessOutput = self.mocker.replace(
            "twisted.internet.utils.getProcessOutput")
        getProcessOutput("/sbin/status", ["juju-ns1-file-storage"])
        self.mocker.result(succeed("stop/waiting"))
        for _ in range(5):
            getProcessOutput("/sbin/status", ["juju-ns1-file-storage"])
            self.mocker.result(succeed("start/running 123"))
        self.mocker.replay()

        try:
            os.remove("/tmp/juju-ns1-file-storage.output")
        except OSError:
            pass  # just make sure it's not there, so the .start()
                  # doesn't insert a spurious rm

        yield self._server.start()
        chmod = subprocess_calls[1]
        conf_dest = os.path.join(init_dir, "juju-ns1-file-storage.conf")
        self.assertEquals(chmod, ("sudo", "chmod", "644", conf_dest))
        start = subprocess_calls[-1]
        self.assertEquals(
            start, ("sudo", "/sbin/start", "juju-ns1-file-storage"))

        with open(conf_dest) as f:
            for line in f:
                if line.startswith("env"):
                    self.fail("didn't expect any special environment")
                if line.startswith("exec"):
                    exec_ = line[5:].strip()

        expect_exec = (
            "twistd --nodaemon --uid %s --gid %s --logfile %s --pidfile= -d "
            "%s web --port tcp:%s:interface=localhost --path %s >> "
            "/tmp/juju-ns1-file-storage.output 2>&1"
            % (os.getuid(), os.getgid(), self._logfile, self._storage_path,
            self._port, self._storage_path))
        self.assertEquals(exec_, expect_exec)

    @uses_sudo
    @inlineCallbacks
    def test_start_stop(self):
        yield self._storage.put("abc", StringIO("hello world"))
        yield self._server.start()
        # Starting multiple times is fine.
        yield self._server.start()
        storage_url = yield self._storage.get_url("abc")

        # It might not have started actually accepting connections yet...
        yield self.wait_for_server(self._server)
        self.assertEqual((yield getPage(storage_url)), "hello world")

        # Check that it can be killed by the current user (ie, is not running
        # as root) and still comes back up
        old_pid = yield self._server.get_pid()
        os.kill(old_pid, signal.SIGKILL)
        new_pid = yield self._server.get_pid()
        self.assertNotEquals(old_pid, new_pid)

        # Give it a moment to actually start serving again
        yield self.wait_for_server(self._server)
        self.assertEqual((yield getPage(storage_url)), "hello world")

        yield self._server.stop()
        # Stopping multiple times is fine too.
        yield self._server.stop()

    @uses_sudo
    @inlineCallbacks
    def test_namespacing(self):
        alt_storage_path = self.makeDir()
        alt_storage = LocalStorage(alt_storage_path)
        yield alt_storage.put("some-path", StringIO("alternative"))
        yield self._storage.put("some-path", StringIO("original"))

        alt_server = StorageServer(
            "ns2", alt_storage_path, "localhost", get_open_port(),
            self.makeFile())
        yield alt_server.start()
        yield self._server.start()
        yield self.wait_for_server(alt_server)
        yield self.wait_for_server(self._server)

        alt_contents = yield getPage(
            (yield alt_storage.get_url("some-path")))
        self.assertEquals(alt_contents, "alternative")
        orig_contents = yield getPage(
            (yield self._storage.get_url("some-path")))
        self.assertEquals(orig_contents, "original")

        yield alt_server.stop()
        yield self._server.stop()

    @uses_sudo
    @inlineCallbacks
    def test_capture_errors(self):
        self._port = get_open_port()
        self._server = StorageServer(
            "borken", self._storage_path, "lol borken", self._port,
            self._logfile)
        d = self._server.start()
        e = yield self.assertFailure(d, ServiceError)
        self.assertTrue(str(e).startswith(
            "Failed to start job juju-borken-file-storage; got output:\n"))
        self.assertIn("Wrong number of arguments", str(e))
        yield self._server.stop()


class FileStorageTest(TestCase):

    def setUp(self):
        self._storage = LocalStorage(self.makeDir())

    @inlineCallbacks
    def test_get_url(self):
        yield self.assertFailure(self._storage.get_url("abc"), ProviderError)
        self._storage.put(SERVER_URL_KEY, StringIO("abc"))
        yield self.assertFailure(self._storage.get_url("abc"), ProviderError)
        self._storage.put(
            SERVER_URL_KEY,
            StringIO(yaml.dump({"storage-url": "http://localhost/"})))

        self.assertEqual((yield self._storage.get_url("abc")),
                         "http://localhost/abc")
