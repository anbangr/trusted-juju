import operator
from UserDict import DictMixin

from juju.errors import ConstraintError, UnknownConstraintError


def _dont_convert(s):
    return s


class _ConstraintType(object):
    """Defines a constraint.

    :param str name: The constraint's name
    :param default: The default value as a str, or None to indicate "unset"
    :param converter: Function to convert str value to "real" value (and
        thereby implicitly validate it; should raise ValueError)
    :param comparer: Function used to determine whether one constraint
        satisfies another
    :param bool visible: If False, indicates a computed constraint which
        should not be settable by a user.

    Merely creating a Constraint does not activate it; you also need to
    register it with a specific ConstraintSet.
    """

    def __init__(self, name, default, converter, comparer, visible):
        self.name = name
        self.default = default
        self._converter = converter
        self._comparer = comparer
        self.visible = visible

    def convert(self, s):
        """Convert a string representation of a constraint into a useful form.
        """
        if s is None:
            return
        try:
            return self._converter(s)
        except ValueError as e:
            raise ConstraintError(
                "Bad %r constraint %r: %s" % (self.name, s, e))

    def can_satisfy(self, candidate, benchmark):
        """Check whether candidate can satisfy benchmark"""
        return self._comparer(candidate, benchmark)


class ConstraintSet(object):
    """A ConstraintSet represents all constraints applicable to a provider.

    Individual providers can construct ConstraintSets which will be used to
    construct Constraints objects directly relevant to that provider."""

    def __init__(self, provider_type):
        self._provider_type = provider_type
        self._registry = {}
        self._conflicts = {}

        # These constraints must always be available (but are not user-visible
        # or -settable).
        self.register("ubuntu-series", visible=False)
        self.register("provider-type", visible=False)

    def register(self, name, default=None, converter=_dont_convert,
                 comparer=operator.eq, visible=True):
        """Register a constraint to be handled by this ConstraintSet.

        :param str name: The constraint's name
        :param default: The default value as a str, or None to indicate "unset"
        :param converter: Function to convert str value to "real" value (and
            thereby implicitly validate it; should raise ValueError)
        :param comparer: Function used to determine whether one constraint
            satisfies another
        :param bool visible: If False, indicates a computed constraint which
            should not be settable by a user.
        """
        self._registry[name] = _ConstraintType(
            name, default, converter, comparer, visible)
        self._conflicts[name] = set()

    def register_conflicts(self, reds, blues):
        """Set cross-constraint override behaviour.

        :param reds: list of constraint names which affect all constraints
            specified in `blues`
        :param blues: list of constraint names which affect all constraints
            specified in `reds`

        When two constraints conflict:

        * It is an error to set both constraints in the same Constraints.
        * When a Constraints overrides another which specifies a conflicting
          constraint, the value in the overridden Constraints is cleared.
        """
        for red in reds:
            self._conflicts[red].update(blues)
        for blue in blues:
            self._conflicts[blue].update(reds)

    def register_generics(self, instance_type_names):
        """Register a common set of constraints.

        This always includes arch, cpu, and mem; and will include instance-type
        if instance_type_names is not empty. This is because we believe
        instance-type to be a broadly applicable concept, even though the only
        provider that registers names here (and hence accepts the constraint)
        is currently EC2.
        """
        self.register("arch", default="amd64", converter=_convert_arch)
        self.register(
            "cpu", default="1", converter=_convert_cpu, comparer=operator.ge)
        self.register(
            "mem", default="512M", converter=_convert_mem,
            comparer=operator.ge)

        if instance_type_names:

            def convert(instance_type_name):
                if instance_type_name in instance_type_names:
                    return instance_type_name
                raise ValueError("unknown instance type")

            self.register("instance-type", converter=convert)
            self.register_conflicts(["cpu", "mem"], ["instance-type"])

    def names(self):
        """Get the names of all registered constraints."""
        return self._registry.keys()

    def get(self, name):
        """Get the (internal) _ConstraintType object corresponding to `name`.

        Returns None if no _ConstraintType has been registered under that name.
        """
        return self._registry.get(name)

    def parse(self, strs):
        """Create a Constraints from strings (as used on the command line)"""
        data = {"provider-type": self._provider_type}
        for s in strs:
            try:
                name, value = s.split("=", 1)
                constraint = self.get(name)
                if constraint is None:
                    raise UnknownConstraintError(name)
                if value == "any":
                    value = None
                if value == "":
                    value = constraint.default
                constraint.convert(value)
            except ValueError as e:
                raise ConstraintError(
                    "Could not interpret %r constraint: %s" % (s, e))
            if not constraint.visible:
                raise ConstraintError(
                    "Cannot set computed constraint: %r" % name)
            data[name] = value

        conflicts = set()
        for name in sorted(data):
            if data[name] is None:
                continue
            for conflict in sorted(self._conflicts[name]):
                if conflict in data:
                    raise ConstraintError(
                        "Ambiguous constraints: %r overlaps with %r"
                        % (name, conflict))
                conflicts.add(conflict)

        data.update(dict((conflict, None) for conflict in conflicts))
        return Constraints(self, data)

    def load(self, data):
        """Convert a data dict to a Constraints"""
        for k, v in data.items():
            constraint = self.get(k)
            if constraint is not None:
                # Include all of data; validate those parts we know how to.
                constraint.convert(v)
        return Constraints(self, data)


