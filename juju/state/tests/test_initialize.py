import zookeeper

from twisted.internet.defer import inlineCallbacks
from txzookeeper.tests.utils import deleteTree

from juju.environment.tests.test_config import EnvironmentsConfigTestBase
from juju.state.auth import  make_identity
from juju.state.environment import (
    GlobalSettingsStateManager, EnvironmentStateManager)
from juju.state.initialize import StateHierarchy
from juju.state.machine import MachineStateManager


class LayoutTest(EnvironmentsConfigTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(LayoutTest, self).setUp()
        self.log = self.capture_logging("juju.state.init")
        zookeeper.set_debug_level(0)
        self.client = self.get_zookeeper_client()
        self.identity = make_identity("admin:genie")
        constraints_data = {
            "arch": "arm",
            "cpu": None,
            "ubuntu-series": "cranky",
            "provider-type": "dummy"}
        self.layout = StateHierarchy(
            self.client, self.identity, "i-abcdef", constraints_data, "dummy")
        yield self.client.connect()

    def tearDown(self):
        deleteTree(handle=self.client.handle)
        self.client.close()

    @inlineCallbacks
    def assert_existence_and_acl(self, path):
        exists = yield self.client.exists(path)
        self.assertTrue(exists)
        acls, stat = yield self.client.get_acl(path)

        found_admin_acl = False
        for acl in acls:
            if acl["id"] == self.identity \
                   and acl["perms"] == zookeeper.PERM_ALL:
                found_admin_acl = True
                break
        self.assertTrue(found_admin_acl)

    @inlineCallbacks
    def test_initialize(self):
        yield self.layout.initialize()

        yield self.assert_existence_and_acl("/charms")
        yield self.assert_existence_and_acl("/services")
        yield self.assert_existence_and_acl("/units")
        yield self.assert_existence_and_acl("/machines")
        yield self.assert_existence_and_acl("/relations")
        yield self.assert_existence_and_acl("/initialized")

        # To check that the constraints landed correctly, we need the
        # environment config to have been sent, or we won't be able to
        # get a provider to help us construct the appropriate objects.
        yield self.push_default_config(with_constraints=False)

        esm = EnvironmentStateManager(self.client)
        env_constraints = yield esm.get_constraints()
        self.assertEquals(env_constraints, {
            "provider-type": "dummy",
            "ubuntu-series": None,
            "arch": "arm",
            "cpu": None,
            "mem": 512})

        machine_state_manager = MachineStateManager(self.client)
        machine_state = yield machine_state_manager.get_machine_state(0)
        machine_constraints = yield machine_state.get_constraints()
        self.assertTrue(machine_constraints.complete)
        self.assertEquals(machine_constraints, {
            "provider-type": "dummy",
            "ubuntu-series": "cranky",
            "arch": "arm",
            "cpu": None,
            "mem": 512})
        instance_id = yield machine_state.get_instance_id()
        self.assertEqual(instance_id, "i-abcdef")

        settings_manager = GlobalSettingsStateManager(self.client)
        self.assertEqual((yield settings_manager.get_provider_type()), "dummy")
        self.assertEqual(
            self.log.getvalue().strip(),
            "Initializing zookeeper hierarchy")
