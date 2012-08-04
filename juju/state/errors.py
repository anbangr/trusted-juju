from juju.errors import JujuError


class StateError(JujuError):
    """Base class for state-related errors."""


class StateChanged(StateError):
    """Service state was modified while operation was in progress.

    This is generally a situation which should be avoided via locks,
    ignored if it was an automated procedure, or reported back to the
    user when operating interactively.
    """

    def __str__(self):
        return "State changed while operation was in progress"


class StopWatcher(JujuError):
    """Exception value to denote watching should stop.
    """


class StateNotFound(StateError):
    """State not found.

    Expecting a Zookeeper node with serialised state but it could not
    be found at a given path.
    """
    def __init__(self, path):
        self.path = path

    def __str__(self):
        return "State for %s not found." % self.path


class PrincipalNotFound(StateError):

    def __init__(self, principal_name):
        self.principal_name = principal_name

    def __str__(self):
        return "Principal %r not found" % self.principal_name


class CharmStateNotFound(StateError):
    """Charm state was not found."""

    def __init__(self, charm_id):
        self.charm_id = charm_id

    def __str__(self):
        return "Charm %r was not found" % self.charm_id


class ServiceStateNotFound(StateError):
    """Service state was not found."""

    def __init__(self, service_name):
        self.service_name = service_name

    def __str__(self):
        return "Service %r was not found" % self.service_name


class ServiceStateNameInUse(StateError):
    """Service name is already in use."""

    def __init__(self, service_name):
        self.service_name = service_name

    def __str__(self):
        return "Service name %r is already in use" % self.service_name


class ServiceUnitStateNotFound(StateError):
    """Service unit state was not found."""

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return "Service unit %r was not found" % self.unit_name


class BadServiceStateName(StateError):
    """Service name was misused when another service name was expected.

    This may happen, for instance, because unit names embed service
    names in them (e.g. wordpress/2), so there's a chance to misuse
    such a unit name in an incorrect location.
    """

    def __init__(self, expected_name, obtained_name):
        self.expected_name = expected_name
        self.obtained_name = obtained_name

    def __str__(self):
        return "Expected service name %r but got %r" % \
               (self.expected_name, self.obtained_name)


class MachineStateNotFound(StateError):
    """Machine state was not found."""

    def __init__(self, machine_id):
        self.machine_id = machine_id

    def __str__(self):
        return "Machine %r was not found" % self.machine_id


class MachineStateInUse(StateError):
    """Machine state in use."""

    def __init__(self, machine_id):
        self.machine_id = machine_id

    def __str__(self):
        return "Resources are currently assigned to machine %r" % \
            self.machine_id


class NoUnusedMachines(StateError):
    """No unused machines are available for assignment."""

    def __str__(self):
        return "No unused machines are available for assignment"


class IllegalSubordinateMachineAssignment(StateError):
    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return ("Unable to assign subordinate %s to machine." % (
            self.unit_name))


class ServiceUnitStateMachineAlreadyAssigned(StateError):

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return "Service unit %r is already assigned to a machine" % \
               self.unit_name


class ServiceUnitStateMachineNotAssigned(StateError):

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return "Service unit %r is not assigned to a machine" % \
               self.unit_name


class ServiceUnitDebugAlreadyEnabled(StateError):
    """The unit already is in debug mode.
    """

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return "Service unit %r is already in debug mode." % \
            self.unit_name


class ServiceUnitUpgradeAlreadyEnabled(StateError):
    """The unit has already been marked for upgrade.
    """

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return "Service unit %r is already marked for upgrade." % (
            self.unit_name)


class ServiceUnitResolvedAlreadyEnabled(StateError):
    """The unit has already been marked resolved.
    """

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return "Service unit %r is already marked as resolved." % (
            self.unit_name)


class ServiceUnitRelationResolvedAlreadyEnabled(StateError):
    """The relation has already been marked resolved.
    """

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return "Service unit %r already has relations marked as resolved." % (
            self.unit_name)


class RelationAlreadyExists(StateError):

    def __init__(self, endpoints):
        self.endpoints = endpoints

    def __str__(self):
        services = [endpoint.service_name for endpoint in self.endpoints]
        if len(services) > 1:
            return "Relation %s already exists between %s" % (
                self.endpoints[0].relation_type,
                " and ".join(services))
        else:
            return "Relation %s already exists for %s" % (
                self.endpoints[0].relation_type, services[0])


class RelationStateNotFound(StateError):

    def __str__(self):
        return "Relation not found"


class UnitRelationStateAlreadyAssigned(StateError):
    """The unit already exists in the relation."""

    def __init__(self, relation_id, relation_name, unit_name):
        self.relation_id = relation_id
        self.relation_name = relation_name
        self.unit_name = unit_name

    def __str__(self):
        return "The relation %r already contains a unit for %r" % (
            self.relation_name,
            self.unit_name)


