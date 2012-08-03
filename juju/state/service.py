import yaml
import zookeeper

from twisted.internet.defer import (
    inlineCallbacks, returnValue, maybeDeferred)

from txzookeeper.utils import retry_change

from juju.charm.url import CharmURL
from juju.state.agent import AgentStateMixin
from juju.state.base import log, StateBase
from juju.state.endpoint import RelationEndpoint
from juju.state.environment import EnvironmentStateManager
from juju.state.errors import (
    StateChanged, ServiceStateNotFound, ServiceUnitStateNotFound,
    ServiceUnitStateMachineAlreadyAssigned, ServiceStateNameInUse,
    BadDescriptor, BadServiceStateName, NoUnusedMachines,
    ServiceUnitDebugAlreadyEnabled, ServiceUnitResolvedAlreadyEnabled,
    ServiceUnitRelationResolvedAlreadyEnabled, StopWatcher,
    IllegalSubordinateMachineAssignment, PrincipalServiceUnitRequired,
    NotSubordinateCharm, ServiceUnitUpgradeAlreadyEnabled)

from juju.state.charm import CharmStateManager
from juju.state.relation import ServiceRelationState, RelationStateManager
from juju.state.machine import _public_machine_id, MachineState
from juju.state.topology import InternalTopologyError
from juju.state.utils import (
    remove_tree, dict_merge, YAMLState, YAMLStateNodeMixin)

RETRY_HOOKS = 1000
NO_HOOKS = 1001


def _series_constraints(base_constraints, charm_id):
    series = CharmURL.parse(charm_id).collection.series
    return base_constraints.with_series(series)


class ServiceStateManager(StateBase):
    """Manages the state of services in an environment."""

    @inlineCallbacks
    def add_service_state(self, service_name, charm_state, constraints):
        """Create a new service with the given name.

        @param service_name: Unique name of the service created.
        @param charm_state: CharmState for the service.
        @param constraints: Constraints needed to deploy the service.

        @return: ServiceState for the created service.
        """
        charm_id = charm_state.id
        constraints = _series_constraints(constraints, charm_id)
        service_details = {
            "charm": charm_id, "constraints": constraints.data}
        # charm metadata is always decoded into unicode, ensure any
        # serialized state references strings to avoid tying to py runtime.
        node_data = yaml.safe_dump(service_details)
        path = yield self._client.create("/services/service-", node_data,
                                         flags=zookeeper.SEQUENCE)
        internal_id = path.rsplit("/", 1)[1]

        # create a child node for configuration options
        yield self._client.create("%s/config" % path, yaml.dump({}))

        def add_service(topology):
            if topology.find_service_with_name(service_name):
                raise ServiceStateNameInUse(service_name)
            topology.add_service(internal_id, service_name)

        yield self._retry_topology_change(add_service)

        returnValue(ServiceState(self._client, internal_id, service_name))

    @inlineCallbacks
    def remove_service_state(self, service_state):
        """Remove the service's state.

        This will destroy any existing units, and break any existing
        relations of the service.
        """
        # Remove relations first, to prevent spurious hook execution.
        relation_manager = RelationStateManager(self._client)
        relations = yield relation_manager.get_relations_for_service(
            service_state)

        for relation_state in relations:
            yield relation_manager.remove_relation_state(relation_state)

        # Remove the units
        unit_names = yield service_state.get_unit_names()
        for unit_name in unit_names:
            unit_state = yield service_state.get_unit_state(unit_name)
            yield service_state.remove_unit_state(unit_state)

        # Remove the service from the topology.
        def remove_service(topology):
            if not topology.has_service(service_state.internal_id):
                raise StateChanged()
            topology.remove_service(service_state.internal_id)

        yield self._retry_topology_change(remove_service)

        # Remove any remaining state
        yield remove_tree(
            self._client, "/services/%s" % service_state.internal_id)

    @inlineCallbacks
    def get_service_state(self, service_name):
        """Return a service state with the given name.

        @return ServiceState with the given name.

        @raise ServiceStateNotFound if the unit id is not found.
        """
        topology = yield self._read_topology()
        internal_id = topology.find_service_with_name(service_name)
        if internal_id is None:
            raise ServiceStateNotFound(service_name)
        returnValue(ServiceState(self._client, internal_id, service_name))

    @inlineCallbacks
    def get_unit_state(self, unit_name):
        """Returns the unit state with the given name.

        A convience api to retrieve a unit in one api call. May raise
        exceptions regarding the nonexistance of either the service or unit.
        """
        if not "/" in unit_name:
            raise ServiceUnitStateNotFound(unit_name)

        service_name, _ = unit_name.split("/")
        service_state = yield self.get_service_state(service_name)
        unit_state = yield service_state.get_unit_state(unit_name)
        returnValue(unit_state)

    @inlineCallbacks
    def get_all_service_states(self):
        """Get all the deployed services in the environment.

        @return: list of ServiceState instances.
        """
        topology = yield self._read_topology()
        services = []
        for service_id in topology.get_services():
            service_name = topology.get_service_name(service_id)
            service = ServiceState(self._client, service_id, service_name)
            services.append(service)
        returnValue(services)

    @inlineCallbacks
    def get_relation_endpoints(self, descriptor):
        """Get all relation endpoints for `descriptor`.

        A `descriptor` is of the form ``<service name>[:<relation name>]``.
        Returns the following:

          - Returns a list of matching endpoints, drawn from the
            peers, provides, and requires interfaces. An empty list is
            returned if there are no endpoints matching the
            `descriptor`. This list is sorted such that implicit
            relations appear last.

          - Raises a `BadDescriptor` exception if `descriptor` cannot
            be parsed.

        """
        tokens = descriptor.split(":")
        if len(tokens) == 1 and bool(tokens[0]):
            query_service_name, query_relation_name = descriptor, None
        elif len(tokens) == 2 and bool(tokens[0]) and bool(tokens[1]):
            query_service_name, query_relation_name = tokens
        else:
            raise BadDescriptor(descriptor)

        service_state = yield self.get_service_state(
            query_service_name)
        charm_state = yield service_state.get_charm_state()
        charm_metadata = yield charm_state.get_metadata()
        endpoints = set()
        relation_role_map = {
            "peer": "peers", "client": "requires", "server": "provides"}
        for relation_role in ("peer", "client", "server"):
            relations = getattr(
                charm_metadata, relation_role_map[relation_role])
            if relations:
                for relation_name, spec in relations.iteritems():
                    if (query_relation_name is None or
                        query_relation_name == relation_name):
                        endpoints.add(RelationEndpoint(
                            service_name=query_service_name,
                            relation_type=spec["interface"],
                            relation_name=relation_name,
                            relation_role=relation_role,
                            relation_scope=spec["scope"]))

        # Add in implicit relations
        endpoints.add(
            RelationEndpoint(
                query_service_name, "juju-info", "juju-info", "server"))

        # When offering matches implicit relations should be
        # considered last. This cmpfunc pushes implicit methods to
        # the end of the list of possible endpoints.
        def low_priority_implicit_cmp(rel1, rel2):
            if rel1.relation_name.startswith("juju-"):
                return 1
            if rel2.relation_name.startswith("juju-"):
                return -1
            return cmp(rel1.relation_name, rel2.relation_name)

        endpoints = sorted(endpoints, low_priority_implicit_cmp)

        returnValue(endpoints)

    @inlineCallbacks
    def join_descriptors(self, descriptor1, descriptor2):
        """Return a list of pairs of RelationEndpoints joining descriptors."""
        result = []
        relations_1 = yield self.get_relation_endpoints(descriptor1)
        relations_2 = yield self.get_relation_endpoints(descriptor2)

        for relation1 in relations_1:
            for relation2 in relations_2:
                if relation1.may_relate_to(relation2):
                    result.append((relation1, relation2))
        returnValue(result)

    def watch_service_states(self, callback):
        """Observe changes in the known services via `callback`.

        `callback(old_service_names, new_service_names)`: function called
           upon a change to the service topology. `old_service_names`
           and `new_service_names` are both sets, possibly empty.

        Note that there are no guarantees that this function will be
        called once for *every* change in the topology, which means
        that multiple modifications may be observed as a single call.

        This method currently sets a pretty much perpetual watch
        (errors will make it bail out).  In the future, the return
        value of the watch function may be used to define whether to
        continue watching or to stop.
        """

        def watch_topology(old_topology, new_topology):

            def get_service_names(topology):
                service_names = set()
                if topology is None:
                    return service_names
                for service_id in topology.get_services():
                    service_names.add(topology.get_service_name(service_id))
                return service_names

            old_services = get_service_names(old_topology)
            new_services = get_service_names(new_topology)
            if old_services != new_services:
                return callback(old_services, new_services)

        return self._watch_topology(watch_topology)


