import yaml

from juju.errors import IncompatibleVersion


# The protocol version, which is stored in the /topology node under
# the "version" key. The protocol version should *only* be updated
# when we know that a version is in fact actually incompatible.

VERSION = 2


class InternalTopologyError(Exception):
    """Inconsistent action attempted.

    This is mostly for testing and debugging, since it should never
    happen in practice.
    """


class InternalTopology(object):
    """Helper to deal with the high-level topology map stored in ZK.

    This must not be used outside of juju.state.  To work with the
    topology itself, check out the "machine" and "service"modules.

    The internal topology implementation is based on the use of single
    node to function as a logical map of some entities within the
    zookeeper hierarchy.  Being a single node means that it may be
    changed atomically, and thus the network of services have a
    central consistency point which may be used to develop further
    algorithms on top of.  Without it, incomplete creation of various
    multi-node objects would have to be considered.

    This topology contains details such as service names, service unit
    sequence numbers on a per service basis, mapping of service_id to
    service names (service names are inteded as display names, and
    hence subject to change), and mapping of service units to
    machines.

    The internal state is maintained in a dictionary, but its
    structure and storage format should not be depended upon.

    """

    _nil_dict = {}

    def __init__(self):
        self._state = {"version": VERSION}

    def reset(self):
        """Put the InternalTopology back in its initial state.
        """
        self._state = {"version": VERSION}

    def dump(self):
        """Return string containing the state of this topology.

        This string may be provided to the :method:`parse` to
        reestablish the same topology state back.
        """
        return yaml.safe_dump(self._state)

    def parse(self, data):
        """Parse the dumped data provided and restore internal state.

        The provided data must necessarily have been retrieved by
        calling the :method:`dump`.
        """
        parsed = yaml.load(data)
        self._state = parsed
        version = self.get_version()
        if version != VERSION:
            # This will raise if it cannot update the topology.
            migrate_topology(self)

    def get_version(self):
        return self._state.get("version", 0)

    def add_machine(self, machine_id):
        """Add the given machine_id to the topology state.
        """
        machines = self._state.setdefault("machines", {})
        if machine_id in machines:
            raise InternalTopologyError(
                "Attempted to add duplicated "
                "machine (%s)" % machine_id)
        machines[machine_id] = {}

    def has_machine(self, machine_id):
        """Return True if machine_id was registered in the topology.
        """
        return machine_id in self._state.get(
            "machines", self._nil_dict)

    def get_machines(self):
        """Return list of machine ids registered in this topology.
        """
        return sorted(self._state.get("machines", self._nil_dict).keys())

    def machine_has_units(self, machine_id):
        """Return True if machine has any assigned units."""
        self._assert_machine(machine_id)
        services = self._state.get("services", self._nil_dict)
        for service in services.itervalues():
            for unit in service["units"].itervalues():
                if unit.get("machine") == machine_id:
                    return True
        return False

    def remove_machine(self, machine_id):
        """Remove machine_id from this topology.
        """
        self._assert_machine(machine_id)
        if self.machine_has_units(machine_id):
            raise InternalTopologyError(
                "Can't remove machine %r while units are assigned"
                % machine_id)
        # It's fine, so remove it.
        del self._state["machines"][machine_id]

    def add_service(self, service_id, service_name):
        """Add service_id to this topology.
        """
        services = self._state.setdefault("services", {})
        if service_id in services:
            raise InternalTopologyError(
                "Attempted to add duplicated service: %s" % service_id)
        for some_service_id in services:
            if services[some_service_id].get("name") == service_name:
                raise InternalTopologyError(
                    "Service name %r already in use" % service_name)
        services[service_id] = {"name": service_name,
                                "units": {}}
        unit_sequence = self._state.setdefault("unit-sequence", {})
        if not service_name in unit_sequence:
            unit_sequence[service_name] = 0

    def has_service(self, service_id):
        """Return True if service_id was previously added.
        """
        return service_id in self._state.get(
            "services", self._nil_dict)

    def get_service_name(self, service_id):
        """Return service name for the given service id.
        """
        self._assert_service(service_id)
        return self._state["services"][service_id]["name"]

    def find_service_with_name(self, service_name):
        """Return service_id for the named service, or None."""
        services = self._state.get("services", ())
        for service_id in services:
            if services[service_id].get("name") == service_name:
                return service_id
        return None

    def get_services(self):
        """Return list of previously added service ids.
        """
        return self._state.get("services", {}).keys()

    def remove_service(self, service_id):
        """Remove service_id from this topology.
        """
        self._assert_service(service_id)
        relations = self.get_relations_for_service(service_id)
        if relations:
            raise InternalTopologyError(
                "Service %r is associated to relations %s" % (
                    service_id, relations))
        del self._state["services"][service_id]

    def add_service_unit(self, service_id, unit_id, container_id=None):
        """Register unit_id under service_id in this topology state.

        The new unit id registered will get a sequence number assigned
        to it.  The sequence number increases monotonically for each
        service, and is helpful to provide nice unit names for users.

        :param container_id: optional unit_id of the principal
        service unit to which the new unit is subordinate. Defaults to
        None. If a `container` scoped relationship to the service of
        the principal node doesn't exist InternalTopologyError is
        raised.

        :return: The sequence number assigned to the unit_id.

        """
        self._assert_service(service_id)
        services = self._state["services"]
        for some_service_id in services:
            if unit_id in services[some_service_id]["units"]:
                raise InternalTopologyError(
                    "Unit %s already in service: %s" %
                    (unit_id, some_service_id))

        if container_id is not None:
            principal_unit_service = self.get_service_unit_service(
                container_id)
            relations = self.get_relations_for_service(service_id)
            found_container_relation = False

            for relation in relations:
                relation_id = relation["relation_id"]
                if (self.relation_has_service(
                        relation_id,
                        principal_unit_service) and
                    self.get_relation_scope(relation_id) ==
                    "container"):
                    found_container_relation = True
                    break

            if not found_container_relation:
                raise InternalTopologyError(
                    "Attempted to add subordinate unit "
                    "without container relation")

        service = services[service_id]
        services[service_id]["units"][unit_id] = unit = {}
        unit["sequence"] = self._state[ \
                                "unit-sequence"][service["name"]]
        unit["container"] = container_id
        self._state["unit-sequence"][service["name"]] += 1
        return unit["sequence"]

    def has_service_unit(self, service_id, unit_id):
        """Return True if unit_id was exists under service_id.
        """
        self._assert_service(service_id)
        service = self._state["services"][service_id]
        return unit_id in service.get("units", self._nil_dict)

    def get_service_units(self, service_id):
        """Return list of unit_id registered under service_id.
        """
        self._assert_service(service_id)
        service = self._state["services"].get(service_id,
                                              self._nil_dict)
        return service.get("units", self._nil_dict).keys()

    def get_service_unit_service(self, unit_id):
        """Given a unit id, return its corresponding service id."""
        services = self._state.get("services", self._nil_dict)
        for service_id, service in services.iteritems():
            if unit_id in service["units"]:
                return service_id
        raise InternalTopologyError("Service unit ID %s not "
                                    "found" % unit_id)

    def get_service_unit_name(self, service_id, unit_id):
        """Return the user-oriented name for the given unit."""
        self._assert_service_unit(service_id, unit_id)
        service = self._state["services"][service_id]
        service_name = service["name"]
        unit_sequence = service["units"][unit_id]["sequence"]
        return "%s/%s" % (service_name, unit_sequence)

    def get_service_unit_id_from_name(self, unit_name):
        """Return the service unit id from the unit name."""
        service_name, unit_sequence_id = unit_name.split("/")
        service_id = self.find_service_with_name(service_name)
        unit_id = self.find_service_unit_with_sequence(
            service_id, int(unit_sequence_id))
        return unit_id

    def get_service_unit_name_from_id(self, unit_id):
        """Retrieve the user-oriented name from the given unit.

        A simple convenience accessor.
        """
        service_id = self.get_service_unit_service(unit_id)
        return self.get_service_unit_name(service_id, unit_id)

    def get_service_unit_principal(self, unit_id):
        services = self._state.get("services", self._nil_dict)
        for service_id, service in services.iteritems():
            if unit_id in service["units"]:
                unit_info = service["units"][unit_id]
                return unit_info.get("container")

        raise InternalTopologyError("Service unit ID %s not "
                                    "found" % unit_id)

    def get_service_unit_container(self, unit_id):
        """Return information about the container of a unit.

        If the unit_id has a container this method returns
        (service_id, service_name, sequence, container_id). Otherwise
        it returns None.
        """
        container_id = self.get_service_unit_principal(unit_id)
        if container_id is None:
            return None

        service_id = self.get_service_unit_service(container_id)
        container_unit_name = self.get_service_unit_name(service_id,
                                                         container_id)
        service_name, sequence = container_unit_name.rsplit("/", 1)
        sequence = int(sequence)
        return (service_id, service_name, sequence, container_id)

    def remove_service_unit(self, service_id, unit_id):
        """Remove unit_id from under service_id in the topology state.
        """
        self._assert_service_unit(service_id, unit_id)
        del self._state["services"][service_id]["units"][unit_id]

    def find_service_unit_with_sequence(self, service_id, sequence):
        """Return unit_id with the given sequence under service_id.

        @return: unit_id with the given sequence, or None if not found.
        """
        self._assert_service(service_id)
        units = self._state["services"][service_id]["units"]
        for unit_id in units:
            if units[unit_id]["sequence"] == sequence:
                return unit_id
        return None

    def get_service_unit_sequence(self, service_id, unit_id):
        """Return the sequence number for the given service unit.
        """
        self._assert_service_unit(service_id, unit_id)
        unit = self._state["services"][service_id]["units"][unit_id]
        return unit["sequence"]

    def assign_service_unit_to_machine(self, service_id, unit_id,
                                       machine_id):
        """Assign the given unit_id to the provided machine_id.

        The unit_id must exist and be in an unassigned state for
        this to work.
        """
        self._assert_service_unit(service_id, unit_id)
        self._assert_machine(machine_id)
        unit = self._state["services"][service_id]["units"][unit_id]
        if "machine" in unit:
            raise InternalTopologyError(
                "Service unit %s in service %s already "
                "assigned to a machine."
                % (unit_id, service_id))
        unit["machine"] = machine_id

    def get_service_unit_machine(self, service_id, unit_id):
        """Return the machine_id the unit_id is assigned to, or None.
        """
        self._assert_service_unit(service_id, unit_id)
        unit = self._state["services"][service_id]["units"][unit_id]
        if "machine" not in unit:
            return None
        return unit["machine"]

    def unassign_service_unit_from_machine(self, service_id, unit_id):
        """Unassign the given unit_id from its current machine.

        The unit_id must necessarily be assigned to a machine for this
        to work.
        """
        self._assert_service_unit(service_id, unit_id)
        unit = self._state["services"][service_id]["units"][unit_id]
        if "machine" not in unit:
            raise InternalTopologyError(
                "Service unit %s in service %s is not "
                "assigned to a machine." % (service_id, unit_id))
        del unit["machine"]

    def get_service_units_in_machine(self, machine_id):
        self._assert_machine(machine_id)
        units = []
        services = self._state.get("services", self._nil_dict)
        for service_id, service in services.iteritems():
            for unit_id, unit in service["units"].iteritems():
                if unit.get("machine") == machine_id:
                    units.append(unit_id)
        return units

    def add_relation(self, relation_id, relation_type,
                     relation_scope="global"):
        """Add a relation with given id and of the given type.
        """
        relations = self._state.setdefault("relations", {})
        if relation_id in relations:
            raise InternalTopologyError(
                "Relation id %r already in use" % relation_id)
        relations[relation_id] = dict(interface=relation_type,
                                      scope=relation_scope,
                                      services=dict())

    def has_relation(self, relation_id):
        """Return True if relation with relation_id exists.
        """
        return relation_id in self._state.get(
            "relations", self._nil_dict)

    def get_relations(self):
        """Returns a list of relation_id in the topology.
        """
        return self._state.get("relations", self._nil_dict).keys()

    def get_relation_services(self, relation_id):
        """Get all the services associated to the relation.
        """
        self._assert_relation(relation_id)
        relation_data = self._state["relations"][relation_id]
        return relation_data["services"]

    def get_relation_type(self, relation_id):
        """Get the type of a relation (its interface name)."""
        self._assert_relation(relation_id)
        relation_data = self._state["relations"][relation_id]
        return relation_data["interface"]

    def get_relation_scope(self, relation_id):
        """Get the scope of a relation."""
        self._assert_relation(relation_id)
        relation_data = self._state["relations"][relation_id]
        return relation_data["scope"]

    def relation_has_service(self, relation_id, service_id):
        """Return if `service_id` is assigned to `relation_id`."""
        relations = self._state.get("relations", self._nil_dict)
        relation_data = relations.get(relation_id, self._nil_dict)
        services = relation_data.get("services", self._nil_dict)
        return service_id in services

    def remove_relation(self, relation_id):
        """It should be possible to remove a relation.
        """
        self._assert_relation(relation_id)
        del self._state["relations"][relation_id]

    def assign_service_to_relation(self, relation_id, service_id,
                                   name, role):
        """Associate a service to a relation.

        @param role: The relation role of the service.
        @param name: The relation name from the service.
        """
        self._assert_service(service_id)
        self._assert_relation(relation_id)
        relation_data = self._state["relations"][relation_id]
        services = relation_data["services"]
        for sid in services:
            if sid == service_id:
                raise InternalTopologyError(
                    "Service %r is already assigned "
                    "to relation %r" % (service_id, relation_id))
            service_info = services[sid]
            if service_info["role"] == role:
                raise InternalTopologyError(
                    ("Another service %r is already providing %r "
                     "role in relation") % (sid,
                                            service_info["role"]))
        services[service_id] = {"role": role, "name": name}

    def unassign_service_from_relation(self, relation_id, service_id):
        """Disassociate service to relation.
        """
        self._assert_service(service_id)
        self._assert_relation(relation_id)
        relation_data = self._state["relations"][relation_id]
        services = relation_data["services"]
        if not service_id in services:
            raise InternalTopologyError(
                "Service %r is not assigned to relation %r" % (
                    service_id, relation_id))
        del services[service_id]

    def get_relation_service(self, relation_id, service_id):
        """Retrieve the service settings for a relation."""
        self._assert_service(service_id)
        self._assert_relation(relation_id)

        relation_data = self._state.get("relations").get(relation_id)
        if not service_id in relation_data.get(
                "services", self._nil_dict):
            raise InternalTopologyError(
                "Service %r not assigned to relation %r" % (
                    service_id, relation_id))
        return (relation_data["interface"],
                relation_data["services"][service_id])

    def get_relations_for_service(self, service_id):
        """Given a service id retrieve its relations."""
        self._assert_service(service_id)
        relations = []

        relations_dict = self._state.get("relations", self._nil_dict)
        for relation_id, relation_data in relations_dict.items():
            services = relation_data.get("services")
            if services and service_id in services:
                relations.append(dict(
                    relation_id=relation_id,
                    interface=relation_data["interface"],
                    scope=relation_data["scope"],
                    service=services[service_id]))
        return relations

    def _assert_relation(self, relation_id):
        if relation_id not in self._state.get(
                "relations", self._nil_dict):
            raise InternalTopologyError(
                "Relation not found: %s" % relation_id)

    def _assert_machine(self, machine_id):
        if machine_id not in self._state.get(
                "machines", self._nil_dict):
            raise InternalTopologyError(
                "Machine not found: %s" % machine_id)

    def _assert_service(self, service_id):
        if service_id not in self._state.get(
                "services", self._nil_dict):
            raise InternalTopologyError(
                "Service not found: %s" % service_id)

    def _assert_service_unit(self, service_id, unit_id):
        self._assert_service(service_id)
        service = self._state["services"][service_id]
        if unit_id not in service.get("units", self._nil_dict):
            raise InternalTopologyError(
                "Service unit %s not found in service %s" % (
                    unit_id, service_id))

    def has_relation_between_endpoints(self, endpoints):
        """Check if relation exists between `endpoints`.

        The relation, with a ``relation type`` common to the
        endpoints, must exist between all endpoints (presumably one
        for peer, two for client-server). The topology for the
        relations looks like the following in YAML::

          relations:
            relation-0000000000:
            - mysql
            - global
            - service-0000000000: {name: db, role: client}
              service-0000000001: {name: server, role: server}
        """
        service_ids = dict((e, self.find_service_with_name(
            e.service_name)) for e in endpoints)
        relations = self._state.get("relations", self._nil_dict)
        for relation_data in relations.itervalues():
            scope = relation_data["scope"]
            services = relation_data["services"]
            for endpoint in endpoints:
                service = services.get(service_ids[endpoint])
                if not service or service["name"] != \
                   endpoint.relation_name or \
                   scope != endpoint.relation_scope:
                    break
            else:
                return True
        return False

    def get_relation_between_endpoints(self, endpoints):
        """Return relation id existing between `endpoints` or None"""
        service_ids = dict((e, self.find_service_with_name(
            e.service_name))
                           for e in endpoints)
        relations = self._state.get("relations", self._nil_dict)
        for relation_id, relation_data in relations.iteritems():
            interface = relation_data["interface"]
            services = relations[relation_id]["services"]
            if interface != endpoints[0].relation_type:
                continue
            for endpoint in endpoints:
                service = services.get(service_ids[endpoint])
                if not service or service["name"] != endpoint.relation_name:
                    break
            else:
                return relation_id
        return None