class UnitRelationStateNotFound(StateError):
    """The unit does not exist in the relation."""

    def __init__(self, relation_id, relation_name, unit_name):
        self.relation_id = relation_id
        self.relation_name = relation_name
        self.unit_name = unit_name

    def __str__(self):
        return "The relation %r has no unit state for %r" % (
            self.relation_name,
            self.unit_name)


class PrincipalServiceUnitRequired(StateError):
    """Non-principal service unit was used as a container."""
    def __init__(self, service_name, value):
        self.service_name = service_name
        self.value = value

    def __str__(self):
        return ("Expected principal service unit as container "
                "for %s instance, got %r" % (
                    self.service_name, self.value))


class UnitMissingContainer(StateError):
    """The subordinate unit was added without a container."""

    def __init__(self, unit_name):
        self.unit_name = unit_name

    def __str__(self):
        return ("The unit %s expected a principal container "
                "but none was assigned." % (
                    self.unit_name))


class SubordinateUsedAsContainer(StateError):
    """The subordinate unit was used to contain another unit."""

    def __init__(self, service_name, other_unit):
        self.service_name = service_name
        self.other_unit = other_unit

    def __str__(self):
        return "Attempted to assign unit of %s to subordinate %s" % (
            self.service_name,
            self.other_unit)


class NotSubordinateCharm(StateError):
    """Can not add `subordinate: false` to a container."""

    def __init__(self, service_name, other_unit):
        self.service_name = service_name
        self.other_unit = other_unit

    def __str__(self):
        return "%s cannot be used as subordinate to %s" % (
            self.service_name,
            self.other_unit)


class UnsupportedSubordinateServiceRemoval(StateError):
    """
    Currently unspported to remove subordinate services once a container
    relation has been established.

    This will change in a future release, however, removal of subordinate
    services is complicated by stop hooks not being called properly
    at this time.
    """
    def __init__(self, subordinate_service_name, principal_service_name):
        self.subordinate_service_name = subordinate_service_name
        self.principal_service_name = principal_service_name

    def __str__(self):
        return  ("Unsupported attempt to destroy subordinate "
                 "service '%s' while principal service '%s' "
                 "is related." % (
                     self.subordinate_service_name,
                     self.principal_service_name))


class EnvironmentStateNotFound(StateError):

    """Environment state was not found."""

    def __str__(self):
        return "Environment state was not found"


class UnknownRelationRole(StateError):
    """An unknown relation type was specified."""

    def __init__(self, relation_id, relation_role, service_name):
        self.relation_id = relation_id
        self.relation_role = relation_role
        self.service_name = service_name

    def __str__(self):
        return "Unknown relation role %r for service %r" % (
            self.relation_role, self.service_name)


class BadDescriptor(ValueError, JujuError):
    """Descriptor is not valid.

    A descriptor must be of the form <service name>[:<relation name>].
    Currently the only restriction on these names is that they not
    embed colons, but we may wish to impose other restrictions.
    """

    def __init__(self, descriptor):
        self.descriptor = descriptor

    def __str__(self):
        return "Bad descriptor: %r" % (self.descriptor,)


class DuplicateEndpoints(StateError):
    """Endpoints cannot be duplicate."""

    def __init__(self, *endpoints):
        self.endpoints = endpoints

    def __str__(self):
        return "Duplicate endpoints: %r" % (self.endpoints,)


class IncompatibleEndpoints(StateError):
    """Endpoints are incompatible."""

    def __init__(self, *endpoints):
        self.endpoints = endpoints

    def __str__(self):
        return "Incompatible endpoints: %r" % (self.endpoints,)


class NoMatchingEndpoints(StateError):
    """Endpoints do not match in a relation."""

    def __str__(self):
        return "No matching endpoints"


class AmbiguousRelation(StateError):
    """Endpoints have more than one possible shared relation."""

    def __init__(self, requested_pair, endpoint_pairs):
        self.requested_pair = requested_pair
        self.endpoint_pairs = endpoint_pairs

    def __str__(self):
        relations = []
        for (end1, end2) in self.endpoint_pairs:
            relations.append("  '%s:%s %s:%s' (%s %s / %s %s)" % (
                end1.service_name, end1.relation_name,
                end2.service_name, end2.relation_name,
                end1.relation_type, end1.relation_role,
                end2.relation_type, end2.relation_role))
        requested = "%s %s" % self.requested_pair
        return "Ambiguous relation %r; could refer to:\n%s" % (
            requested, "\n".join(sorted(relations)))


class RelationBrokenContextError(StateError):
    """An inappropriate operation was attempted in a relation-broken hook"""


class InvalidRelationIdentity(ValueError, JujuError):
    """Relation identity is not valid.

    A relation identity must be of the form <relation name>:<internal
    relation id>.
    """

    def __init__(self, relation_ident):
        self.relation_ident = relation_ident

    def __str__(self):
        return "Not a valid relation id: %r" % (self.relation_ident,)
