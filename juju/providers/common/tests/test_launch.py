import tempfile

from twisted.internet.defer import fail, succeed, inlineCallbacks, returnValue

from juju.errors import EnvironmentNotFound
from juju.lib.testing import TestCase
from juju.machine.tests.test_constraints import dummy_cs
from juju.providers.common.base import MachineProviderBase
from juju.providers.common.launch import LaunchMachine
from juju.providers.dummy import DummyMachine, FileStorage


def get_classes(test, master, machine_id, zookeeper):

    class DummyProvider(MachineProviderBase):

        provider_type = "dummy"

        def __init__(self):
            self.config = {"admin-secret": "BLAH"}
            self._file_storage = FileStorage(tempfile.mkdtemp())

        def get_file_storage(self):
            return self._file_storage

        def get_constraint_set(self):
            return succeed(dummy_cs)

        def get_zookeeper_machines(self):
            if zookeeper:
                return succeed([zookeeper])
            return fail(EnvironmentNotFound())

    class DummyLaunchMachine(LaunchMachine):

        def start_machine(self, actual_machine_id, zookeepers):
            test.assertEquals(actual_machine_id, machine_id)
            test.assertEquals(zookeepers, filter(None, [zookeeper]))
            test.assertEquals(self._master, master)
            test.assertEquals(self._constraints, self.expect_constraints)
            return succeed([DummyMachine("i-malive")])

    return DummyProvider, DummyLaunchMachine


@inlineCallbacks
def launch_machine(test, master, zookeeper):
    DummyProvider, DummyLaunchMachine = get_classes(
        test, master, "1234", zookeeper)

    provider = DummyProvider()
    cs = yield provider.get_constraint_set()
    constraints = cs.parse([])
    launch = DummyLaunchMachine(provider, constraints, master)
    launch.expect_constraints = constraints
    machines = yield launch.run("1234")
    returnValue((provider, machines))


class LaunchMachineTest(TestCase):

    @inlineCallbacks
    def assert_success(self, master, zookeeper):
        provider, machines = yield launch_machine(self, master, zookeeper)
        (machine,) = machines
        self.assertTrue(isinstance(machine, DummyMachine))
        self.assertEquals(machine.instance_id, "i-malive")
        returnValue(provider)

    def test_start_nonzookeeper_no_zookeepers(self):
        """Starting a non-zookeeper without a zookeeper is an error"""
        return self.assertFailure(
            launch_machine(self, False, None), EnvironmentNotFound)

    @inlineCallbacks
    def test_start_zookeeper_no_zookeepers(self):
        """A new zookeeper should be recorded in provider state"""
        provider = yield self.assert_success(True, None)
        provider_state = yield provider.load_state()
        self.assertEquals(
            provider_state, {"zookeeper-instances": ["i-malive"]})

    @inlineCallbacks
    def test_works_with_zookeeper(self):
        """Creating a non-zookeeper machine should not alter provider state"""
        provider = yield self.assert_success(False, DummyMachine("i-keepzoos"))
        provider_state = yield provider.load_state()
        self.assertEquals(provider_state, False)

    @inlineCallbacks
    def test_convenience_launch(self):
        DummyProvider, DummyLaunchMachine = get_classes(
            self, True, "999", None)

        provider = DummyProvider()
        cs = yield provider.get_constraint_set()
        constraints = cs.parse(["arch=arm", "mem=1G"])
        DummyLaunchMachine.expect_constraints = constraints
        machine_data = {
            "machine-id": "999", "constraints": constraints}
        machines = yield DummyLaunchMachine.launch(
            provider, machine_data, True)
        (machine,) = machines
        self.assertTrue(isinstance(machine, DummyMachine))
        self.assertEquals(machine.instance_id, "i-malive")
