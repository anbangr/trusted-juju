from twisted.internet.defer import inlineCallbacks

from juju.control import main
from juju.state.environment import EnvironmentStateManager

from .common import MachineControlToolTest


class ControlSetConstraintsTest(MachineControlToolTest):

    @inlineCallbacks
    def setUp(self):
        yield super(ControlSetConstraintsTest, self).setUp()
        self.service_state = yield self.add_service_from_charm("mysql")
        self.output = self.capture_logging()
        self.stderr = self.capture_stream("stderr")

    @inlineCallbacks
    def test_set_service_constraints(self):
        finished = self.setup_cli_reactor()
        self.setup_exit(0)
        self.mocker.replay()
        main(["set-constraints", "--service", "mysql", "cpu=8", "mem=1G"])
        yield finished

        constraints = yield self.service_state.get_constraints()
        expect = {
            "arch": "amd64", "cpu": 8, "mem": 1024,
            "provider-type": "dummy", "ubuntu-series": "series"}
        self.assertEquals(constraints, expect)

    @inlineCallbacks
    def test_bad_service_constraint(self):
        initial_constraints = yield self.service_state.get_constraints()
        finished = self.setup_cli_reactor()
        self.setup_exit(1)
        self.mocker.replay()
        main(["set-constraints", "--service", "mysql", "arch=proscenium"])
        yield finished

        self.assertIn(
            "Bad 'arch' constraint 'proscenium': unknown architecture",
            self.stderr.getvalue())

        constraints = yield self.service_state.get_constraints()
        self.assertEquals(constraints, initial_constraints)

    @inlineCallbacks
    def test_environment_constraint(self):
        yield self.client.delete("/environment")
        finished = self.setup_cli_reactor()
        self.setup_exit(0)
        self.mocker.replay()
        main(["set-constraints", "arch=arm", "cpu=any"])
        yield finished

        esm = EnvironmentStateManager(self.client)
        yield esm.get_config()
        constraints = yield esm.get_constraints()
        self.assertEquals(constraints, {
            "ubuntu-series": None,
            "provider-type": "dummy",
            "arch": "arm",
            "cpu": None,
            "mem": 512.0})

    @inlineCallbacks
    def test_legacy_environment(self):
        yield self.client.delete("/constraints")
        finished = self.setup_cli_reactor()
        self.setup_exit(0)
        self.mocker.replay()
        main(["set-constraints", "arch=arm", "cpu=any"])
        yield finished

        self.assertIn(
            "Constraints are not valid in legacy deployments.",
            self.stderr.getvalue())
