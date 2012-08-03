from collections import namedtuple

from twisted.internet.defer import inlineCallbacks, returnValue, succeed, fail

from juju.state.base import StateBase
from juju.state.errors import (
    UnitRelationStateNotFound, StateNotFound, RelationBrokenContextError,
    RelationStateNotFound, InvalidRelationIdentity, StateChanged)
from juju.state.relation import (
    RelationStateManager, ServiceRelationState, UnitRelationState)
from juju.state.service import ServiceStateManager, parse_service_name
from juju.state.utils import YAMLState


class RelationChange(
    namedtuple(
        "RelationChange",
        "relation_ident change_type unit_name")):

    __slots__ = ()

    @property
    def relation_name(self):
        return self.relation_ident.split(":")[0]


class HookContext(StateBase):
    """Context for hooks which don't depend on relation state. """

    def __init__(self, client, unit_name, topology=None):
        super(HookContext, self).__init__(client)

        self._unit_name = unit_name
        self._service = None

        # A cache of retrieved nodes.
        self._node_cache = {}

        # A cache of node names to node ids.
        self._name_cache = {}

        # Service options
        self._config_options = None

        # Cached topology
        self._topology = topology

    @inlineCallbacks
    def _resolve_id(self, unit_id):
        """Resolve a unit id to a unit name."""
        if self._topology is None:
            self._topology = yield self._read_topology()
        unit_name = self._topology.get_service_unit_name_from_id(unit_id)
        returnValue(unit_name)

    @inlineCallbacks
    def _resolve_name(self, unit_name):
        """Resolve a unit name to a unit id with caching."""
        if unit_name in self._name_cache:
            returnValue(self._name_cache[unit_name])
        if self._topology is None:
            self._topology = yield self._read_topology()
        unit_id = self._topology.get_service_unit_id_from_name(unit_name)
        self._name_cache[unit_name] = unit_id
        returnValue(unit_id)

    @inlineCallbacks
    def get_local_unit_state(self):
        """Return ServiceUnitState for the local service unit."""
        service_state_manager = ServiceStateManager(self._client)
        unit_state = yield service_state_manager.get_unit_state(
            self._unit_name)
        returnValue(unit_state)

    @inlineCallbacks
    def get_local_service(self):
        """Return ServiceState for the local service."""
        if self._service is None:
            service_state_manager = ServiceStateManager(self._client)
            self._service = yield(
                service_state_manager.get_service_state(
                    parse_service_name(self._unit_name)))
        returnValue(self._service)

    @inlineCallbacks
    def get_config(self):
        """Gather the configuration options.

        Returns YAMLState for the service options of the current hook
        and caches them internally.This state object's `write` method
        must be called to publish changes to Zookeeper. `flush` will
        do this automatically.
        """
        if not self._config_options:
            service = yield self.get_local_service()
            self._config_options = yield service.get_config()
        returnValue(self._config_options)

    @inlineCallbacks
    def get_relations(self):
        """Get the relations associated to the local service."""
        relations = []
        if self._topology is None:
            self._topology = yield self._read_topology()
        service = yield self.get_local_service()
        internal_service_id = service.internal_id
        for info in self._topology.get_relations_for_service(
                internal_service_id):
            service_info = info["service"]
            relations.append(
                ServiceRelationState(
                    self._client,
                    internal_service_id,
                    info["relation_id"],
                    info["scope"],
                    **service_info))
        returnValue(relations)

    @inlineCallbacks
    def get_relation_idents(self, relation_name):
        """Return the relation idents for `relation_name`"""
        relations = yield self.get_relations()
        returnValue(sorted([r.relation_ident for r in relations if
                            not relation_name or
                            r.relation_name == relation_name]))

    @inlineCallbacks
    def get_relation_id_and_scope(self, relation_ident):
        """Return the (internal) relation id for `relation_ident`."""
        parts = relation_ident.split(":")
        if len(parts) != 2 or not parts[1].isdigit():
            raise InvalidRelationIdentity(relation_ident)
        relation_name, normalized_id = parts
        relation_id = "%s-%s" % ("relation", normalized_id.zfill(10))
        # Double check the internal relation id by looking it up
        relations = yield self.get_relations()
        for r in relations:
            if (r.relation_name == relation_name and \
                r.internal_relation_id == relation_id):
                returnValue((relation_id, r.relation_scope))
        else:
            raise RelationStateNotFound()

    @inlineCallbacks
    def get_relation_hook_context(self, relation_ident):
        """Return a child hook context for `relation_ident`"""
        service = yield self.get_local_service()
        unit = yield self.get_local_unit_state()
        relation_id, relation_scope = yield self.get_relation_id_and_scope(
            relation_ident)
        unit_relation = UnitRelationState(
            self._client, service.internal_id, unit.internal_id,
            relation_id, relation_scope)
        # Ensure that the topology is shared so there's a consistent view
        # between the parent and any children.
        returnValue(RelationHookContext(
                self._client, unit_relation,
                relation_ident,
                unit_name=self._unit_name,
                topology=self._topology))

    @inlineCallbacks
    def flush(self):
        """Flush pending state."""
        config = yield self.get_config()
        yield config.write()


