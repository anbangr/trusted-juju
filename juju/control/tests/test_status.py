from fnmatch import fnmatch
import inspect
import json
import logging
import os
from StringIO import StringIO
import yaml

from twisted.internet.defer import inlineCallbacks, returnValue

from juju.agents.base import TwistedOptionNamespace
from juju.agents.machine import MachineAgent
from juju.environment.environment import Environment
from juju.control import status
from juju.control import tests
from juju.state.endpoint import RelationEndpoint
from juju.state.environment import GlobalSettingsStateManager
from juju.state.tests.test_service import ServiceStateManagerTestBase
from juju.tests.common import get_test_zookeeper_address
from juju.unit.workflow import ZookeeperWorkflowState

from .common import ControlToolTest

tests_path = os.path.dirname(inspect.getabsfile(tests))
sample_path = os.path.join(tests_path, "sample_cluster.yaml")
sample_cluster = yaml.load(open(sample_path, "r"))


def dump_stringio(stringio, filename):
    """Debug utility to dump a StringIO to a filename."""
    fp = open(filename, "w")
    fp.write(stringio.getvalue())
    fp.close()


@inlineCallbacks
def collect(scope, provider, client, log):
    """Collect and return status info as dict"""
    # provided for backwards compatibility with
    # original API
    # used only in testing
    s = status.StatusCommand(client, provider, log)
    state = yield s(scope)
    returnValue(state)