class ServiceState(StateBase, YAMLStateNodeMixin):
    """State of a service registered in an environment.

    Each service is composed by units, and each unit represents an
    actual deployment of software to satisfy the needs defined in
    this service state.
    """

    def __init__(self, client, internal_id, service_name):
        super(ServiceState, self).__init__(client)
        self._internal_id = internal_id
        self._service_name = service_name

    def __hash__(self):
        return hash(self.internal_id)

    def __eq__(self, other):
        if not isinstance(other, ServiceState):
            return False
        return self.internal_id == other.internal_id

    def __repr__(self):
        return "<%s %s>" % (
            self.__class__.__name__,
            self.internal_id)

    @property
    def service_name(self):
        """Name of the service represented by this state.
        """
        return self._service_name

    @property
    def internal_id(self):
        return self._internal_id

    @property
    def _zk_path(self):
        """Return the path within zookeeper.

        This attribute should not be used outside of the .state
        package or for debugging.
        """
        return "/services/" + self._internal_id

    @property
    def _config_path(self):
        return "%s/config" % self._zk_path

    @property
    def _exposed_path(self):
        """Path of ZK node that if it exists, indicates service is exposed."""
        return "%s/exposed" % self._zk_path

    def _node_missing(self):
        raise ServiceStateNotFound(self._service_name)

    def get_charm_id(self):
        """Return the charm id this service is supposed to use.
        """
        return self._get_node_value("charm")

    @inlineCallbacks
    def set_charm_id(self, charm_id):
        """Set the charm id this service is supposed to use.
        """
        # Verify its a valid charm id
        CharmURL.parse(charm_id).assert_revision()
        yield self._set_node_value("charm", charm_id)

    @inlineCallbacks
    def get_charm_state(self):
        """Return the CharmState for this service."""
        charm_id = yield self.get_charm_id()
        formula_state_manager = CharmStateManager(self._client)
        charm = yield formula_state_manager.get_charm_state(charm_id)
        returnValue(charm)

    @inlineCallbacks
    def is_subordinate(self):
        charm_state = yield self.get_charm_state()
        returnValue(charm_state.is_subordinate())

    @inlineCallbacks
    def set_constraints(self, constraints):
        """Set hardware requirements for any new machines running this service.

        :param constraints: a Constraints instance describing the service-level
            machine constraints.

        Constraints are settable individually by level, and dynamically
        combined with the current environment constraints when it's time to
        add a unit.
        """
        charm_id = yield self.get_charm_id()
        constraints = _series_constraints(constraints, charm_id)
        yield self._set_node_value("constraints", constraints.data)

    @inlineCallbacks
    def get_constraints(self):
        """Get combined environment- and service-level machine constraints.

        :return: a Constraints instance

        This combined Constraints is used both to set the unit constraints at
        add-unit time (so the unit gets a snapshot of the constraints in play
        at creation time) and to display to the user in get-constraints (so
        the user gets to see what constraints will actually be used to deploy
        a unit, and what constraints are currently in play and hence need to
        be set when changing service constraints).
        """
        esm = EnvironmentStateManager(self._client)
        constraints = yield esm.get_constraints()
        constraint_set = yield esm.get_constraint_set()
        service_data = yield self._get_node_value("constraints", {})
        constraints.update(constraint_set.load(service_data))
        returnValue(constraints)

    @inlineCallbacks
    def _validate_principal_container(self, principal_service_unit):
        if not isinstance(principal_service_unit, ServiceUnitState):
            raise PrincipalServiceUnitRequired(
                self.service_name,
                principal_service_unit)

        principal_service = yield principal_service_unit.get_service_state()
        if (yield principal_service.is_subordinate()):
            raise PrincipalServiceUnitRequired(
                self.service_name,
                principal_service_unit.unit_name)

        if not (yield self.is_subordinate()):
            raise NotSubordinateCharm(self.service_name,
                                      principal_service.unit_name)

    @inlineCallbacks
    def add_unit_state(self, container=None):
        """Add a new service unit to this state.

        When provided container should be the service unit state of the
        container for the unit state being added.

        @return: ServiceUnitState for the created unit.

        The new unit's constraints are a frozen copy of the service's
        actualised constraints at the time of unit addition (so, they don't
        change in response to changes in the service or environment
        constraints; if they did it would lead to extreme confusion (ie, after
        a notable change to env/service, users would potentially see units
        apparently deployed on wildly inappropriate machines)).
        """
        constraints = yield self.get_constraints()
        charm_id = yield self.get_charm_id()
        unit_data = {"charm": charm_id, "constraints": constraints.data}
        path = yield self._client.create(
            "/units/unit-", yaml.dump(unit_data), flags=zookeeper.SEQUENCE)

        internal_unit_id = path.rsplit("/", 1)[1]

        sequence = [None]
        if container is not None:
            yield self._validate_principal_container(container)
            container_id = container.internal_id
        else:
            container_id = None

        def add_unit(topology):
            if not topology.has_service(self._internal_id):
                raise StateChanged()
            sequence[0] = topology.add_service_unit(self._internal_id,
                                                    internal_unit_id,
                                                    container_id)

        yield self._retry_topology_change(add_unit)

        returnValue(ServiceUnitState(self._client, self._internal_id,
                                     self._service_name, sequence[0],
                                     internal_unit_id))

    @inlineCallbacks
    def get_unit_names(self):
        topology = yield self._read_topology()
        if not topology.has_service(self._internal_id):
            raise StateChanged()

        unit_ids = topology.get_service_units(self._internal_id)

        unit_names = []
        for unit_id in unit_ids:
            unit_names.append(
                topology.get_service_unit_name(self._internal_id, unit_id))
        returnValue(unit_names)

    @inlineCallbacks
    def remove_unit_state(self, service_unit):
        """Destroy a unit state.
        """
        # Unassign from machine if currently assigned.
        yield service_unit.unassign_from_machine()

        # Remove from topology
        def remove_unit(topology):
            if not topology.has_service(self._internal_id) or \
               not topology.has_service_unit(
                self._internal_id, service_unit.internal_id):
                raise StateChanged()

            topology.remove_service_unit(
                self._internal_id, service_unit.internal_id)

        yield self._retry_topology_change(remove_unit)

        # Remove any local settings.
        yield remove_tree(
            self._client, "/units/%s" % service_unit.internal_id)

    @inlineCallbacks
    def get_all_unit_states(self):
        """Get all the service unit states associated with this service.

        @return: list of ServiceUnitState instances.
        """
        topology = yield self._read_topology()
        if not topology.has_service(self._internal_id):
            raise StateChanged()

        units = []

        for unit_id in topology.get_service_units(self._internal_id):
            unit_name = topology.get_service_unit_name(self._internal_id,
                                                       unit_id)
            service_name, sequence = _parse_unit_name(unit_name)

            internal_unit_id = \
                topology.find_service_unit_with_sequence(self._internal_id,
                                                     sequence)

            unit = ServiceUnitState(self._client, self._internal_id,
                                    self._service_name, sequence,
                                    internal_unit_id)
            units.append(unit)

        returnValue(units)

    @inlineCallbacks
    def get_unit_state(self, unit_name):
        """Return service unit state with the given unit name.

        @return: ServiceUnitState with the given name.

        @raise ServiceUnitStateNotFound if the unit name is not found.
        """
        assert "/" in unit_name, "Bad unit name: %s" % (unit_name,)

        service_name, sequence = _parse_unit_name(unit_name)

        if service_name != self._service_name:
            raise BadServiceStateName(self._service_name, service_name)

        topology = yield self._read_topology()

        if not topology.has_service(self._internal_id):
            raise StateChanged()

        internal_unit_id = \
            topology.find_service_unit_with_sequence(self._internal_id,
                                                     sequence)

        if internal_unit_id is None:
            raise ServiceUnitStateNotFound(unit_name)

        returnValue(ServiceUnitState(self._client, self._internal_id,
                                     self._service_name, sequence,
                                     internal_unit_id))

    def watch_relation_states(self, callback):
        """Observe changes in the assigned relations for the service.

        @param callback: A function/method which accepts two sequences
        of C{ServiceRelationState} instances, representing the old
        relations and new relations. The old relations variable will
        be 'None' the first time the function is called.

        Note there are no guarantees that this function will be called
        once for *every* change in the topology, which means that multiple
        modifications may be observed as a single call.

        This method currently sets a pretty much perpetual watch (errors
        will make it bail out).  In order to cleanly stop the watcher, a
        StopWatch exception can be raised by the callback.
        """

        def watch_topology(old_topology, new_topology):
            if old_topology is None:
                old_relations = None
            else:
                old_relations = old_topology.get_relations_for_service(
                    self._internal_id)

            new_relations = new_topology.get_relations_for_service(
                self._internal_id)

            if old_relations != new_relations:
                if old_relations:
                    old_relations = _to_service_relation_state(
                        self._client, self._internal_id, old_relations)
                new_relations = _to_service_relation_state(
                    self._client, self._internal_id, new_relations)

                return callback(old_relations, new_relations)

        return self._watch_topology(watch_topology)

    @inlineCallbacks
    def watch_config_state(self, callback):
        """Observe changes to config state for a service.

        @param callback: A function/method which accepts the YAMLState
        node of the changed service. No effort is made to present
        deltas to the change function.

        Note there are no guarantees that this function will be called
        once for *every* change in the topology, which means that multiple
        modifications may be observed as a single call.

        This method currently sets a pretty much perpetual watch (errors
        will make it bail out).  In order to cleanly stop the watcher, a
        StopWatch exception can be raised by the callback.
        """
        @inlineCallbacks
        def watcher(change_event):
            if self._client.connected:
                exists_d, watch_d = self._client.exists_and_watch(
                    self._config_path)
            yield callback(change_event)
            watch_d.addCallback(watcher)

        exists_d, watch_d = self._client.exists_and_watch(self._config_path)

        exists = yield exists_d

        # Setup the watch deferred callback after the user defined callback
        # has returned successfully from the existence invocation.
        callback_d = maybeDeferred(callback, bool(exists))
        callback_d.addCallback(
            lambda x: watch_d.addCallback(watcher) and x)

        # Wait on the first callback, reflecting present state, not a zk watch
        yield callback_d

    def watch_service_unit_states(self, callback):
        """Observe changes in service unit membership for this service.

        `callback(old_service_unit_names, new_service_unit_names)`:
           function called upon a change to the service topology. Both
           parameters to the callback are sets, possibly empty.

        Note that there are no guarantees that this function will be
        called once for *every* change in the topology, which means
        that multiple modifications may be observed as a single call.

        This method currently sets a pretty much perpetual watch
        (errors will make it bail out).  In the future, the return
        value of the watch function may be used to define whether to
        continue watching or to stop.
        """

        def watch_topology(old_topology, new_topology):

            def get_service_unit_names(topology):
                if topology is None:
                    return set()
                if not topology.has_service(self._internal_id):
                    # The watch is now running, but by the time we
                    # read the topology node from ZK, the topology has
                    # changed with the service being removed. Since
                    # there are no service units for this service,
                    # simply bail out.
                    return set()
                service_unit_names = set()
                for unit_id in topology.get_service_units(self._internal_id):
                    service_unit_names.add(topology.get_service_unit_name(
                            self._internal_id, unit_id))
                return service_unit_names

            old_service_units = get_service_unit_names(old_topology)
            new_service_units = get_service_unit_names(new_topology)
            if old_service_units != new_service_units:
                return callback(old_service_units, new_service_units)

        return self._watch_topology(watch_topology)

    @inlineCallbacks
    def set_exposed_flag(self):
        """Inform the service that it has been exposed

        Typically set by juju expose
        """
        try:
            yield self._client.create(self._exposed_path)
        except zookeeper.NodeExistsException:
            # We get to the same end state
            pass

    @inlineCallbacks
    def get_exposed_flag(self):
        """Returns a boolean denoting if the exposed flag is set.
        """
        stat = yield self._client.exists(self._exposed_path)
        returnValue(bool(stat))

    @inlineCallbacks
    def clear_exposed_flag(self):
        """Clear the exposed flag.

        Typically cleared by juju unexpose
        """
        try:
            yield self._client.delete(self._exposed_path)
        except zookeeper.NoNodeException:
            # We get to the same end state.
            pass

    def watch_exposed_flag(self, callback):
        """Set `callback` called on changes to this service's exposed flag.

        `callback` - The callback receives a single parameter, the
             current boolean value of the exposed flag (True if
             present in ZK, False otherwise). Only changes will be
             observed, and they respect ZK watch semantics in terms of
             ordering and reliability. Consequently, client of this
             watch do not need to retrieve the exposed flag setting in
             this callback (no surprises).

             It is the responsibility of `callback` to ensure that it
             shuts down the watch with `StopWatcher` before
             application teardown. For example, this can be done by
             having the callback depend on the application state in
             some way.

        The watch is permanent until `callback` raises a `StopWatcher`
        exception.
        """

        @inlineCallbacks
        def manage_callback(*ignored):
            # Need to guard on the client being connected in the case
            # 1) a watch is waiting to run (in the reactor);
            # 2) and the connection is closed.
            #
            # It remains the reponsibility of `callback` to raise
            # `StopWatcher`, per above.
            if not self._client.connected:
                returnValue(None)
            exists_d, watch_d = self._client.exists_and_watch(
                self._exposed_path)
            stat = yield exists_d
            exists = bool(stat)
            try:
                yield callback(exists)
            except StopWatcher:
                returnValue(None)
            watch_d.addCallback(manage_callback)

        return manage_callback()

    @inlineCallbacks
    def get_config(self):
        """Returns the services current options as a YAMLState object.

        When returned this object will have already had its `read`
        method invoked and is ready for use. The state object can then have
        its `write` method invoked to publish the state to Zookeeper.
        """
        charm_state = yield self.get_charm_state()
        config = yield charm_state.get_config()
        defaults = config.get_defaults()

        config_node = ConfigState(
            self._client, "/services/%s/config" % self._internal_id, defaults)

        yield config_node.read()
        returnValue(config_node)


