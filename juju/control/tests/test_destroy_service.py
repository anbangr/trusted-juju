from twisted.internet.defer import inlineCallbacks

from juju.control import main

from .common import ControlToolTest
from juju.charm.tests.test_repository import RepositoryTestBase
from juju.state.tests.test_service import ServiceStateManagerTestBase
from juju.providers import dummy # for coverage/trial interaction


class ControlStopServiceTest(
    ServiceStateManagerTestBase, ControlToolTest, RepositoryTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(ControlStopServiceTest, self).setUp()

        self.service_state1 = yield self.add_service_from_charm("mysql")
        self.service1_unit = yield self.service_state1.add_unit_state()
        self.service_state2 = yield self.add_service_from_charm("wordpress")

        yield self.add_relation(
            "database",
            (self.service_state1, "db", "server"),
            (self.service_state2, "db", "client"))

        self.output = self.capture_logging()
        self.stderr = self.capture_stream("stderr")

    @inlineCallbacks
    def test_stop_service(self):
        """
        'juju-control stop_service ' will shutdown all configured instances
        in all environments.
        """
        topology = yield self.get_topology()
        service_id = topology.find_service_with_name("mysql")
        self.assertNotEqual(service_id, None)

        finished = self.setup_cli_reactor()
        self.setup_exit(0)
        self.mocker.replay()
        main(["destroy-service", "mysql"])
        yield finished
        topology = yield self.get_topology()
        self.assertFalse(topology.has_service(service_id))
        exists = yield self.client.exists("/services/%s" % service_id)
        self.assertFalse(exists)
        self.assertIn("Service 'mysql' destroyed.", self.output.getvalue())

    @inlineCallbacks
    def test_stop_unknown_service(self):
        finished = self.setup_cli_reactor()
        self.setup_exit(0)
        self.mocker.replay()
        main(["destroy-service", "volcano"])
        yield finished
        self.assertIn(
            "Service 'volcano' was not found", self.stderr.getvalue())

    @inlineCallbacks
    def test_destroy_subordinate_service(self):
        log_service = yield self.add_service_from_charm("logging")
        lu1 = yield log_service.add_unit_state()

        yield self.add_relation(
            "juju-info", "container",
            (self.service_state1, "juju-info", "server"),
            (log_service, "juju-info", "client"),
            )


        finished = self.setup_cli_reactor()
        topology = yield self.get_topology()
        service_id = topology.find_service_with_name("logging")
        self.assertNotEqual(service_id, None)

        self.setup_exit(0)
        self.mocker.replay()

        main(["destroy-service", "logging"])
        yield finished
        service_id = topology.find_service_with_name("logging")

        topology = yield self.get_topology()
        self.assertTrue(topology.has_service(service_id))
        exists = yield self.client.exists("/services/%s" % service_id)
        self.assertTrue(exists)
        self.assertEquals(
            "Unsupported attempt to destroy subordinate service 'logging' "
            "while principal service 'mysql' is related.\n",
            self.output.getvalue())

    @inlineCallbacks
    def test_destroy_principal_with_subordinates(self):
        log_service = yield self.add_service_from_charm("logging")
        lu1 = yield log_service.add_unit_state()

        yield self.add_relation(
            "juju-info",
            (self.service_state1, "juju-info", "server"),
            (log_service, "juju-info", "client"))

        finished = self.setup_cli_reactor()
        topology = yield self.get_topology()
        service_id = topology.find_service_with_name("mysql")
        logging_id = topology.find_service_with_name("logging")

        self.assertNotEqual(service_id, None)
        self.assertNotEqual(logging_id, None)

        self.setup_exit(0)
        self.mocker.replay()

        main(["destroy-service", "mysql"])
        yield finished
        service_id = topology.find_service_with_name("mysql")

        topology = yield self.get_topology()
        self.assertFalse(topology.has_service(service_id))
        exists = yield self.client.exists("/services/%s" % service_id)
        self.assertFalse(exists)

        # Verify the subordinate state was not removed as well
        # destroy should allow the destruction of subordinate services
        # with no relations. This means removing the principal and then
        # breaking the relation will allow for actual removal from
        # Zookeeper. see test_destroy_subordinate_without_relations.
        exists = yield self.client.exists("/services/%s" % logging_id)
        self.assertTrue(exists)

        self.assertIn("Service 'mysql' destroyed.", self.output.getvalue())

    @inlineCallbacks
    def test_destroy_subordinate_without_relations(self):
        """Verify we can remove a subordinate w/o relations."""
        yield self.add_service_from_charm("logging")

        finished = self.setup_cli_reactor()
        topology = yield self.get_topology()
        logging_id = topology.find_service_with_name("logging")
        self.assertNotEqual(logging_id, None)

        self.setup_exit(0)
        self.mocker.replay()

        main(["destroy-service", "logging"])
        yield finished

        topology = yield self.get_topology()
        self.assertFalse(topology.has_service(logging_id))
        exists = yield self.client.exists("/services/%s" % logging_id)
        self.assertFalse(exists)