class StatusTestBase(ServiceStateManagerTestBase, ControlToolTest):

    # Status tests setup a large tree every time, make allowances for it.
    # TODO: create minimal trees needed per test.
    timeout = 10

    @inlineCallbacks
    def setUp(self):
        yield super(StatusTestBase, self).setUp()
        settings = GlobalSettingsStateManager(self.client)
        yield settings.set_provider_type("dummy")
        self.log = self.capture_logging()

        self.environment = self.config.get_default()
        self.provider = self.environment.get_machine_provider()
        self.machine_count = 0
        self.output = StringIO()

    @inlineCallbacks
    def set_unit_state(self, unit_state, state, port_protos=()):
        unit_state.set_public_address(
            "%s.example.com" % unit_state.unit_name.replace("/", "-"))
        workflow_client = ZookeeperWorkflowState(self.client, unit_state)
        with (yield workflow_client.lock()):
            yield workflow_client.set_state(state)
        for port_proto in port_protos:
            yield unit_state.open_port(*port_proto)

    @inlineCallbacks
    def add_relation_unit_states(self, relation_state, unit_states, states):
        for unit_state, state in zip(unit_states, states):
            relation_unit_state = yield relation_state.add_unit_state(unit_state)
            workflow_client = ZookeeperWorkflowState(
                self.client, relation_unit_state)
            with (yield workflow_client.lock()):
                yield workflow_client.set_state(state)

    @inlineCallbacks
    def add_relation_with_relation_units(
            self,
            source_endpoint, source_units, source_states,
            dest_endpoint, dest_units, dest_states):

        relation_state, service_relation_states = \
            yield self.relation_state_manager.add_relation_state(
            *[source_endpoint, dest_endpoint])
        source_relation_state, dest_relation_state = service_relation_states
        yield self.add_relation_unit_states(
            source_relation_state, source_units, source_states)
        yield self.add_relation_unit_states(
            dest_relation_state, dest_units, dest_states)

    @inlineCallbacks
    def add_unit(self, service, machine, with_agent=lambda _: True,
                 units=None, container=None):
        unit = yield service.add_unit_state(container=container)
        self.assertTrue(machine or container)
        if machine is not None:
            yield unit.assign_to_machine(machine)
        if with_agent(unit.unit_name):
            yield unit.connect_agent()
        if units is not None:
            units.setdefault(service.service_name, []).append(unit)

        returnValue(unit)

    @inlineCallbacks
    def build_topology(self, base=None, skip_unit_agents=()):
        """Build a simulated topology with a default machine configuration.

        This method returns a dict that can be used to get handles to
        the constructed objects.
        """
        state = {}

        # build out the topology using the state managers
        m1 = yield self.add_machine_state()
        m2 = yield self.add_machine_state()
        m3 = yield self.add_machine_state()
        m4 = yield self.add_machine_state()
        m5 = yield self.add_machine_state()
        m6 = yield self.add_machine_state()
        m7 = yield self.add_machine_state()

        # inform the provider about the machine
        yield self.provider.start_machine({"machine-id": 0,
                                           "dns-name": "steamcloud-1.com"})
        yield self.provider.start_machine({"machine-id": 1,
                                           "dns-name": "steamcloud-2.com"})
        yield self.provider.start_machine({"machine-id": 2,
                                           "dns-name": "steamcloud-3.com"})
        yield self.provider.start_machine({"machine-id": 3,
                                           "dns-name": "steamcloud-4.com"})
        yield self.provider.start_machine({"machine-id": 4,
                                           "dns-name": "steamcloud-5.com"})
        yield self.provider.start_machine({"machine-id": 5,
                                           "dns-name": "steamcloud-6.com"})
        yield self.provider.start_machine({"machine-id": 6,
                                           "dns-name": "steamcloud-7.com"})

        yield m1.set_instance_id(0)
        yield m2.set_instance_id(1)
        yield m3.set_instance_id(2)
        yield m4.set_instance_id(3)
        yield m5.set_instance_id(4)
        yield m6.set_instance_id(5)
        yield m7.set_instance_id(6)

        state["machines"] = [m1, m2, m3, m4, m5, m6, m7]

        # "Deploy" services
        wordpress = yield self.add_service_from_charm("wordpress")
        mysql = yield self.add_service_from_charm("mysql")
        yield mysql.set_exposed_flag()  # but w/ no open ports

        varnish = yield self.add_service_from_charm("varnish")
        yield varnish.set_exposed_flag()
        # w/o additional metadata
        memcache = yield self.add_service("memcache")

        state["services"] = dict(wordpress=wordpress, mysql=mysql,
                                 varnish=varnish, memcache=memcache)

        def with_unit(name):
            for pattern in skip_unit_agents:
                if fnmatch(name, pattern):
                    return False
            return True


        units = {}
        wpu = yield self.add_unit(wordpress, m1, with_unit, units)
        myu1 = yield self.add_unit(mysql, m2, with_unit, units)
        myu2 = yield self.add_unit(mysql, m3, with_unit, units)
        vu1 = yield self.add_unit(varnish, m4, with_unit, units)
        vu2 = yield self.add_unit(varnish, m5, with_unit, units)
        mc1 = yield self.add_unit(memcache, m6, with_unit, units)
        mc2 = yield self.add_unit(memcache, m7, with_unit, units)
        state["units"] = units

        # add unit states to services and assign to machines
        # Set the lifecycle state and open ports, if any, for each unit state.
        yield self.set_unit_state(wpu, "started", [(80, "tcp"), (443, "tcp")])
        yield self.set_unit_state(myu1, "started")
        yield self.set_unit_state(myu2, "stop_error")
        yield self.set_unit_state(vu1, "started", [(80, "tcp")])
        yield self.set_unit_state(vu2, "started", [(80, "tcp")])
        yield self.set_unit_state(mc1, None)
        yield self.set_unit_state(mc2, "installed")

        # Wordpress integrates with each of the following
        # services. Each relation endpoint is used to define the
        # specific relation to be established.
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db", "server")
        memcache_ep = RelationEndpoint(
            "memcache", "client-server", "cache", "server")
        varnish_ep = RelationEndpoint(
            "varnish", "client-server", "proxy", "client")

        wordpress_db_ep = RelationEndpoint(
            "wordpress", "client-server", "db", "client")
        wordpress_cache_ep = RelationEndpoint(
            "wordpress", "client-server", "cache", "client")
        wordpress_proxy_ep = RelationEndpoint(
            "wordpress", "client-server", "proxy", "server")

        # Create relation service units for each of these relations
        yield self.add_relation_with_relation_units(
            mysql_ep, [myu1, myu2], ["up", "departed"],
            wordpress_db_ep, [wpu], ["up"])
        yield self.add_relation_with_relation_units(
            memcache_ep, [mc1, mc2], ["up", "down"],
            wordpress_cache_ep, [wpu], ["up"])
        yield self.add_relation_with_relation_units(
            varnish_ep, [vu1, vu2], ["up", "up"],
            wordpress_proxy_ep, [wpu], ["up"])

        state["relations"] = dict(
            wordpress=[wpu],
            mysql=[myu1, myu2],
            varnish=[vu1, vu2],
            memcache=[mc1, mc2]
            )
        returnValue(state)

    def mock_environment(self):
        mock_environment = self.mocker.patch(Environment)
        mock_environment.get_machine_provider()
        self.mocker.result(self.provider)


