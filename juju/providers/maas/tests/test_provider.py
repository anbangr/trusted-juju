# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test cases for juju.providers.maas.provider"""

from twisted.internet.defer import inlineCallbacks

from juju.errors import ConstraintError, MachinesNotFound, ProviderError
from juju.lib.mocker import ANY
from juju.providers.maas import MachineProvider
from juju.providers.maas.maas import MAASClient
from juju.providers.maas.tests.testing import (
    CONFIG, FakeMAASHTTPConnection, NODE_JSON, TestCase)


LOG_NAME = "juju.maas"


class TestMAASProvider(TestCase):

    def test_create(self):
        name = "mymaas"
        provider = MachineProvider(name, CONFIG)
        self.assertEqual(provider.environment_name, name)
        self.assertEqual(provider.config, CONFIG)

    def test_config_serialization(self):
        """
        The environment config should be serializable so that the zookeeper
        agent can read it back on the master node.
        """
        keys_path = self.makeFile("my-keys")

        config = {
            "admin-secret": "foo",
            "maas-server": "http://localhost:8000",
            "maas-oauth":
                ["HskWvqQmpEwNpkQLnd", "wxHZ99gBwAucKZbwUD",
                "323ybLTwTcENZsuDGNV6KaGkp99DjWcy"],
            "authorized-keys-path": keys_path}

        expected = {
            "admin-secret": "foo",
            "maas-server": "http://localhost:8000",
            "maas-oauth":
                "HskWvqQmpEwNpkQLnd:wxHZ99gBwAucKZbwUD:"
                "323ybLTwTcENZsuDGNV6KaGkp99DjWcy",
            "authorized-keys": "my-keys"}

        provider = MachineProvider("maas", config)
        serialized = provider.get_serialization_data()
        self.assertEqual(serialized, expected)

    @inlineCallbacks
    def test_open_port(self):
        log = self.capture_logging(LOG_NAME)
        yield MachineProvider("blah", CONFIG).open_port(None, None, None)
        self.assertIn(
            "Firewalling is not yet implemented", log.getvalue())

    @inlineCallbacks
    def test_close_port(self):
        log = self.capture_logging(LOG_NAME)
        yield MachineProvider("blah", CONFIG).close_port(None, None, None)
        self.assertIn(
            "Firewalling is not yet implemented", log.getvalue())

    @inlineCallbacks
    def test_get_opened_ports(self):
        log = self.capture_logging(LOG_NAME)
        ports = yield MachineProvider(
            "blah", CONFIG).get_opened_ports(None, None)
        self.assertEquals(ports, set())
        self.assertIn(
            "Firewalling is not yet implemented", log.getvalue())

    @inlineCallbacks
    def test_get_machines(self):
        self.setup_connection(MAASClient, FakeMAASHTTPConnection)
        provider = MachineProvider("blah", CONFIG)
        machines = yield provider.get_machines()
        self.assertNotEqual([], machines)

    def test_get_machines_too_few(self):
        """
        :exc:`juju.errors.MachinesNotFound` is raised when a requested machine
        is not found by `get_machines`. The error contains a list of instance
        IDs that were missing.
        """
        self.setup_connection(MAASClient, FakeMAASHTTPConnection)
        provider = MachineProvider("blah", CONFIG)
        instance_id = "/api/123/nodes/fred"
        d = provider.get_machines([instance_id])
        d = self.assertFailure(d, MachinesNotFound)
        d.addCallback(
            lambda error: self.assertEqual(
                [instance_id], error.instance_ids))
        return d

    def test_get_machines_too_many(self):
        """
        :exc:`juju.errors.ProviderError` is raised when a machine not
        requested is returned by `get_machines`.
        """
        self.setup_connection(MAASClient, FakeMAASHTTPConnection)
        provider = MachineProvider("blah", CONFIG)
        instance_id = NODE_JSON[1]["resource_uri"]
        d = provider.get_machines([instance_id])
        d = self.assertFailure(d, ProviderError)
        d.addCallback(
            lambda error: self.assertEqual(
                "Cannot find machine: %s" % instance_id,
                str(error)))
        return d

    @inlineCallbacks
    def test_shutdown_machines(self):
        self.setup_connection(MAASClient, FakeMAASHTTPConnection)
        client = MAASClient(CONFIG)

        # Patch the client to demonstrate that the node is stopped and
        # released when shutting down.
        mocker = self.mocker
        mock_client = mocker.patch(client)
        mocker.order()
        mock_client.stop_node(ANY)
        mocker.result(None)
        mock_client.release_node(ANY)
        mocker.result(None)
        mocker.replay()

        provider = MachineProvider("blah", CONFIG)
        provider.maas_client = mock_client
        machines = yield provider.get_machines()
        machine_to_shutdown = machines[0]
        machines_terminated = (
            yield provider.shutdown_machines([machine_to_shutdown]))
        self.assertEqual(
            [machines[0].instance_id],
            [machine.instance_id for machine in machines_terminated])

    @inlineCallbacks
    def test_constraints(self):
        provider = MachineProvider("blah", CONFIG)
        cs = yield provider.get_constraint_set()
        self.assertEquals(cs.parse([]), {
            "provider-type": "maas",
            "ubuntu-series": None,
            "maas-name": None})
        self.assertEquals(cs.parse(["maas-name=totoro"]), {
            "provider-type": "maas",
            "ubuntu-series": None,
            "maas-name": "totoro"})

        bill = cs.parse(["maas-name=bill"]).with_series("precise")
        ben = cs.parse(["maas-name=ben"]).with_series("precise")
        nil = cs.parse([]).with_series("precise")
        self.assertTrue(bill.can_satisfy(bill))
        self.assertTrue(bill.can_satisfy(nil))
        self.assertTrue(ben.can_satisfy(ben))
        self.assertTrue(ben.can_satisfy(nil))
        self.assertTrue(nil.can_satisfy(nil))
        self.assertFalse(nil.can_satisfy(bill))
        self.assertFalse(nil.can_satisfy(ben))
        self.assertFalse(ben.can_satisfy(bill))
        self.assertFalse(bill.can_satisfy(ben))

    @inlineCallbacks
    def test_precise_only_constraints(self):
        provider = MachineProvider("blah", CONFIG)
        cs = yield provider.get_constraint_set()
        e = self.assertRaises(
             ConstraintError, cs.parse([]).with_series, "not-precise")
        self.assertEquals(
            str(e),
            "Bad 'ubuntu-series' constraint 'not-precise': MAAS currently "
            "only provisions machines running precise")

