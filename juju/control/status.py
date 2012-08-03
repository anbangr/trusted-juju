from fnmatch import fnmatch
import argparse
import functools
import json
import sys

from twisted.internet.defer import inlineCallbacks, returnValue
import yaml

from juju.control.utils import get_environment
from juju.errors import ProviderError
from juju.state.errors import UnitRelationStateNotFound
from juju.state.charm import CharmStateManager
from juju.state.machine import MachineStateManager
from juju.state.service import ServiceStateManager, parse_service_name
from juju.state.relation import RelationStateManager
from juju.unit.workflow import WorkflowStateClient

# a minimal registry for renderers
# maps from format name to callable
renderers = {}


def configure_subparser(subparsers):
    sub_parser = subparsers.add_parser(
        "status", help=status.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=command.__doc__)

    sub_parser.add_argument(
        "--environment", "-e",
        help="Environment to status.")

    sub_parser.add_argument("--output",
                            help="An optional filename to output "
                            "the result to",
                            type=argparse.FileType("w"),
                            default=sys.stdout)

    sub_parser.add_argument("--format",
                            help="Select an output format",
                            default="yaml"
                           )

    sub_parser.add_argument("scope",
                            nargs="*",
                            help="""scope of status request, service or unit"""
                                 """ must match at least one of these""")

    return sub_parser


def command(options):
    """Output status information about a deployment.

    This command will report on the runtime state of various system
    entities.

    $ juju status

    will return data on entire default deployment.

    $ juju status -e DEPLOYMENT2

    will return data on the DEPLOYMENT2 envionment.
    """
    environment = get_environment(options)
    renderer = renderers.get(options.format)
    if renderer is None:
        formats = sorted(renderers.keys())
        formats = ", ".join(formats)

        raise SystemExit(
            "Unsupported render format %s (valid formats: %s)." % (
                options.format, formats))

    return status(environment,
                  options.scope,
                  renderer,
                  options.output,
                  options.log)


@inlineCallbacks
def status(environment, scope, renderer, output, log):
    """Display environment status information.
    """
    provider = environment.get_machine_provider()
    client = yield provider.connect()
    try:
        # Collect status information
        command = StatusCommand(client, provider, log)
        state = yield command(scope)
        #state = yield collect(scope, provider, client, log)
    finally:
        yield client.close()
    # Render
    renderer(state, output, environment)


def digest_scope(scope):
    """Parse scope used to filter status information.

    `scope`: a list of name specifiers. see collect()

    Returns a tuple of (service_filter, unit_filter). The values in
    either filter list will be passed as a glob to fnmatch
    """

    services = []
    units = []

    if scope is not None:
        for value in scope:
            if "/" in value:
                units.append(value)
            else:
                services.append(value)

    return (services, units)