class StatusTest(StatusTestBase):

    @inlineCallbacks
    def add_provider_machine(self):
        m = yield self.add_machine_state()
        yield self.provider.start_machine(
            {"machine-id": self.machine_count,
             "dns-name": "steamcloud-%s.com" % self.machine_count})
        m.set_instance_id(self.machine_count)
        self.machine_count += 1
        returnValue(m)

    @inlineCallbacks
    def test_peer_relation(self):
        """Verify status works with peer relations.
        """
        m1 = yield self.add_provider_machine()
        m2 = yield self.add_provider_machine()

        riak = yield self.add_service_from_charm("riak")
        riak_u1 = yield self.add_unit(riak, m1)
        riak_u2 = yield self.add_unit(riak, m2, with_agent=lambda _: False)
        yield self.set_unit_state(riak_u1, "started")
        yield self.set_unit_state(riak_u2, "started")

        _, (peer_rel,) = yield self.relation_state_manager.add_relation_state(
            RelationEndpoint("riak", "peer", "ring", "peer"))

        riak_u1_relation = yield peer_rel.add_unit_state(riak_u1)
        riak_u1_workflow = ZookeeperWorkflowState(
            self.client, riak_u1_relation)
        with (yield riak_u1_workflow.lock()):
            yield riak_u1_workflow.set_state("up")
        yield peer_rel.add_unit_state(riak_u2)

        state = yield collect(
            ["riak"], self.provider, self.client, None)
        self.assertEqual(
            state["services"]["riak"],
            {"charm": "local:series/riak-7",
             "relations": {"ring": ["riak"]},
             "units": {"riak/0": {"machine": 0,
                                  "public-address": "riak-0.example.com",
                                  "agent-state": "started"},
                       "riak/1": {"machine": 1,
                                  "public-address": "riak-1.example.com",
                                  "agent-state": "down"}}})

    @inlineCallbacks
    def test_service_with_multiple_instances_of_named_relation(self):
        m1 = yield self.add_provider_machine()
        m2 = yield self.add_provider_machine()
        m3 = yield self.add_provider_machine()

        mysql = yield self.add_service_from_charm("mysql")
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db", "server")
        mysql_1 = yield self.add_unit(mysql, m1)

        myblog = yield self.add_service_from_charm(
            "myblog", charm_name="wordpress")
        myblog_db_ep = RelationEndpoint(
            "myblog", "client-server", "db", "client")
        myblog_1 = yield self.add_unit(myblog, m2)

        teamblog = yield self.add_service_from_charm(
            "teamblog", charm_id=(yield myblog.get_charm_id()))

        teamblog_db_ep = RelationEndpoint(
            "teamblog", "client-server", "db", "client")
        teamblog_1 = yield self.add_unit(teamblog, m3)

        yield self.add_relation_with_relation_units(
            mysql_ep, [mysql_1], ["up"],
            myblog_db_ep, [myblog_1], ["up"])

        yield self.add_relation_with_relation_units(
            mysql_ep, [mysql_1], ["up"],
            teamblog_db_ep, [teamblog_1], ["up"])

        state = yield collect(None, self.provider, self.client, None)
        self.assertEqual(
            state["services"]["mysql"]["units"]["mysql/0"],
            {"agent-state": "pending",
             "machine": 0,
             "public-address": None})
        self.assertEqual(
            state["services"]["mysql"]["relations"],
            {"db": ["myblog", "teamblog"]})

    @inlineCallbacks
    def test_service_with_multiple_rels_to_same_endpoint(self):
        m1 = yield self.add_provider_machine()
        m2 = yield self.add_provider_machine()
        mysql = yield self.add_service_from_charm("mysql")
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db", "server")
        mysql_1 = yield self.add_unit(mysql, m1)

        myblog = yield self.add_service_from_charm(
            "myblog", charm_name="funkyblog")
        write_db_ep = RelationEndpoint(
            "myblog", "client-server", "write-db", "client")
        read_db_ep = RelationEndpoint(
            "myblog", "client-server", "read-db", "client")
        myblog_1 = yield self.add_unit(myblog, m2)

        yield self.add_relation_with_relation_units(
            mysql_ep, [mysql_1], ["down"],
            write_db_ep, [myblog_1], ["up"])

        yield self.add_relation_with_relation_units(
            mysql_ep, [mysql_1], ["down"],
            read_db_ep, [myblog_1], ["up"])

        state = yield collect(None, self.provider, self.client, None)
        # Even though there are two relations to this service we
        # collapse to one the additional displays are redundant.
        self.assertEqual(
            state["services"]["mysql"]["relations"],
            {"db": ["myblog"]})
        self.assertEqual(
            state["services"]["mysql"]["units"]["mysql/0"],
            {"agent-state": "pending",
             "machine": 0,
             "relation-errors": {"db": ["myblog"]},
             "public-address": None})

    @inlineCallbacks
    def test_collect(self):
        yield self.build_topology(skip_unit_agents=("varnish/1",))

        agent = MachineAgent()
        options = TwistedOptionNamespace()
        options["juju_directory"] = self.makeDir()
        options["zookeeper_servers"] = get_test_zookeeper_address()
        options["session_file"] = self.makeFile()
        options["machine_id"] = "0"
        agent.configure(options)
        agent.set_watch_enabled(False)
        agent.client = self.client
        yield agent.start()

        # collect everything
        state = yield collect(None, self.provider, self.client, None)
        services = state["services"]
        self.assertIn("wordpress", services)
        self.assertIn("varnish", services)
        self.assertIn("mysql", services)

        # and verify the specifics of a single service
        self.assertTrue("mysql" in services)
        units = list(services["mysql"]["units"])
        self.assertEqual(len(units), 2)

        self.assertEqual(state["machines"][0],
                         {"instance-id": 0,
                          "instance-state": "unknown",
                          "dns-name": "steamcloud-1.com",
                          "agent-state": "running"})

        self.assertEqual(services["mysql"]["relations"],
                         {"db": ["wordpress"]})

        self.assertEqual(services["wordpress"]["relations"],
                         {"cache": ["memcache"],
                          "db": ["mysql"],
                          "proxy": ["varnish"]})

        self.assertEqual(
            services["varnish"],
            {"units":
                 {"varnish/1": {
                        "machine": 4,
                        "agent-state": "down",
                        "open-ports": ["80/tcp"],
                        "public-address": "varnish-1.example.com"},
                  "varnish/0": {
                        "machine": 3,
                        "agent-state": "started",
                        "public-address": "varnish-0.example.com",
                        "open-ports": ["80/tcp"]}},
             "exposed": True,
             "charm": "local:series/varnish-1",
             "relations": {"proxy": ["wordpress"]}})

        self.assertEqual(
            services["wordpress"],
            {"charm": "local:series/wordpress-3",
             "relations": {
                    "cache": ["memcache"],
                    "db": ["mysql"],
                    "proxy": ["varnish"]},
             "units": {
                    "wordpress/0": {
                        "machine": 0,
                        "public-address": "wordpress-0.example.com",
                        "agent-state": "started"}}})

        self.assertEqual(
            services["memcache"],
            {"charm": "local:series/dummy-1",
             "relations": {"cache": ["wordpress"]},
             "units": {
                 "memcache/0": {
                     "machine": 5,
                     "public-address": "memcache-0.example.com",
                     "agent-state": "pending"},
                 "memcache/1": {
                     "machine": 6,
                    "public-address": "memcache-1.example.com",
                     "relation-errors": {
                         "cache": ["wordpress"]},
                     "agent-state": "installed"}}}
            )

    @inlineCallbacks
    def test_collect_filtering(self):
        yield self.build_topology()

        # collect by service name
        state = yield collect(
            ["wordpress"], self.provider, self.client, None)
        # Validate that only the expected service is present
        # in the state
        self.assertEqual(state["machines"].keys(), [0])
        self.assertEqual(state["services"].keys(), ["wordpress"])

        # collect by unit name
        state = yield collect(["*/0"], self.provider, self.client, None)
        self.assertEqual(set(state["machines"].keys()), set([0, 1, 3, 5]))
        self.assertEqual(set(state["services"].keys()),
                         set(["memcache", "varnish", "mysql", "wordpress"]))

        # collect by unit name
        state = yield collect(["*/1"], self.provider, self.client, None)
        self.assertEqual(set(state["machines"].keys()), set([2, 4, 6]))

        # verify that only the proper units and services are present
        self.assertEqual(
            state["services"],
            {"memcache": {
                "charm": "local:series/dummy-1",
                    "relations": {"cache": ["wordpress"]},
                    "units": {
                        "memcache/1": {
                            "machine": 6,
                            "agent-state": "installed",
                            "public-address": "memcache-1.example.com",
                            "relation-errors": {"cache": ["wordpress"]}}}},
             "mysql": {
                    "exposed": True,
                    "charm": "local:series/mysql-1",
                    "relations": {"db": ["wordpress"]},
                    "units": {
                        "mysql/1": {
                            "machine": 2,
                            "public-address": "mysql-1.example.com",
                            "open-ports": [],
                            "agent-state": "stop-error",
                            "relation-errors": {"db": ["wordpress"]}}}},
             "varnish": {
                    "exposed": True,
                    "charm": "local:series/varnish-1",
                    "relations": {"proxy": ["wordpress"]},
                    "units": {
                        "varnish/1": {
                            "machine": 4,
                            "public-address": "varnish-1.example.com",
                            "open-ports": ["80/tcp"],
                            "agent-state": "started",
                            }}}})

        # filter a missing service
        state = yield collect(
            ["cluehammer"], self.provider, self.client, None)
        self.assertEqual(set(state["machines"].keys()), set([]))
        self.assertEqual(set(state["services"].keys()), set([]))

        # filter a missing unit
        state = yield collect(["*/7"], self.provider, self.client, None)
        self.assertEqual(set(state["machines"].keys()), set([]))
        self.assertEqual(set(state["services"].keys()), set([]))

    @inlineCallbacks
    def test_collect_with_unassigned_machines(self):
        yield self.build_topology()
        # get a service's units and unassign one of them
        wordpress = yield self.service_state_manager.get_service_state(
            "wordpress")
        units = yield wordpress.get_all_unit_states()
        # There is only a single wordpress machine in the topology.
        unit = units[0]
        machine_id = yield unit.get_assigned_machine_id()
        yield unit.unassign_from_machine()
        yield unit.set_public_address(None)
        # test that the machine is in state information w/o assignment
        state = yield collect(None, self.provider, self.client, None)
        # verify that the unassigned machine appears in the state
        self.assertEqual(state["machines"][machine_id],
                         {"dns-name": "steamcloud-1.com",
                          "instance-id": 0,
                          "instance-state": "unknown",
                          "agent-state": "not-started"})

        # verify that we have a record of the unassigned service;
        # but note that unassigning this machine without removing the
        # service unit and relation units now produces other dangling
        # records in the topology
        self.assertEqual(
            state["services"]["wordpress"]["units"],
            {"wordpress/0":
                 {"machine": None,
                  "public-address": None,
                  "agent-state": "started"}})

    @inlineCallbacks
    def test_collect_with_removed_unit(self):
        yield self.build_topology()
        # get a service's units and unassign one of them
        wordpress = yield self.service_state_manager.get_service_state(
            "wordpress")
        units = yield wordpress.get_all_unit_states()
        # There is only a single wordpress machine in the topology.
        unit = units[0]
        machine_id = yield unit.get_assigned_machine_id()
        yield wordpress.remove_unit_state(unit)

        # test that wordpress has no assigned service units
        state = yield collect(None, self.provider, self.client, None)
        self.assertEqual(
            state["services"]["wordpress"],
            {"charm": "local:series/wordpress-3",
             "relations": {"cache": ["memcache"],
                           "db": ["mysql"],
                           "proxy": ["varnish"]},
             "units": {}})

        # but its machine is still available as reported by status
        seen_machines = set()
        for service, service_data in state["services"].iteritems():
            for unit, unit_data in service_data["units"].iteritems():
                seen_machines.add(unit_data["machine"])
        self.assertIn(machine_id, state["machines"])
        self.assertNotIn(machine_id, seen_machines)

    @inlineCallbacks
    def test_provider_pending_machine_state(self):
        # verify that we get some error reporting if the provider
        # doesn't have proper machine info
        yield self.build_topology()

        # add a new machine to the topology (but not the provider)
        # and status it
        m8 = yield self.add_machine_state()
        wordpress = yield self.service_state_manager.get_service_state(
            "wordpress")
        wpu = yield wordpress.add_unit_state()
        yield wpu.assign_to_machine(m8)

        # test that we identify we don't have machine state
        state = yield collect(
            None, self.provider, self.client, logging.getLogger())
        self.assertEqual(state["machines"][7]["instance-id"],
                         "pending")

    @inlineCallbacks
    def test_render_yaml(self):
        yield self.build_topology()
        self.mock_environment()
        self.mocker.replay()

        yield status.status(self.environment, [],
                            status.render_yaml, self.output, None)
        state = yaml.load(self.output.getvalue())

        self.assertEqual(set(state["machines"].keys()),
                         set([0, 1, 2, 3, 4, 5, 6]))

        services = state["services"]

        self.assertEqual(set(services["memcache"].keys()),
                         set(["charm", "relations", "units"]))
        self.assertEqual(set(services["mysql"].keys()),
                         set(["exposed", "charm", "relations", "units"]))
        self.assertEqual(set(services["varnish"].keys()),
                         set(["exposed", "charm", "relations", "units"]))
        self.assertEqual(set(services["wordpress"].keys()),
                         set(["charm", "relations", "units"]))

        for service in services.itervalues():
            self.assertGreaterEqual(  # may also include "exposed" key
                set(service.keys()),
                set(["units", "relations", "charm"]))
            self.assertTrue(service["charm"].startswith("local:series/"))

        self.assertEqual(state["machines"][0],
                         {"instance-id": 0,
                          "instance-state": "unknown",
                          "dns-name": "steamcloud-1.com",
                          "agent-state": "down"})

        self.assertEqual(services["mysql"]["relations"],
                         {"db": ["wordpress"]})

        self.assertEqual(services["mysql"]["units"]["mysql/1"]["open-ports"],
                         [])

        self.assertEqual(services["wordpress"]["relations"],
                         {"cache": ["memcache"],
                          "db": ["mysql"],
                          "proxy": ["varnish"]})

    @inlineCallbacks
    def test_render_json(self):
        yield self.build_topology()
        self.mock_environment()
        self.mocker.replay()

        yield status.status(self.environment, [],
                            status.render_json, self.output, None)

        state = json.loads(self.output.getvalue())
        self.assertEqual(set(state["machines"].keys()),
                         set([unicode(i) for i in [0, 1, 2, 3, 4, 5, 6]]))

        services = state["services"]

        self.assertEqual(set(services["memcache"].keys()),
                         set(["charm", "relations", "units"]))
        self.assertEqual(set(services["mysql"].keys()),
                         set(["exposed", "charm", "relations", "units"]))
        self.assertEqual(set(services["varnish"].keys()),
                         set(["exposed", "charm", "relations", "units"]))
        self.assertEqual(set(services["wordpress"].keys()),
                         set(["charm", "relations", "units"]))

        for service in services.itervalues():
            self.assertTrue(service["charm"].startswith("local:series/"))

        self.assertEqual(state["machines"][u"0"],
                         {"instance-id": 0,
                          "instance-state": "unknown",
                          "dns-name": "steamcloud-1.com",
                          "agent-state": "down"})

        self.assertEqual(services["mysql"]["relations"],
                         {"db": ["wordpress"]})

        self.assertEqual(services["mysql"]["units"]["mysql/1"]["open-ports"],
                         [])

        self.assertEqual(services["wordpress"]["relations"],
                         {"cache": ["memcache"],
                          "db": ["mysql"],
                          "proxy": ["varnish"]})

        self.assertEqual(
            services["varnish"],
            {
                "exposed": True,
                "units":
                    {"varnish/1": {
                        "machine": 4,
                        "public-address": "varnish-1.example.com",
                        "open-ports": ["80/tcp"],
                        "agent-state": "started"},
                     "varnish/0": {
                        "machine": 3,
                        "public-address": "varnish-0.example.com",
                        "open-ports": ["80/tcp"],
                        "agent-state": "started"},
                     },
                     "charm": "local:series/varnish-1",
                "relations": {"proxy": ["wordpress"]}})

    @inlineCallbacks
    def test_render_dot(self):
        yield self.build_topology()
        self.mock_environment()
        self.mocker.replay()

        yield status.status(self.environment, [],
                            status.render_dot, self.output, None)

        result = self.output.getvalue()
        #dump_stringio(self.output, "/tmp/ens.dot")

        # make mild assertions about the expected DOT output
        # because the DOT language is simple we can test that some
        # relationships are present
        self.assertIn('memcache -> "memcache/1"', result)
        self.assertIn('varnish -> "varnish/0"', result)
        self.assertIn('varnish -> "varnish/1"', result)

        # test that relationships are being rendered
        self.assertIn("wordpress -> memcache", result)
        self.assertIn("mysql -> wordpress", result)

        # assert that properties were applied to a relationship