class ConfigState(YAMLState):

    def __init__(self, client, path, defaults):
        super(ConfigState, self).__init__(client, path)
        self._defaults = defaults

    def __getitem__(self, key):
        self._check()
        if key in self._cache:
            return self._cache[key]
        return self._defaults[key]

    def keys(self):
        return list(set(self._cache).union(set(self._defaults)))

    def __delitem__(self, key):
        raise NotImplemented("Not defined")


def _to_service_relation_state(client, service_id, assigned_relations):
    """Helper method to construct a list of service relation states.

    @param client: Zookeeper client
    @param service_id: Id of the service
    @param assigned_relations: sequence of relation_id, relation_type and the
           service relation specific information (role and name).
    """
    service_relations = []
    for relation in assigned_relations:
        relation_info = relation["service"]
        service_relations.append(
            ServiceRelationState(client,
                                 service_id,
                                 relation["relation_id"],
                                 relation["scope"],
                                 **relation_info))
    return service_relations


class ServiceUnitState(StateBase, AgentStateMixin, YAMLStateNodeMixin):
    """State of a service unit registered in an environment.

    Each service is composed by units, and each unit represents an
    actual deployment of software to satisfy the needs defined in
    this service state.
    """

    def __init__(self, client, internal_service_id, service_name,
                 unit_sequence, internal_id):
        self._client = client
        self._internal_service_id = internal_service_id
        self._service_name = service_name
        self._unit_sequence = unit_sequence
        self._internal_id = internal_id

    def __hash__(self):
        return hash(self.unit_name)

    def __eq__(self, other):
        if not isinstance(other, ServiceUnitState):
            return False
        return self.unit_name == other.unit_name

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__,
                            self.unit_name)

    @property
    def service_name(self):
        """Service name for the service from this unit."""
        return self._service_name

    @property
    def internal_id(self):
        """Unit's internal id, of the form unit-NNNNNNNNNN."""
        return self._internal_id

    @property
    def unit_name(self):
        """Get a nice user-oriented identifier for this unit."""
        return "%s/%d" % (self._service_name, self._unit_sequence)

    @property
    def _zk_path(self):
        return "/units/%s" % self._internal_id

    @property
    def _ports_path(self):
        """The path for the open ports for this service unit."""
        return "%s/ports" % self._zk_path

    def _get_agent_path(self):
        """Get the zookeeper path for the service unit agent."""
        return "%s/agent" % self._zk_path

    def _check_valid_in(self, topology):
        ok = topology.has_service(self._internal_service_id)
        ok = ok and topology.has_service_unit(
            self._internal_service_id, self._internal_id)
        if not ok:
            raise StateChanged()

    def _node_missing(self):
        raise ServiceUnitStateNotFound(self._service_name)

        return "/units/%s/agent" % self._internal_id

    @inlineCallbacks
    def is_subordinate(self):
        service_state = yield self.get_service_state()
        returnValue((yield service_state.is_subordinate()))

    @inlineCallbacks
    def get_container(self):
        """Return the ServiceUnitState of the container this unit executes in.

        By default units are their own container, however subordinate services
        will return the unit state of their principal service.

        """
        topology = yield self._read_topology()
        try:
            container_info = topology.get_service_unit_container(
                self.internal_id)
        except InternalTopologyError:
            returnValue(None)

        if container_info is None:
            returnValue(None)

        service_id, service_name, sequence, container_id = container_info
        returnValue(ServiceUnitState(self._client,
                                     service_id,
                                     service_name,
                                     sequence,
                                     container_id))

    def get_service_state(self):
        """Return the service state for this unit."""
        return ServiceState(self._client,
                            self._internal_service_id,
                            self._service_name)

    def get_public_address(self):
        """Get the public address of the unit.

        If the unit is unassigned, or its unit agent hasn't started this
        value maybe None.
        """
        return self._get_node_value("public-address")

    def set_public_address(self, public_address):
        """A unit's public address can be utilized to access the service.

        The service must have been exposed for the service to be reachable
        outside of the environment.
        """
        return self._set_node_value("public-address", public_address)

    def get_private_address(self):
        """Get the private address of the unit.

        If the unit is unassigned, or its unit agent hasn't started this
        value maybe None.
        """
        return self._get_node_value("private-address")

    def set_private_address(self, private_address):
        """A unit's address private to the environment.

        Other service will see and utilize this address for relations.
        """
        return self._set_node_value("private-address", private_address)

    def get_charm_id(self):
        """The id of the charm currently deployed on the unit"""
        return self._get_node_value("charm")

    @inlineCallbacks
    def set_charm_id(self, charm_id):
        """Set the charm identifier that the unit is currently running."""
        CharmURL.parse(charm_id).assert_revision()
        yield self._set_node_value("charm", charm_id)

    @inlineCallbacks
    def get_constraints(self):
        """The complete constraints for this unit (as set at creation time).

        Unit constraints are not settable; they're a snapshot of environment/
        service state at unit addition time, so that get-constraints returns
        sane results when asked about a unit when considered relative to the
        constraints of the machine it's deployed on (rather than showing
        combined service/env constraints for the parent, which could be wildly
        different, and would certainly lead to confusion and bug reports).
        """
        esm = EnvironmentStateManager(self._client)
        constraint_set = yield esm.get_constraint_set()
        data = yield self._get_node_value("constraints", {})
        returnValue(constraint_set.load(data))

    @inlineCallbacks
    def get_assigned_machine_id(self):
        """Get the assigned machine id or None if the unit is not assigned.
        """
        topology = yield self._read_topology()
        self._check_valid_in(topology)
        machine_id = topology.get_service_unit_machine(
            self._internal_service_id, self._internal_id)
        if machine_id is None:
            container = yield self.get_container()
            if container:
                machine_id = yield container.get_assigned_machine_id()
                returnValue(machine_id)

        if machine_id is not None:
            machine_id = _public_machine_id(machine_id)
        returnValue(machine_id)

    @inlineCallbacks
    def assign_to_machine(self, machine_state):
        """Assign this service unit to the given machine.
        """
        constraints = yield self.get_constraints()
        machine_constraints = yield machine_state.get_constraints()
        if not machine_constraints.can_satisfy(constraints):
            log.warning(
                "Unit %s assigned to machine %s with incompatible constraints",
                self.unit_name, machine_state.id)

        def assign_unit(topology):
            self._check_valid_in(topology)

            machine_id = topology.get_service_unit_machine(
                self._internal_service_id, self._internal_id)

            container = topology.get_service_unit_container(self.internal_id)
            if container is not None:
                raise IllegalSubordinateMachineAssignment(self.unit_name)

            if machine_id is None:
                topology.assign_service_unit_to_machine(
                    self._internal_service_id,
                    self._internal_id,
                    machine_state.internal_id)
            elif machine_id == machine_state.internal_id:
                # It's a NOOP. To avoid dealing with concurrency issues
                # here, we can let it go through.
                pass
            else:
                raise ServiceUnitStateMachineAlreadyAssigned(self.unit_name)
        yield self._retry_topology_change(assign_unit)

    @inlineCallbacks
    def assign_to_unused_machine(self):
        """Assign this service unit to an unused machine (if available).

        It will not attempt to reuse machine 0, since this is
        currently special.

        Raises `NoUnusedMachines` if there are no available machines
        for reuse. Usually this then should result in using code to
        subsequently attempt to create a new machine in the
        environment, then assign directly to it with
        `assign_to_machine`.
        """
        # used to provide a writable result for the callback
        scope_escaper = [None]
        unit_constraints = yield self.get_constraints()

        @inlineCallbacks
        def assign_unused_unit(topology):
            self._check_valid_in(topology)

            # XXX We cannot reuse the "root" machine (used by the
            # provisioning agent), but the topology metadata does not
            # properly reflect its allocation.  In the future, once it
            # is managed like any other service, this special case can
            # be removed.
            root_machine_id = "machine-%010d" % 0
            for internal_id in topology.get_machines():
                if internal_id == root_machine_id:
                    continue
                if topology.machine_has_units(internal_id):
                    continue
                machine_state = MachineState(self._client, internal_id)
                machine_constraints = yield machine_state.get_constraints()
                if machine_constraints.can_satisfy(unit_constraints):
                    break
            else:
                raise NoUnusedMachines()

            topology.assign_service_unit_to_machine(
                self._internal_service_id,
                self._internal_id,
                machine_state.internal_id)
            scope_escaper[0] = machine_state

        yield self._retry_topology_change(assign_unused_unit)
        returnValue(scope_escaper[0])

    def unassign_from_machine(self):
        """Unassign this service unit from whatever machine it's assigned to.
        """

        def unassign_unit(topology):
            self._check_valid_in(topology)

            # If for whatever reason it's not already assigned to a
            # machine, ignore it and move forward so that we don't
            # have to deal with conflicts.
            machine_id = topology.get_service_unit_machine(
                self._internal_service_id, self._internal_id)
            if machine_id is not None:
                topology.unassign_service_unit_from_machine(
                    self._internal_service_id, self._internal_id)
        return self._retry_topology_change(unassign_unit)

    @property
    def _hook_debug_path(self):
        return "%s/debug" % self._zk_path

    @inlineCallbacks
    def enable_hook_debug(self, hook_names):
        """Enable hook debugging.

        :param hook_name: The name of the hook to debug. The special
               value ``*`` will enable debugging on all hooks.

        Returns True if the debug was successfully enabled.

        The enabling hook debugging functionality triggers the
        creation of an ephemeral node used to notify unit agents of
        the debug behavior they should enable. Upon close of the
        zookeeper client used to enable this debug, this value will be
        cleared.
        """
        if not isinstance(hook_names, (list, tuple)):
            raise AssertionError("Hook names must be a list: got %r"
                                 % hook_names)

        if "*" in hook_names and len(hook_names) > 1:
            msg = "Ambigious to debug all hooks and named hooks %r" % (
                hook_names,)
            raise ValueError(msg)

        try:
            yield self._client.create(
                self._hook_debug_path,
                yaml.safe_dump({"debug_hooks": hook_names}),
                flags=zookeeper.EPHEMERAL)
        except zookeeper.NodeExistsException:
            raise ServiceUnitDebugAlreadyEnabled(self.unit_name)
        returnValue(True)

    @inlineCallbacks
    def clear_hook_debug(self):
        """Clear any debug hook settings.

        When a single hook is being debugged this method is used by agents
        to clear the debug settings after they have been processed.
        """
        try:
            yield self._client.delete(self._hook_debug_path)
        except zookeeper.NoNodeException:
            # We get to the same end state.
            pass
        returnValue(True)

    @inlineCallbacks
    def get_hook_debug(self):
        """Retrieve the current value if any of the hook debug setting.

        If no setting is found, None is returned.
        """
        try:
            content, stat = yield self._client.get(self._hook_debug_path)
        except zookeeper.NoNodeException:
            # We get to the same end state.
            returnValue(None)
        returnValue(yaml.load(content))

    @inlineCallbacks
    def watch_hook_debug(self, callback, permanent=True):
        """Set a callback to be invoked when the debug state changes.

        :param callback: The callback recieves a single parameter, the
               change event. The watcher always recieve an initial
               boolean value invocation denoting the existence of the
               debug setting. Subsequent invocations will be with change
               events.

        :param permanent: Determines if the watch automatically resets.

        Its important that clients do not rely on the event as reflective
        of the current state. It is only a reflection of some change
        happening, to inform watch users should fetch the current value.
        """

        @inlineCallbacks
        def watcher(change_event):
            if permanent and self._client.connected:
                exists_d, watch_d = self._client.exists_and_watch(
                    self._hook_debug_path)

            yield callback(change_event)

            if permanent:
                watch_d.addCallback(watcher)

        exists_d, watch_d = self._client.exists_and_watch(
            self._hook_debug_path)
        exists = yield exists_d
        # Setup the watch deferred callback after the user defined callback
        # has returned successfully from the existence invocation.
        callback_d = maybeDeferred(callback, bool(exists))
        callback_d.addCallback(
            lambda x: watch_d.addCallback(watcher) and x)
        # Wait on the first callback, reflecting present state, not a zk watch
        yield callback_d

    @property
    def _upgrade_flag_path(self):
        return "%s/upgrade" % self._zk_path

    @inlineCallbacks
    def set_upgrade_flag(self, force=False):
        """Inform the unit it should perform an upgrade.
        """
        assert isinstance(force, bool), "Invalid force upgrade flag"

        def update(content, stat):
            if not content:
                flags = dict(force=force)
                return yaml.dump(flags)

            flags = yaml.load(content)
            if not isinstance(flags, dict):
                flags = dict(force=force)
                return yaml.dump(flags)

            if flags['force'] != force:
                raise ServiceUnitUpgradeAlreadyEnabled(self.unit_name)

            return content

        yield retry_change(self._client,
                           self._upgrade_flag_path,
                           update)

    @inlineCallbacks
    def get_upgrade_flag(self):
        """Returns a dictionary containing the upgrade flag or False.
        """
        try:
            content, stat = yield self._client.get(self._upgrade_flag_path)
            if not content:
                returnValue(False)
        except zookeeper.NoNodeException:
            returnValue(False)
        returnValue(yaml.load(content))

    @inlineCallbacks
    def clear_upgrade_flag(self):
        """Clear the upgrade flag.

        Typically done by the unit agent before beginning the
        upgrade.
        """
        try:
            yield self._client.delete(self._upgrade_flag_path)
        except zookeeper.NoNodeException:
            # We get to the same end state.
            pass

    @inlineCallbacks
    def watch_upgrade_flag(self, callback, permanent=True):
        """Set a callback to be invoked when an upgrade is requested.

        :param callback: The callback recieves a single parameter, the
               change event. The watcher always recieve an initial
               boolean value invocation denoting the existence of the
               upgrade setting. Subsequent invocations will be with change
               events.

        :param permanent: Determines if the watch automatically resets.

        Its important that clients do not rely on the event as reflective
        of the current state. It is only a reflection of some change
        happening, the callback should fetch the current value via
        the API, if needed.
        """
        @inlineCallbacks
        def watcher(change_event):

            if permanent and self._client.connected:
                exists_d, watch_d = self._client.exists_and_watch(
                    self._upgrade_flag_path)

            yield callback(change_event)

            if permanent:
                watch_d.addCallback(watcher)

        exists_d, watch_d = self._client.exists_and_watch(
            self._upgrade_flag_path)

        exists = yield exists_d

        # Setup the watch deferred callback after the user defined callback
        # has returned successfully from the existence invocation.
        callback_d = maybeDeferred(callback, bool(exists))
        callback_d.addCallback(
            lambda x: watch_d.addCallback(watcher) and x)
        # Wait on the first callback, reflecting present state, not a zk watch
        yield callback_d

    @property
    def _unit_resolved_path(self):
        return "%s/resolved" % self._zk_path

    @inlineCallbacks
    def set_resolved(self, retry):
        """Mark the unit as in need of being resolved.

        :param retry: A boolean denoting if hooks should fire as a result
        of the retry.

        The resolved setting is set by the command line to inform
        a unit to attempt an retry transition from an error state.
        """

        if not retry in (RETRY_HOOKS, NO_HOOKS):
            raise ValueError("invalid retry value %r" % retry)

        try:
            yield self._client.create(
                self._unit_resolved_path, yaml.safe_dump({"retry": retry}))
        except zookeeper.NodeExistsException:
            raise ServiceUnitResolvedAlreadyEnabled(self.unit_name)

    @inlineCallbacks
    def get_resolved(self):
        """Get the value of the resolved setting if any.

        The resolved setting is retrieved by the unit agent and if
        found instructs it to attempt an retry transition from an
        error state.
        """
        try:
            content, stat = yield self._client.get(self._unit_resolved_path)
        except zookeeper.NoNodeException:
            # Return a default value.
            returnValue(None)
        returnValue(yaml.load(content))

    @inlineCallbacks
    def clear_resolved(self):
        """Remove any resolved setting on the unit."""
        try:
            yield self._client.delete(self._unit_resolved_path)
        except zookeeper.NoNodeException:
            # We get to the same end state.
            pass

    @inlineCallbacks
    def watch_resolved(self, callback):
        """Set a callback to be invoked when an unit is marked resolved.

        :param callback: The callback recieves a single parameter, the
               change event. The watcher always recieve an initial
               boolean value invocation denoting the existence of the
               resolved setting. Subsequent invocations will be with change
               events.
        """
        @inlineCallbacks
        def watcher(change_event):
            if not self._client.connected:
                returnValue(None)

            exists_d, watch_d = self._client.exists_and_watch(
                self._unit_resolved_path)
            try:
                yield callback(change_event)
            except StopWatcher:
                returnValue(None)
            watch_d.addCallback(watcher)

        exists_d, watch_d = self._client.exists_and_watch(
            self._unit_resolved_path)
        exists = yield exists_d

        # Setup the watch deferred callback after the user defined callback
        # has returned successfully from the existence invocation.
        callback_d = maybeDeferred(callback, bool(exists))
        callback_d.addCallback(
            lambda x: watch_d.addCallback(watcher) and x)
        callback_d.addErrback(
            lambda failure: failure.trap(StopWatcher))

        # Wait on the first callback, reflecting present state, not a zk watch
        yield callback_d

    @property
    def _relation_resolved_path(self):
        return "%s/relation-resolved" % self._zk_path

    @inlineCallbacks
    def set_relation_resolved(self, relation_map):
        """Mark a unit's relations as being resolved.

        The unit agent will watch this setting and unblock the unit,
        via manipulation of the unit workflow and lifecycle.

        :param relation_map: A map of internal relation ids, to retry hook
              values either juju.state.service.NO_HOOKS or
              RETRY_HOOKS.

        TODO:
        The api currently takes internal relation ids, this should be
        cleaned up with a refactor to state request protocol objects.
        Only public names should be exposed beyond the state api.

        There's an ongoing discussion on whether this needs to support
        retries. Currently it doesn't, without it the the arg to this
        method could just be a list of relations. Supporting retries
        would mean capturing enough information to retry the hook, and
        has reconciliation issues wrt to what's current at the time of
        re-execution. The existing hook scheduler automatically
        performs merges of redundant events. The retry could execute a
        relation change hook, for a remote unit that has already
        departed at the time of re-execution (and for which we have
        a pending hook execution), which would be inconsistent, wrt
        to what would be exposed via the hook cli api. With support
        for on disk persistence and recovery, some of this temporal
        synchronization would already be in place.
        """
        if not isinstance(relation_map, dict):
            raise ValueError(
                "Relation map must be a dictionary %r" % relation_map)

        if [v for v in relation_map.values() if v not in (
            RETRY_HOOKS, NO_HOOKS)]:
            raise ValueError("Invalid setting for retry hook")

        def update_relation_resolved(content, stat):
            if not content:
                return yaml.safe_dump(relation_map)

            content = yaml.safe_dump(
                dict_merge(yaml.load(content), relation_map))
            return content

        try:
            yield retry_change(
                self._client,
                self._relation_resolved_path,
                update_relation_resolved)
        except StateChanged:
            raise ServiceUnitRelationResolvedAlreadyEnabled(self.unit_name)
        returnValue(True)

    @inlineCallbacks
    def get_relation_resolved(self):
        """Retrieve any resolved flags set for this unit's relations.
        """
        try:
            content, stat = yield self._client.get(
                self._relation_resolved_path)
        except zookeeper.NoNodeException:
            returnValue(None)
        returnValue(yaml.load(content))

    @inlineCallbacks
    def clear_relation_resolved(self):
        """ Clear the relation resolved setting.
        """
        try:
            yield self._client.delete(self._relation_resolved_path)
        except zookeeper.NoNodeException:
            # We get to the same end state.
            pass

    @inlineCallbacks
    def watch_relation_resolved(self, callback):
        """Set a callback to be invoked when a unit's relations are resolved.

        :param callback: The callback recieves a single parameter, the
               change event. The watcher always recieve an initial
               boolean value invocation denoting the existence of the
               resolved setting. Subsequent invocations will be with change
               events.
        """
        @inlineCallbacks
        def watcher(change_event):
            if not self._client.connected:
                returnValue(None)
            exists_d, watch_d = self._client.exists_and_watch(
                self._relation_resolved_path)
            try:
                yield callback(change_event)
            except StopWatcher:
                returnValue(None)

            watch_d.addCallback(watcher)

        exists_d, watch_d = self._client.exists_and_watch(
            self._relation_resolved_path)

        exists = yield exists_d

        # Setup the watch deferred callback after the user defined callback
        # has returned successfully from the existence invocation.
        callback_d = maybeDeferred(callback, bool(exists))
        callback_d.addCallback(
            lambda x: watch_d.addCallback(watcher) and x)
        callback_d.addErrback(
            lambda failure: failure.trap(StopWatcher))

        # Wait on the first callback, reflecting present state, not a zk watch
        yield callback_d

    @inlineCallbacks
    def open_port(self, port, proto):
        """Sets policy that `port` (using `proto`) should be opened.

        This only takes effect when the service itself is exposed.
        """
        def zk_open_port(content, stat):
            if content is None:
                data = {}
            else:
                data = yaml.load(content)
                if data is None:
                    data = {}
            open_ports = data.setdefault("open", [])
            port_proto = dict(port=port, proto=proto)
            if port_proto not in open_ports:
                open_ports.append(port_proto)
            return yaml.safe_dump(data)

        yield retry_change(self._client, self._ports_path, zk_open_port)

    @inlineCallbacks
    def close_port(self, port, proto):
        """Sets policy that `port` (using `proto`) should be closed.

        This only takes effect when the service itself is exposed;
        otherwise all ports are closed regardless.
        """
        def zk_close_port(content, stat):
            if content is None:
                data = {}
            else:
                data = yaml.load(content)
                if data is None:
                    data = {}
            open_ports = data.setdefault("open", [])
            port_proto = dict(port=port, proto=proto)
            if port_proto in open_ports:
                open_ports.remove(port_proto)
            return yaml.dump(data)

        yield retry_change(
            self._client, self._ports_path, zk_close_port)

    @inlineCallbacks
    def get_open_ports(self):
        """Gets the open ports for this service unit, or an empty list.

        The retrieved format is [{"port": PORT, "proto": PROTO}, ...]

        Any open ports are only opened if the service itself is
        exposed.
        """
        try:
            content, stat = yield self._client.get(self._ports_path)
        except zookeeper.NoNodeException:
            returnValue([])
        data = yaml.load(content)
        if data is None:
            returnValue(())
        returnValue(data.get("open", ()))

    @inlineCallbacks
    def watch_ports(self, callback):
        """Set `callback` to be invoked when a unit's ports are changed.

        `callback` - receives a single parameter, the change
            event. The watcher always receives an initial boolean value
            invocation denoting the existence of the open ports
            node. Subsequent invocations will be with change
            events.
        """
        @inlineCallbacks
        def watcher(change_event):
            if not self._client.connected:
                returnValue(None)
            exists_d, watch_d = self._client.exists_and_watch(
                self._ports_path)
            try:
                yield callback(change_event)
            except StopWatcher:
                returnValue(None)

            watch_d.addCallback(watcher)
        exists_d, watch_d = self._client.exists_and_watch(
            self._ports_path)
        exists = yield exists_d

        # Setup the watch deferred callback after the user defined callback
        # has returned successfully from the existence invocation.
        callback_d = maybeDeferred(callback, bool(exists))
        callback_d.addCallback(
            lambda x: watch_d.addCallback(watcher) and x)
        callback_d.addErrback(
            lambda failure: failure.trap(StopWatcher))

        # Wait on the first callback, reflecting present state, not a zk watch
        yield callback_d


def _parse_unit_name(unit_name):
    """Parse a unit's name into the service name and its sequence.

    Expecting a unit_name in the common format 'wordpress/0' this
    method will return ('wordpress', 0).

    @return: a tuple containing the service name(str) and the sequence
    number(int).
    """
    service_name, sequence = unit_name.rsplit("/", 1)
    sequence = int(sequence)
    return service_name, sequence


def parse_service_name(unit_name):
    """Return the service name from a given unit name."""
    try:
        return _parse_unit_name(unit_name)[0]
    except (AttributeError, ValueError):
        raise ValueError("Not a proper unit name: %r" % (unit_name,))
