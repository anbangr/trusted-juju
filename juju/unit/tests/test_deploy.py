import logging
import os
import os.path

from twisted.internet.defer import inlineCallbacks, succeed, Deferred

from juju.charm.bundle import CharmBundle
from juju.charm.publisher import CharmPublisher
from juju.charm.tests import local_charm_id
from juju.lib import under
from juju.lib.mocker import MATCH
from juju.machine.tests.test_constraints import (
    dummy_constraints, series_constraints)
from juju.machine.unit import UnitMachineDeployment
from juju.state.machine import MachineStateManager
from juju.state.service import ServiceStateManager
from juju.state.tests.common import StateTestBase
from juju.unit.deploy import UnitDeployer
from juju.tests.common import get_test_zookeeper_address

MATCH_BUNDLE = MATCH(lambda x: isinstance(x, CharmBundle))


class UnitDeployerTest(StateTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(UnitDeployerTest, self).setUp()
        self.output = self.capture_logging(level=logging.DEBUG)
        yield self.push_default_config()

        # Load the environment with the charm state and charm binary
        environment = self.config.get_default()
        provider = environment.get_machine_provider()
        storage = provider.get_file_storage()
        publisher = CharmPublisher(self.client, storage)
        yield publisher.add_charm(local_charm_id(self.charm), self.charm)
        self.charm_state, = yield publisher.publish()

        # Create a service from the charm, then add a unit and assign
        # it to a machine.
        self.service_state_manager = ServiceStateManager(self.client)
        self.machine_state_manager = MachineStateManager(self.client)
        self.service = yield self.service_state_manager.add_service_state(
            "myblog", self.charm_state, dummy_constraints)
        self.unit_state = yield self.service.add_unit_state()
        self.machine_state = yield self.machine_state_manager.\
            add_machine_state(series_constraints)
        yield self.unit_state.assign_to_machine(self.machine_state)

        # NOTE machine_id must be a str to use with one of the
        # deployment classes
        self.juju_dir = self.makeDir()
        self.unit_manager = UnitDeployer(
            self.client, str(self.machine_state.id), self.juju_dir)
        yield self.unit_manager.start()

    def test_start_initializes(self):
        """Verify starting unit manager initializes any necessary resources."""
        self.assertTrue(os.path.isdir(self.unit_manager.charms_directory))
        self.assertEqual(self.unit_manager.deploy_factory,
                         UnitMachineDeployment)

    @inlineCallbacks
    def test_charm_download(self):
        """Downloading a charm should store the charm locally."""
        yield self.unit_manager.download_charm(self.charm_state)
        checksum = self.charm.get_sha256()
        charm_id = local_charm_id(self.charm)
        charm_key = under.quote("%s:%s" % (charm_id, checksum))
        charm_path = os.path.join(self.unit_manager.charms_directory, charm_key)

        self.assertTrue(os.path.exists(charm_path))
        bundle = CharmBundle(charm_path)
        self.assertEquals(
            bundle.get_revision(), self.charm.get_revision())
        self.assertEquals(bundle.get_sha256(), checksum)
        self.assertIn(
            "Downloading charm %s" % charm_id, self.output.getvalue())

    @inlineCallbacks
    def test_start_service_unit(self):
        """Verify starting a service unit kicks off the desired deployment."""
        mock_deployment = self.mocker.patch(self.unit_manager.deploy_factory)
        mock_deployment.start("0", get_test_zookeeper_address(), MATCH_BUNDLE)
        test_deferred = Deferred()

        def test_complete(machine_id, servers, bundle):
            test_deferred.callback(True)

        self.mocker.call(test_complete)
        self.mocker.replay()

        yield self.unit_manager.start_service_unit("myblog/0")
        yield test_deferred
        self.assertLogLines(
            self.output.getvalue(),
            ["Downloading charm local:series/dummy-1 to %s" % \
                 os.path.join(self.juju_dir, "charms"),
            "Starting service unit myblog/0...",
            "Started service unit myblog/0"])

    @inlineCallbacks
    def test_kill_service_unit(self):
        """Verify killing a service unit destroys the deployment."""
        mock_deployment = self.mocker.patch(self.unit_manager.deploy_factory)
        mock_deployment.start("0", get_test_zookeeper_address(), MATCH_BUNDLE)
        self.mocker.result(succeed(True))
        mock_deployment.destroy()
        self.mocker.result(succeed(True))
        test_deferred = Deferred()

        def test_complete():
            test_deferred.callback(True)

        self.mocker.call(test_complete)
        self.mocker.replay()

        # Start
        yield self.unit_manager.start_service_unit("myblog/0")

        # and stop.
        yield self.unit_state.unassign_from_machine()
        yield self.unit_manager.kill_service_unit("myblog/0")

        yield test_deferred
        self.assertLogLines(
            self.output.getvalue(),
            ["Downloading charm local:series/dummy-1 to %s" % \
                 os.path.join(self.juju_dir, "charms"),
             "Starting service unit myblog/0...",
             "Started service unit myblog/0",
             "Stopping service unit myblog/0...",
             "Stopped service unit myblog/0"])