#        self.assertIn("wordpress -> varnish  [dir=none, "
#                      "label=\"varnish:wordpress/proxy\"]",
#                      result)

        # verify that the renderer picked up the DNS name of the
        # machines (and they are associated with the proper machine)
        self.assertIn(
            '"mysql/0" [color="#DD4814", fontcolor="#ffffff", '
            "shape=box, style=filled, label=<mysql/0<br/><i>mysql-0."
            "example.com</i>>]",
            result)
        self.assertIn(
            '"mysql/1" [color="#DD4814", fontcolor="#ffffff", shape=box, style=filled, label=<mysql/1<br/><i>mysql-1.example.com</i>>]',
            result)

        # Check the charms are present in the service node.
        self.assertIn(
            'memcache [color="#772953", fontcolor="#ffffff", shape=component, style=filled, label=<memcache<br/>local:series/dummy-1>]', result)
        self.assertIn(
            'varnish [color="#772953", fontcolor="#ffffff", shape=component, style=filled, label=<varnish<br/>local:series/varnish-1>]',result)
        self.assertIn(
            'mysql [color="#772953", fontcolor="#ffffff", shape=component, style=filled, label=<mysql<br/>local:series/mysql-1>]', result)

        self.assertIn("local:series/dummy-1", result)

    def test_render_dot_bad_clustering(self):
        """Test around Bug #792448.

        Deployment producing bad status dot output, but sane normal
        output.
        """
        self.mocker.replay()

        output = StringIO()
        renderer = status.renderers["dot"]

        renderer(sample_cluster, output, self.environment, format="dot")

        # Verify that the invalid names were properly corrected
        self.assertIn("subgraph cluster_wiki_db {",
                      output.getvalue())
        self.assertIn('wiki_cache -> "wiki_cache/0"',
                      output.getvalue())

    @inlineCallbacks
    def test_render_svg(self):
        yield self.build_topology()
        self.mock_environment()
        self.mocker.replay()

        yield status.status(self.environment, [],
                            status.renderers["svg"],
                            self.output,
                            None)

        # look for a hint the process completed.
        self.assertIn("</svg>", self.output.getvalue())

    @inlineCallbacks
    def test_subordinate_status_output(self):
        state = yield self.build_topology()
        # supplement status with additional subordinates
        # add logging to mysql and wordpress
        logging = yield self.add_service_from_charm("logging")

        mysql_ep = RelationEndpoint("mysql", "client-server",
                                    "juju-info", "server")
        wordpress_db_ep = RelationEndpoint("wordpress", "client-server",
                                           "juju-info", "server")
        logging_ep = RelationEndpoint("logging", "client-server",
                                      "juju-info", "client", "container")

        my_log_rel, my_log_services = (
            yield self.relation_state_manager.add_relation_state(
                mysql_ep, logging_ep))
        wp_log_rel, wp_log_services = (
            yield self.relation_state_manager.add_relation_state(
            wordpress_db_ep, logging_ep))

        units = state["units"]
        log_units = units.setdefault("logging", {})
        wp1 = iter(units["wordpress"]).next()
        mu1, mu2 = list(units["mysql"])

        yield self.add_unit(logging, None, container=mu1, units=log_units)
        yield self.add_unit(logging, None, container=wp1, units=log_units)
        yield self.add_unit(logging, None, container=mu2, units=log_units)

        self.mock_environment()
        self.mocker.replay()

        yield status.status(self.environment, [],
                            status.render_yaml, self.output, None)

        state = yaml.load(self.output.getvalue())

        # verify our changes
        log_state = state["services"]["logging"]

        self.assertEqual(set(log_state["relations"]["juju-info"]),
                         set(["mysql", "wordpress"]))
        self.assertEqual(set(log_state["subordinate-to"]),
                         set(["mysql", "wordpress"]))

        wp_state = state["services"]["wordpress"]
        self.assertEqual(wp_state["relations"]["juju-info"], ["logging"])
        wp_subs = wp_state["units"]["wordpress/0"]["subordinates"]
        logging_sub = wp_subs["logging/1"]
        # this assertion verifies that we don't see keys we don't
        # expect as well
        self.assertEqual(logging_sub, {"agent-state": "pending"})

    @inlineCallbacks
    def test_subordinate_status_output_no_container(self):
        state = yield self.build_topology()
        # supplement status with additional subordinates
        # add logging to mysql and wordpress
        logging = yield self.add_service_from_charm("logging")

        mysql_ep = RelationEndpoint("mysql", "client-server",
                                    "juju-info", "server")
        wordpress_db_ep = RelationEndpoint("wordpress", "client-server",
                                           "juju-info", "server")
        logging_ep = RelationEndpoint("logging", "client-server",
                                      "juju-info", "client", "container")

        my_log_rel, my_log_services = (
            yield self.relation_state_manager.add_relation_state(
                mysql_ep, logging_ep))
        wp_log_rel, wp_log_services = (
            yield self.relation_state_manager.add_relation_state(
            wordpress_db_ep, logging_ep))

        units = state["units"]
        log_units = units.setdefault("logging", {})
        wp1 = iter(units["wordpress"]).next()
        mu1, mu2 = list(units["mysql"])

        yield self.add_unit(logging, None, container=mu1, units=log_units)
        yield self.add_unit(logging, None, container=wp1, units=log_units)
        yield self.add_unit(logging, None, container=mu2, units=log_units)

        # remove mysql/0
        yield state["services"]["mysql"].remove_unit_state(mu1)

        self.mock_environment()
        self.mocker.replay()

        yield status.status(self.environment, [],
                            status.render_yaml, self.output, None)

        output = yaml.load(self.output.getvalue())
        self.assertNotIn(mu1.unit_name, output["services"]["mysql"]["units"])
        self.assertIn(mu2.unit_name, output["services"]["mysql"]["units"])
