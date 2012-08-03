# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for juju.providers.maas.launch"""

from StringIO import StringIO
from twisted.internet import defer
from twisted.internet.defer import inlineCallbacks
from xmlrpclib import Fault

from juju.errors import ProviderError
from juju.lib.mocker import ANY
from juju.machine import ProviderMachine
from juju.providers.common.state import _STATE_FILE
from juju.providers.maas import MAASMachine, MachineProvider
from juju.providers.maas.launch import MAASLaunchMachine
from juju.providers.maas.maas import MAASClient
from juju.providers.maas.tests.testing import (
    CONFIG, FakeMAASHTTPConnection, NODE_JSON, TestCase,
    FakeMAASHTTPConnectionWithNoAvailableNodes)


LOG_NAME = "juju.maas"


class FakeStorage(object):

    fake_state = """
        zookeeper-instances: [%s]
        """ % NODE_JSON[0]['resource_uri']

    def get(self, name):
        if name == _STATE_FILE:
            return defer.succeed(StringIO(self.fake_state))


class LaunchMachineTest(TestCase):

    def _get_provider(self, client_factory=None):
        self.setup_connection(MAASClient, client_factory)
        provider = MachineProvider("mymaas", CONFIG)
        provider._storage = FakeStorage()
        return provider

    def test_no_machine_id(self):
        provider = self._get_provider()
        d = provider.start_machine({})
        self.assertFailure(d, ProviderError)

        def verify(error):
            self.assertEqual(
                str(error),
                "Cannot launch a machine without specifying a machine-id")
        d.addCallback(verify)
        return d

    def test_no_constraints(self):
        provider = self._get_provider()
        d = provider.start_machine({"machine-id": 99})
        self.assertFailure(d, ProviderError)

        def verify(error):
            self.assertEqual(
                str(error),
                "Cannot launch a machine without specifying constraints")
        d.addCallback(verify)
        return d

    def test_no_available_machines(self):
        # Requesting a startup of an already-acquired machine should
        # result in a Fault being returned.
        provider = self._get_provider(
            FakeMAASHTTPConnectionWithNoAvailableNodes)
        machine_data = {
            "machine-id": "foo", "constraints": {"maas-name": None}}
        d = provider.start_machine(machine_data)

        # These arbitrary fake failure values come from
        # FakeMAASHTTPConnectionWithNoAvailableNodes.acquire_node()
        def check_failure_values(failure):
            code = failure.faultCode
            text = failure.faultString
            self.assertEqual(1, code)
            self.assertEqual("No available nodes", text)

        return self.assertFailure(d, Fault).addBoth(check_failure_values)

    @inlineCallbacks
    def test_actually_launch(self):
        # Grab a node from the pre-prepared testing data and some of its
        # data.
        target_node = NODE_JSON[0]
        machine_id = target_node['resource_uri']
        dns_name = target_node['hostname']

        # Try to start up that node using the fake MAAS.
        provider = self._get_provider(FakeMAASHTTPConnection)
        machine_data = {
            "machine-id": machine_id, "constraints": {"maas-name": None}}
        machine_list = yield provider.start_machine(machine_data)

        # Test that it returns a list containing a single MAASMachine
        # with the right properties.
        [machine] = machine_list
        self.assertIsInstance(machine, MAASMachine)
        expected = [machine_id, dns_name]
        actual = [machine.instance_id, machine.dns_name]
        self.assertEqual(
            actual, expected,
            "MAASMachine values of instance_id / dns_name don't match expected"
            "values")

    @inlineCallbacks
    def test_launch_with_name_constraint(self):
        # Try to launch a particular machine by its name using the
        # "maas-name" constraint.

        # Pick the "moon" node out of the test data.
        target_node = NODE_JSON[1]
        self.assertEqual("moon", target_node["hostname"])
        machine_id = "foo"
        provider = self._get_provider(FakeMAASHTTPConnection)
        constraints = {"maas-name": "moon"}
        machine_data = {"machine-id": machine_id, "constraints": constraints}
        machine_list = yield provider.start_machine(machine_data)

        # Check that "moon" was started.
        [machine] = machine_list
        self.assertIsInstance(machine, MAASMachine)
        self.assertEqual(machine.dns_name, "moon")

    def test_failed_launch(self):
        """If an acquired node fails to start, it is released."""
        # Throw away logs.
        self.capture_logging(LOG_NAME)

        mocker = self.mocker
        mock_client = mocker.mock()
        mock_provider = mocker.mock()
        mock_provider.maas_client
        mocker.result(mock_client)

        # These are used for generating cloud-init data.
        mock_provider.get_zookeeper_machines()
        mocker.result([ProviderMachine("zk.example.com")])
        mock_provider.config
        mocker.result({"authorized-keys": "key comment"})

        # The following operations happen in sequence.
        mocker.order()
        # First, the node is acquired.
        mock_client.acquire_node({"maas-name": None})
        mocker.result({"resource_uri": "/node/123"})
        # Second, the node is started. We simulate a failure at this stage.
        mock_client.start_node("/node/123", ANY)
        mocker.throw(ZeroDivisionError)
        # Last, the node is released.
        mock_client.release_node("/node/123")
        mocker.result({"resource_uri": "/node/123"})

        mocker.replay()
        return self.assertFailure(
            MAASLaunchMachine(mock_provider, {"maas-name": None}).run("fred"),
            ZeroDivisionError)