class StatusCommand(object):
    def __init__(self, client, provider, log):
        """
        Callable status command object.

        `client`: ZK client connection
        `provider`: machine provider for the environment
        `log`: a Python stdlib logger.

        """
        self.client = client
        self.provider = provider
        self.log = log

        self.service_manager = ServiceStateManager(client)
        self.relation_manager = RelationStateManager(client)
        self.machine_manager = MachineStateManager(client)
        self.charm_manager = CharmStateManager(client)
        self._reset()

    def _reset(self, scope=None):
        # init per-run state
        # self.state is assembled by the various process methods
        # intermediate access to state is made more convenient
        # using these references to its internals.
        self.service_data = {}  # service name: service info
        self.machine_data = {}  # machine id: machine state
        self.unit_data = {}  # unit_name :unit_info

        # used in collecting subordinate (which are added to state in a two
        # phase pass)
        self.subordinates = {}  # service : set(principal service names)

        self.state = dict(services=self.service_data,
                     machines=self.machine_data)

        # Filtering info
        self.seen_machines = set()
        self.filter_services, self.filter_units = digest_scope(scope)

    @inlineCallbacks
    def __call__(self, scope=None):
        """Extract status information into nested dicts for rendering.

        `scope`: an optional list of name specifiers. Globbing based wildcards
        supported. Defaults to all units, services and relations.

        """
        self._reset(scope)

        # Pass 1 Gather Data (including principals and subordinates)
        # this builds unit info  and container relationships
        # which is assembled in pass 2 below
        yield self._process_services()

        # Pass 2: Nest information according to principal/subordinates
        # rules
        self._process_subordinates()

        yield self._process_machines()

        returnValue(self.state)

    @inlineCallbacks
    def _process_services(self):
        """
        For each service gather the following information::

          <service name>:
            charm: <charm name>
            exposed: <expose boolean>
            relations:
                 <relation info -- see _process_relations>
            units:
                 <unit info -- see _process_units>
        """
        services = yield self.service_manager.get_all_service_states()
        for service in services:
            if len(self.filter_services):
                found = False
                for filter_service in self.filter_services:
                    if fnmatch(service.service_name, filter_service):
                        found = True
                        break
                if not found:
                    continue
            yield self._process_service(service)

    @inlineCallbacks
    def _process_service(self, service):
        """
        Gather the service info (described in _process_services).

        `service`: ServiceState instance
        """

        relation_data = {}
        service_data = self.service_data

        charm_id = yield service.get_charm_id()
        charm = yield self.charm_manager.get_charm_state(charm_id)

        service_data[service.service_name] = (
            dict(units={},
                 charm=charm.id,
                 relations=relation_data))

        if (yield service.is_subordinate()):
            service_data[service.service_name]["subordinate"] = True

        yield self._process_expose(service)

        relations, rel_svc_map = yield self._process_relation_map(
            service)

        unit_matched = yield self._process_units(service,
                                                 relations,
                                                 rel_svc_map)

        # after filtering units check if any matched or remove the
        # service from the output
        if self.filter_units and not unit_matched:
            del service_data[service.service_name]
            return

        yield self._process_relations(service, relations, rel_svc_map)

    @inlineCallbacks
    def _process_units(self, service, relations, rel_svc_map):
        """
        Gather unit information for a service::

            <unit name>:
                agent-state: <started|pendding|etc>
                machine: <machine id>
                open-ports: ["port/protocol", ...]
                public-address: <public dns name or ip>
                subordinates:
                     <optional nested units of subordinate services>


        `service`: ServiceState intance
        `relations`: list of ServiceRelationState instance for this service
        `rel_svc_map`: maps relation internal ids to the remote endpoint
                       service name. This references the name of the remote
                       endpoint and so is generated per service.
        """
        units = yield service.get_all_unit_states()
        unit_matched = False

        for unit in units:
            if len(self.filter_units):
                found = False
                for filter_unit in self.filter_units:
                    if fnmatch(unit.unit_name, filter_unit):
                        found = True
                        break
                if not found:
                    continue
            yield self._process_unit(service, unit, relations, rel_svc_map)
            unit_matched = True
        returnValue(unit_matched)

    @inlineCallbacks
    def _process_unit(self, service, unit, relations, rel_svc_map):
        """ Generate unit info for a single unit of a single service.

        `unit`: ServiceUnitState
        see `_process_units` for an explanation of other arguments.

        """
        u = self.unit_data[unit.unit_name] = dict()
        container = yield unit.get_container()

        if container:
            u["container"] = container.unit_name
            self.subordinates.setdefault(unit.service_name,
                                    set()).add(container.service_name)

        machine_id = yield unit.get_assigned_machine_id()
        u["machine"] = machine_id
        unit_workflow_client = WorkflowStateClient(self.client, unit)
        unit_state = yield unit_workflow_client.get_state()
        if not unit_state:
            u["agent-state"] = "pending"
        else:
            unit_connected = yield unit.has_agent()
            u["agent-state"] = unit_state.replace("_", "-") \
                               if unit_connected else "down"

        exposed = self.service_data[service.service_name].get("exposed")
        if exposed:
            open_ports = yield unit.get_open_ports()
            u["open-ports"] = ["{port}/{proto}".format(**port_info)
                               for port_info in open_ports]

        u["public-address"] = yield unit.get_public_address()

        # indicate we should include information about this
        # machine later
        self.seen_machines.add(machine_id)

        # collect info on each relation for the service unit
        yield self._process_unit_relations(service, unit,
                                           relations, rel_svc_map)

    @inlineCallbacks
    def _process_relation_map(self, service):
        """Generate a mapping from a services relations to the service name of
        the remote endpoints.

        returns: ([ServiceRelationState, ...], mapping)
        """
        relation_data = self.service_data[service.service_name]["relations"]
        relation_mgr = self.relation_manager
        relations = yield relation_mgr.get_relations_for_service(service)
        rel_svc_map = {}

        for relation in relations:
            rel_services = yield relation.get_service_states()

            # A single related service implies a peer relation. More
            # imply a bi-directional provides/requires relationship.
            # In the later case we omit the local side of the relation
            # when reporting.
            if len(rel_services) > 1:
                # Filter out self from multi-service relations.
                rel_services = [
                    rsn for rsn in rel_services if rsn.service_name !=
                    service.service_name]

            if len(rel_services) > 1:
                raise ValueError("Unexpected relationship with more "
                                 "than 2 endpoints")

            rel_service = rel_services[0]
            relation_data.setdefault(relation.relation_name, set()).add(
                rel_service.service_name)
            rel_svc_map[relation.internal_relation_id] = (
                 rel_service.service_name)

        returnValue((relations, rel_svc_map))

    @inlineCallbacks
    def _process_relations(self, service, relations, rel_svc_map):
        """Generate relation information for a given service

        Each service with relations will have a relations dict
        nested under it with one or more relations described::

           relations:
              <relation name>:
              - <remote service name>

        """
        relation_data = self.service_data[service.service_name]["relations"]

        for relation in relations:
            rel_services = yield relation.get_service_states()

            # A single related service implies a peer relation. More
            # imply a bi-directional provides/requires relationship.
            # In the later case we omit the local side of the relation
            # when reporting.
            if len(rel_services) > 1:
                # Filter out self from multi-service relations.
                rel_services = [
                    rsn for rsn in rel_services if rsn.service_name !=
                    service.service_name]

            if len(rel_services) > 1:
                raise ValueError("Unexpected relationship with more "
                                 "than 2 endpoints")

            rel_service = rel_services[0]
            relation_data.setdefault(
                relation.relation_name, set()).add(
                    rel_service.service_name)
            rel_svc_map[relation.internal_relation_id] = (
                rel_service.service_name)

        # Normalize the sets back to lists
        for r in relation_data:
            relation_data[r] = sorted(relation_data[r])

    @inlineCallbacks
    def _process_unit_relations(self, service, unit, relations, rel_svc_map):
        """Collect UnitRelationState information per relation and per unit.

        Includes information under each unit for its relations including
        its relation state and information about any possible errors.

        see `_process_relations` for argument information
        """
        u = self.unit_data[unit.unit_name]
        relation_errors = {}

        for relation in relations:
            try:
                relation_unit = yield relation.get_unit_state(unit)
            except UnitRelationStateNotFound:
                # This exception will occur when relations are
                # established between services without service
                # units, and therefore never have any
                # corresponding service relation units.
                # UPDATE: common with subordinate services, and
                # some testing scenarios.
                continue
            relation_workflow_client = WorkflowStateClient(
                self.client, relation_unit)
            workflow_state = yield relation_workflow_client.get_state()

            rel_svc_name = rel_svc_map.get(relation.internal_relation_id)
            if rel_svc_name and workflow_state not in ("up", None):
                relation_errors.setdefault(
                    relation.relation_name, set()).add(rel_svc_name)

        if relation_errors:
            # Normalize sets and store.
            u["relation-errors"] = dict(
                [(r, sorted(relation_errors[r])) for r in relation_errors])

    def _process_subordinates(self):
        """Properly nest subordinate units under their principal service's
        unit nodes. Services and units are generated in one pass, then
        iterated by this method to structure the output data to reflect
        actual unit containment.

        Subordinate units will include the follow::
           subordinate: true
            subordinate-to:
            - <principal service names>

        Principal services that have subordinates will include::

            subordinates:
              <subordinate unit name>:
                agent-state: <agent state>
        """
        service_data = self.service_data

        for unit_name, u in self.unit_data.iteritems():
            container = u.get("container")
            if container:
                d = self.unit_data[container].setdefault("subordinates", {})
                d[unit_name] = u

                # remove keys that don't appear in output or come from container
                for key in ("container", "machine", "public-address"):
                    u.pop(key, None)
            else:
                service_name = parse_service_name(unit_name)
                service_data[service_name]["units"][unit_name] = u

        for sub_service, principal_services in self.subordinates.iteritems():
            service_data[sub_service]["subordinate-to"] = sorted(principal_services)
            service_data[sub_service].pop("units", None)

    @inlineCallbacks
    def _process_expose(self, service):
        """Indicate if a service is exposed or not."""
        exposed = yield service.get_exposed_flag()
        if exposed:
            self.service_data[service.service_name].update(exposed=exposed)
        returnValue(exposed)

    @inlineCallbacks
    def _process_machines(self):
        """Gather machine information.

        machines:
          <machine id>:
            agent-state: <agent state>
            dns-name: <dns name>
            instance-id: <provider specific instance id>
            instance-state: <instance state>
        """

        machines = yield self.machine_manager.get_all_machine_states()
        for machine_state in machines:
            if (self.filter_services or self.filter_units) and \
                    machine_state.id not in self.seen_machines:
                continue
            yield self._process_machine(machine_state)

    @inlineCallbacks
    def _process_machine(self, machine_state):
        """
        `machine_state`: MachineState instance
        """
        instance_id = yield machine_state.get_instance_id()
        m = {"instance-id": instance_id \
             if instance_id is not None else "pending"}
        if instance_id is not None:
            try:
                pm = yield self.provider.get_machine(instance_id)
                m["dns-name"] = pm.dns_name
                m["instance-state"] = pm.state
                if (yield machine_state.has_agent()):
                    # if the agent's connected, we're fine
                    m["agent-state"] = "running"
                else:
                    units = (
                        yield machine_state.get_all_service_unit_states())
                    for unit in units:
                        unit_workflow_client = WorkflowStateClient(
                            self.client, unit)
                        if (yield unit_workflow_client.get_state()):
                            # for unit to have a state, its agent must
                            # have run, which implies the machine agent
                            # must have been running correctly at some
                            # point in the past
                            m["agent-state"] = "down"
                            break
                    else:
                        # otherwise we're probably just still waiting
                        m["agent-state"] = "not-started"
            except ProviderError:
                # The provider doesn't have machine information
                self.log.error(
                    "Machine provider information missing: machine %s" % (
                        machine_state.id))

        self.machine_data[machine_state.id] = m