class RelationHookContext(HookContext):
    """A hook execution data cache and write buffer of relation settings.

    Performs caching of any relation settings examined by the
    hook. Also buffers all writes till the flush method is invoked.
    """

    def __init__(self, client, unit_relation, relation_ident,
                 members=None, unit_name=None, topology=None):
        """
        @param unit_relation: The unit relation state associated to the hook.
        @param change: A C{RelationChange} instance.
        """
        # Zookeeper client.
        super(RelationHookContext, self).__init__(
            client, unit_name=unit_name, topology=topology)
        self._unit_relation = unit_relation

        # A cache of related units in the relation.
        self._members = members

        # The relation ident of this context
        self._relation_ident = relation_ident

        # Whether settings have been modified (set/delete) for this context
        self._needs_flushing = False

        # A cache of the relation scope path
        self._settings_scope_path = None

    def get_settings_path(self, unit_id):
        if self._unit_relation.relation_scope == "global":
            return "/relations/%s/settings/%s" % (
                self._unit_relation.internal_relation_id, unit_id)

        if self._settings_scope_path:
            return "%s/%s" % (self._settings_scope_path, unit_id)

        def process(unit_settings_path):
            self._settings_scope_path = "/".join(
                unit_settings_path.split("/")[:-1])
            return "%s/%s" % (self._settings_scope_path, unit_id)

        d = self._unit_relation.get_settings_path()
        return d.addCallback(process)

    @property
    def relation_ident(self):
        """Returns the relation ident corresponding to this context."""
        return self._relation_ident

    @property
    def relation_name(self):
        """Returns the relation name corresponding to this context."""
        return self._relation_ident.split(":")[0]

    @inlineCallbacks
    def get_members(self):
        """Gets the related unit members of the relation with caching."""
        if self._members is not None:
            returnValue(self._members)

        try:
            container = yield self._unit_relation.get_related_unit_container()
        except StateChanged:
            # The unit relation has vanished, so there are no members.
            returnValue([])
        unit_ids = yield self._client.get_children(container)
        if self._unit_relation.internal_unit_id in unit_ids:
            unit_ids.remove(self._unit_relation.internal_unit_id)

        members = []
        for unit_id in unit_ids:
            unit_name = yield self._resolve_id(unit_id)
            members.append(unit_name)

        self._members = members
        returnValue(members)

    @inlineCallbacks
    def _setup_relation_state(self, unit_name=None):
        """For a given unit name make sure we have YAMLState."""
        if unit_name is None:
            unit_name = yield self._resolve_id(
                self._unit_relation.internal_unit_id)

        if unit_name in self._node_cache:
            returnValue(self._node_cache[unit_name])

        unit_id = yield self._resolve_name(unit_name)
        path = yield self.get_settings_path(unit_id)

        # verify the unit relation path exists
        relation_data = YAMLState(self._client, path)
        try:
            yield relation_data.read(required=True)
        except StateNotFound:
            raise UnitRelationStateNotFound(
                self._unit_relation.internal_relation_id,
                self.relation_name,
                unit_name)

        # cache the value
        self._node_cache[unit_name] = relation_data
        returnValue(relation_data)

    @inlineCallbacks
    def get(self, unit_name):
        """Get the relation settings for a unit.

        Returns the settings as a dictionary.
        """
        relation_data = yield self._setup_relation_state(unit_name)
        returnValue(dict(relation_data))

    @inlineCallbacks
    def get_value(self, unit_name, key):
        """Get a relation setting value for a unit."""
        settings = yield self.get(unit_name)
        if not settings:
            returnValue("")
        returnValue(settings.get(key, ""))

    @inlineCallbacks
    def set(self, data):
        """Set the relation settings for a unit.

        @param data: A dictionary containing the settings.

        **Warning**, this method will replace existing values for the
        unit relation with those from the ``data`` dictionary.
        """
        if not isinstance(data, dict):
            raise TypeError("A dictionary is required.")

        self._needs_flushing = True
        state = yield self._setup_relation_state()
        state.update(data)

    @inlineCallbacks
    def set_value(self, key, value):
        """Set a relation value for a unit."""
        self._needs_flushing = True
        state = yield self._setup_relation_state()
        state[key] = value

    @inlineCallbacks
    def delete_value(self, key):
        """Delete a relation value for a unit."""
        self._needs_flushing = True
        state = yield self._setup_relation_state()
        try:
            del state[key]
        except KeyError:
            # deleting a non-existent key is a no-op
            pass

    def has_read(self, unit_name):
        """Has the context been used to access the settings of the unit.
        """
        return unit_name in self._node_cache

    @inlineCallbacks
    def flush(self):
        """Flush all writes to the unit settings.

        A flush will attempt to intelligently merge values modified on
        the context to the current state of the underlying settings
        node.  It supports externally modified or deleted values that
        are unchanged on the context, to be preserved.

        The change items to the relation YAMLState is returned (this
        could also be done with config settings, but given their
        usage model, doesn't seem to be worth logging).
        """
        relation_setting_changes = []
        if self._needs_flushing:
            rel_state = yield self._setup_relation_state()
            relation_setting_changes = yield rel_state.write()
        yield super(RelationHookContext, self).flush()
        returnValue(relation_setting_changes)


