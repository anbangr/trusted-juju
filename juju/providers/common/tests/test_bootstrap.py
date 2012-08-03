import logging
import tempfile

from twisted.internet.defer import fail, succeed, inlineCallbacks

from juju.errors import EnvironmentNotFound, ProviderError
from juju.lib.testing import TestCase
from juju.machine.tests.test_constraints import dummy_cs
from juju.providers.common.base import MachineProviderBase
from juju.providers.dummy import DummyMachine, FileStorage


class SomeError(Exception):
    pass


class WorkingFileStorage(FileStorage):

    def __init__(self):
        super(WorkingFileStorage, self).__init__(tempfile.mkdtemp())


class UnwritableFileStorage(object):

    def put(self, name, f):
        return fail(Exception("oh noes"))


class DummyProvider(MachineProviderBase):

    provider_type = "dummy"
    config = {"default-series": "splendid"}

    def __init__(self, test, file_storage, zookeeper):
        self._test = test
        self._file_storage = file_storage
        self._zookeeper = zookeeper

    def get_file_storage(self):
        return self._file_storage

    def get_zookeeper_machines(self):
        if isinstance(self._zookeeper, Exception):
            return fail(self._zookeeper)
        if self._zookeeper:
            return succeed([self._zookeeper])
        return fail(EnvironmentNotFound())

    def start_machine(self, machine_data, master=False):
        self._test.assertTrue(master)
        self._test.assertEquals(machine_data["machine-id"], "0")
        constraints = machine_data["constraints"]
        self._test.assertEquals(constraints["provider-type"], "dummy")
        self._test.assertEquals(constraints["ubuntu-series"], "splendid")
        self._test.assertEquals(constraints["arch"], "arm")
        return [DummyMachine("i-keepzoos")]


class BootstrapTest(TestCase):

    @inlineCallbacks
    def setUp(self):
        yield super(BootstrapTest, self).setUp()
        self.constraints = dummy_cs.parse(["arch=arm"]).with_series("splendid")

    def test_unknown_error(self):
        provider = DummyProvider(self, None, SomeError())
        d = provider.bootstrap(self.constraints)
        self.assertFailure(d, SomeError)
        return d

    def test_zookeeper_exists(self):
        log = self.capture_logging("juju.common", level=logging.DEBUG)
        provider = DummyProvider(
            self, WorkingFileStorage(), DummyMachine("i-alreadykeepzoos"))
        d = provider.bootstrap(self.constraints)

        def verify(machines):
            (machine,) = machines
            self.assertTrue(isinstance(machine, DummyMachine))
            self.assertEquals(machine.instance_id, "i-alreadykeepzoos")

            log_text = log.getvalue()
            self.assertIn(
                "juju environment previously bootstrapped", log_text)
            self.assertNotIn("Launching", log_text)
        d.addCallback(verify)
        return d

    def test_bad_storage(self):
        provider = DummyProvider(self, UnwritableFileStorage(), None)
        d = provider.bootstrap(self.constraints)
        self.assertFailure(d, ProviderError)

        def verify(error):
            self.assertEquals(
                str(error),
                "Bootstrap aborted because file storage is not writable: "
                "oh noes")
        d.addCallback(verify)
        return d

    def test_create_zookeeper(self):
        log = self.capture_logging("juju.common", level=logging.DEBUG)
        provider = DummyProvider(self, WorkingFileStorage(), None)
        d = provider.bootstrap(self.constraints)

        def verify(machines):
            (machine,) = machines
            self.assertTrue(isinstance(machine, DummyMachine))
            self.assertEquals(machine.instance_id, "i-keepzoos")

            log_text = log.getvalue()
            self.assertIn("Launching juju bootstrap instance", log_text)
            self.assertNotIn("previously bootstrapped", log_text)
        d.addCallback(verify)
        return d