def render_yaml(data, filelike, environment):
    # remove the root nodes empty name
    yaml.safe_dump(data, filelike, default_flow_style=False)

renderers["yaml"] = render_yaml


def jsonify(data, filelike, pretty=True, **kwargs):
    args = dict(skipkeys=True)
    args.update(kwargs)
    if pretty:
        args["sort_keys"] = True
        args["indent"] = 4
    return json.dump(data, filelike, **args)


def render_json(data, filelike, environment):
    jsonify(data, filelike)

renderers["json"] = render_json


# Supplement kwargs provided to pydot.Cluster/Edge/Node.
# The first key is used as the data type selector.
DEFAULT_STYLE = {
    "service_container": {
        "bgcolor": "#dedede",
        },
    "service": {
        "color": "#772953",
        "shape": "component",
        "style": "filled",
        "fontcolor": "#ffffff",
        },
    "unit": {
        "color": "#DD4814",
        "fontcolor": "#ffffff",
        "shape": "box",
        "style": "filled",
        },

    "subunit": {
        "color": "#c9c9c9",
        "fontcolor": "#ffffff",
        "shape": "box",
        "style": "filled",
        "rank": "same"
        },

    "relation": {
        "dir": "none"}
    }


def safe_dot_label(name):
    """Convert a name to a label safe for use in DOT.

    Works around an issue where service names like wiki-db will produce DOT
    items with names like cluster_wiki-db where the trailing '-' invalidates
    the name.

    """
    return name.replace("-", "_")