class Constraints(object, DictMixin):
    """A Constraints object encapsulates a set of machine constraints.

    Constraints instances should not be constructed directly; please use
    ConstraintSet's parse and load methods instead.

    They implement a dict interface, which exposes all constraints for the
    appropriate provider, and is the expected mode of usage for clients not
    concerned with the construction or comparison of Constraints objects.

    A Constraints object only ever contains a single "layer" of data, but can
    be combined with other Constraints objects in such a way as to produce a
    single object following the rules laid down in internals/placement-spec.

    Constraints objects can be compared, in a limited sense, by using the
    `can_satisfy` method.
    """

    def __init__(self, available, data):
        self._available = available
        self._data = data

    def keys(self):
        """DictMixin"""
        return self._available.names()

    def __getitem__(self, name):
        """DictMixin"""
        if name not in self.keys():
            raise KeyError(name)
        constraint = self._available.get(name)
        raw_value = self.data.get(name, constraint.default)
        return constraint.convert(raw_value)

    def with_series(self, series):
        """Return a Constraints with the "ubuntu-series" set to `series`"""
        data = dict(self._data)
        data["ubuntu-series"] = series
        return self._available.load(data)

    @property
    def complete(self):
        """Have provider-type and ubuntu-series both been set?"""
        return None not in (
            self.get("provider-type"), self.get("ubuntu-series"))

    @property
    def data(self):
        """Return a dict suitable for serialisation and reconstruction.

        Note that data contains (1) the specified value for every
        constraint that has been explicitly set, and (2) a None value for
        every constraint which conflicts with one that has been set.

        Therefore, by updating one Constraints's data with another's,
        any setting thus masked on the lower level will be preserved as None;
        consequently, Constraints~s can be collapsed onto one another without
        losing any information that is not overridden (whether implicitly or
        explicitly) by the overriding Constraints.
        """
        return dict(self._data)

    def update(self, other):
        """Overwrite `self`'s data from `other`."""
        self._data.update(other.data)

    def can_satisfy(self, other):
        """Can a machine with constraints `self` be used for a unit with
        constraints `other`? ie ::

            if machine_constraints.can_satisfy(unit_constraints):
                # place unit on machine
        """
        if not (self.complete and other.complete):
            # Incomplete constraints cannot satisfy or be satisfied; we should
            # only ever hit this branch if we're running new code (that knows
            # about constraints) against an old deployment (which will contain
            # at least *some* services/machines which don't have constraints).
            return False

        for (name, unit_value) in other.items():
            if unit_value is None:
                # The unit doesn't care; any machine value will be fine.
                continue
            machine_value = self[name]
            if machine_value is None:
                # The unit *does* care, and the machine value isn't
                # specified, so we can't guarantee a match. If we were
                # to update machine constraints after provisioning (ie
                # when we knew the values of the constraints left
                # unspecified) we'd hit this branch less often.  We
                # may also need to do something clever here to get
                # sensible machine reuse on ec2 -- in what
                # circumstances, if ever, is it OK to place a unit
                # specced for one instance-type on a machine of
                # another type? Does it matter if either or both were
                # derived from generic constraints? What about cost?
                return False
            constraint = self._available.get(name)
            if not constraint.can_satisfy(machine_value, unit_value):
                # The machine's value is definitely not ok for the unit.
                return False

        return True


#==============================================================================
# Generic constraint information (used by multiple providers).
_VALID_ARCHS = ("i386", "amd64", "arm")
_MEGABYTES = 1
_GIGABYTES = _MEGABYTES * 1024
_TERABYTES = _GIGABYTES * 1024
_MEM_SUFFIXES = {"M": _MEGABYTES, "G": _GIGABYTES, "T": _TERABYTES}


def _convert_arch(s):
    if s in _VALID_ARCHS:
        return s
    raise ValueError("unknown architecture")


def _convert_cpu(s):
    value = float(s)
    if value >= 0:
        return value
    raise ValueError("must be non-negative")


def _convert_mem(s):
    if s[-1] in _MEM_SUFFIXES:
        value = float(s[:-1]) * _MEM_SUFFIXES[s[-1]]
    else:
        value = float(s)
    if value >= 0:
        return value
    raise ValueError("must be non-negative")
