import yaml

from twisted.internet.defer import inlineCallbacks

from juju.control import main
from juju.machine.tests.test_constraints import dummy_cs
from juju.state.environment import EnvironmentStateManager

from .common import MachineControlToolTest

env_log = "Fetching constraints for environment"
machine_log = "Fetching constraints for machine 1"
service_log = "Fetching constraints for service mysql"
unit_log = "Fetching constraints for service unit mysql/0"


class ConstraintsGetTest(MachineControlToolTest):

    @inlineCallbacks
    def setUp(self):
        yield super(ConstraintsGetTest, self).setUp()
        env_constraints = dummy_cs.parse(["mem=1024"])
        esm = EnvironmentStateManager(self.client)
        yield esm.set_constraints(env_constraints)
        self.expect_env = {
            "arch": "amd64", "cpu": 1.0, "mem": 1024.0,
            "provider-type": "dummy", "ubuntu-series": None}

        service_constraints = dummy_cs.parse(["cpu=10"])
        service = yield self.add_service_from_charm(
            "mysql", constraints=service_constraints)
        # unit will snapshot the state of service when added
        unit = yield service.add_unit_state()
        self.expect_unit = {
            "arch": "amd64", "cpu": 10.0, "mem": 1024.0,
            "provider-type": "dummy", "ubuntu-series": "series"}

        # machine gets its own constraints
        machine_constraints = dummy_cs.parse(["cpu=15", "mem=8G"])
        machine = yield self.add_machine_state(
            constraints=machine_constraints.with_series("series"))
        self.expect_machine = {
            "arch": "amd64", "cpu": 15.0, "mem": 8192.0,
            "provider-type": "dummy", "ubuntu-series": "series"}
        yield unit.assign_to_machine(machine)

        # service gets new constraints, leaves unit untouched
        yield service.set_constraints(dummy_cs.parse(["mem=16G"]))
        self.expect_service = {
            "arch": "amd64", "cpu": 1.0, "mem": 16384.0,
            "provider-type": "dummy", "ubuntu-series": "series"}

        self.log = self.capture_logging()
        self.stdout = self.capture_stream("stdout")
        self.finished = self.setup_cli_reactor()
        self.setup_exit(0)
        self.mocker.replay()

    def assert_messages(self, *messages):
        log = self.log.getvalue()
        for message in messages:
            self.assertIn(message, log)

    @inlineCallbacks
    def test_env(self):
        main(["get-constraints"])
        yield self.finished
        result = yaml.load(self.stdout.getvalue())
        self.assertEquals(result, self.expect_env)
        self.assert_messages(env_log)

    @inlineCallbacks
    def test_service(self):
        main(["get-constraints", "mysql"])
        yield self.finished
        result = yaml.load(self.stdout.getvalue())
        self.assertEquals(result, {"mysql": self.expect_service})
        self.assert_messages(service_log)

    @inlineCallbacks
    def test_unit(self):
        main(["get-constraints", "mysql/0"])
        yield self.finished
        result = yaml.load(self.stdout.getvalue())
        self.assertEquals(result, {"mysql/0": self.expect_unit})
        self.assert_messages(unit_log)

    @inlineCallbacks
    def test_machine(self):
        main(["get-constraints", "1"])
        yield self.finished
        result = yaml.load(self.stdout.getvalue())
        self.assertEquals(result, {"1": self.expect_machine})
        self.assert_messages(machine_log)

    @inlineCallbacks
    def test_all(self):
        main(["get-constraints", "mysql", "mysql/0", "1"])
        yield self.finished
        result = yaml.load(self.stdout.getvalue())
        expect = {"mysql": self.expect_service,
                  "mysql/0": self.expect_unit,
                  "1": self.expect_machine}
        self.assertEquals(result, expect)
        self.assert_messages(service_log, unit_log, machine_log)

    @inlineCallbacks
    def test_syncs_environment(self):
        """If the environment were not synced, it would be impossible to create
        the Constraints, so tool success proves sync."""
        yield self.client.delete("/environment")
        main(["get-constraints", "mysql/0"])
        yield self.finished
        result = yaml.load(self.stdout.getvalue())
        self.assertEquals(result, {"mysql/0": self.expect_unit})
        self.assert_messages(unit_log)