class DepartedRelationHookContext(HookContext):
    """A hook execution context suitable for running a relation-broken hook.

    This context exposes the same interface as RelationHookContext, but:

    * relation settings cannot be changed
    * no remote units are reported to exist
    * remote unit settings are not accessible
    """

    def __init__(self, client, unit_name, unit_id, relation_name, relation_id):
        super(DepartedRelationHookContext, self).__init__(client, unit_name)
        self._relation_name = relation_name
        self._relation_id = relation_id
        self._settings_path = None
        # Cache of relation settings for the local unit
        self._relation_cache = None

    def get_members(self):
        return succeed([])

    @property
    def relation_ident(self):
        """Returns the external relation id corresponding to this context."""
        return ServiceRelationState.get_relation_ident(
            self._relation_name, self._relation_id)

    @inlineCallbacks
    def get_settings_path(self):
        if self._settings_path:
            returnValue(self._settings_path)

        unit_id = yield self._resolve_name(self._unit_name)
        topology = yield self._read_topology()
        container = topology.get_service_unit_container(unit_id)
        container_info = ""
        if container:
            container_info = "%s/" % container[-1]

        self._settings_path = "/relations/%s/%ssettings/%s" % (
            self._relation_id, container_info, unit_id)
        returnValue(self._settings_path)

    @inlineCallbacks
    def get(self, unit_name):
        # Only this unit's settings should be accessible.
        if unit_name not in (None, self._unit_name):
            raise RelationBrokenContextError(
                "Cannot access other units in broken relation")

        settings_path = yield self.get_settings_path()

        if self._relation_cache is None:
            relation_data = YAMLState(self._client, settings_path)
            try:
                yield relation_data.read(required=True)
                self._relation_cache = dict(relation_data)
            except StateNotFound:
                self._relation_cache = {}
        returnValue(self._relation_cache)

    @inlineCallbacks
    def get_value(self, unit_name, key):
        settings = yield self.get(unit_name)
        returnValue(settings.get(key, ""))

    def set(self, data):
        return fail(RelationBrokenContextError(
            "Cannot change settings in broken relation"))

    def set_value(self, key, value):
        return fail(RelationBrokenContextError(
            "Cannot change settings in broken relation"))

    def delete_value(self, key):
        return fail(RelationBrokenContextError(
            "Cannot change settings in broken relation"))

    def has_read(self, unit_name):
        """Has the context been used to access the settings of the unit.
        """
        if unit_name in (None, self._unit_name):
            return self._relation_cache is not None
        return False