def render_dot(
    data, filelike, environment, format="dot", style=DEFAULT_STYLE):
    """Render a graphiz output of the status information.
    """
    try:
        import pydot
    except ImportError:
        raise SystemExit("You need to install the pydot "
                         "library to support DOT visualizations")

    dot = pydot.Dot(graph_name=environment.name)
    # first create a cluster for each service
    seen_relations = set()
    for service_name, service in data["services"].iteritems():

        cluster = pydot.Cluster(
            safe_dot_label(service_name),
            shape="component",
            label="%s service" % service_name,
            **style["service_container"])

        snode = pydot.Node(safe_dot_label(service_name),
                           label="<%s<br/>%s>" % (
                               service_name,
                               service["charm"]),
                           **style["service"])
        cluster.add_node(snode)

        for unit_name, unit in service.get("units", {}).iteritems():
            subordinates = unit.get("subordinates")
            if subordinates:
                container = pydot.Subgraph()
                un = pydot.Node(safe_dot_label(unit_name),
                                    label="<%s<br/><i>%s</i>>" % (
                                        unit_name,
                                        unit.get("public-address")),
                                    **style["unit"])
                container.add_node(un)
                for sub in subordinates:
                    s = pydot.Node(safe_dot_label(sub),
                                   label="<%s<br/>>" % (sub),
                                   **style["subunit"])
                    container.add_node(s)
                    container.add_edge(pydot.Edge(un, s, **style["relation"]))
                cluster.add_subgraph(container)
            else:
                un = pydot.Node(safe_dot_label(unit_name),
                                label="<%s<br/><i>%s</i>>" % (
                                    unit_name,
                                    unit.get("public-address")),
                                **style["unit"])
                cluster.add_node(un)

            cluster.add_edge(pydot.Edge(snode, un))

        dot.add_subgraph(cluster)

        # now map the relationships
        for kind, relation in service["relations"].iteritems():
            if not isinstance(relation, list):
                relation = (relation,)
            for rel in relation:
                src = safe_dot_label(rel)
                dest = safe_dot_label(service_name)
                descriptor = ":".join(tuple(sorted((src, dest))))
                #kind = safe_dot_label("%s/%s" % (descriptor, kind))
                if descriptor not in seen_relations:
                    seen_relations.add(descriptor)
                    dot.add_edge(pydot.Edge(
                            src,
                            dest,
                            label=kind,
                            **style["relation"]
                        ))

    if format == "dot":
        filelike.write(dot.to_string())
    else:
        filelike.write(dot.create(format=format))

renderers["dot"] = render_dot
renderers["svg"] = functools.partial(render_dot, format="svg")
renderers["png"] = functools.partial(render_dot, format="png")