def _migrate_version_1(topology):
    """Migrate topology version 1 to version 2.

    This change includes the transition from::

          relations:
            relation-0000000000:
            - mysql
            - service-0000000000: {name: db, role: client}
              service-0000000001: {name: server, role: server}

    to::

          relations
              relation-00000001:
                  interface: name
                  scope: name
                  services:
                    service-00000001: {name: name, role: role}
                    service-00000002: {name: name, role: role}

    for all relations.

    """
    version = topology.get_version()
    if version > 1:
        return topology
    elif version != 1:
        raise IncompatibleVersion(version, VERSION)

    relations = topology._state.get("relations")
    if relations:
        new_relations = {}
        for relation, relation_data in relations.items():
            new_relations[relation] = {}
            relation_type, relation_services = relation_data
            new_relations["interface"] = relation_type
            new_relations["scope"] = "global"
            new_relations["services"] = relation_services

        topology._state["relations"] = new_relations

    topology._state["version"] = 2
    return topology


# A dict of version migration plans for VERSION n (where n is the key)
# to version n + 1. migrate_version will be called until the topology
# version is equal to VERSION. If no migration plan exists FAIL
_VERSION_MIGRATION = {

# DISABLED: We can't migrate the topology till we can migrate older
# code currently running in the env that depends on a previous format.
# 1: _migrate_version_1
}


def migrate_topology(topology):
    """Migrate topology version to current.

    Does an in-place (destructive) stepwise modification of topology
    state from its current version to the VERSION represented in this
    module.
    """
    version = topology.get_version()
    if version == VERSION:
        return topology

    current_version = version
    while current_version < VERSION:
        if current_version not in _VERSION_MIGRATION:
            raise IncompatibleVersion(version, VERSION)
        _VERSION_MIGRATION[current_version](topology)
        current_version = topology.get_version()
