import operator

from juju.errors import ConstraintError
from juju.lib.testing import TestCase
from juju.machine.constraints import Constraints, ConstraintSet

# These objects exist for the convenience of other test files
dummy_cs = ConstraintSet("dummy")
dummy_cs.register_generics([])
dummy_constraints = dummy_cs.parse([])
series_constraints = dummy_constraints.with_series("series")

generic_defaults = {
    "arch": "amd64", "cpu": 1, "mem": 512,
    "ubuntu-series": None, "provider-type": None}
dummy_defaults = dict(generic_defaults, **{
    "provider-type": "dummy"})
ec2_defaults = dict(generic_defaults, **{
    "provider-type": "ec2",
    "ec2-zone": None,
    "instance-type": None})
orchestra_defaults = {
    "provider-type": "orchestra",
    "ubuntu-series": None,
    "orchestra-classes": None}

all_providers = ["dummy", "ec2", "orchestra"]


def _raiser(exc_type):
    def raise_(s):
        raise exc_type(s)
    return raise_


class ConstraintsTestCase(TestCase):

    def assert_error(self, message, *raises_args):
        e = self.assertRaises(ConstraintError, *raises_args)
        self.assertEquals(str(e), message)

    def assert_roundtrip_equal(self, cs, constraints, expected):
        self.assertEquals(dict(constraints), expected)
        self.assertEquals(constraints, expected)
        self.assertEquals(dict(cs.load(constraints.data)), expected)
        self.assertEquals(cs.load(constraints.data), expected)


class ConstraintsTest(ConstraintsTestCase):

    def test_equality(self):
        self.assert_roundtrip_equal(
            dummy_cs, dummy_constraints, dummy_defaults)

    def test_complete(self):
        incomplete_constraints = dummy_cs.parse([])
        complete_constraints = incomplete_constraints.with_series("wandering")
        self.assertTrue(complete_constraints.complete)

    def assert_invalid(self, message, *constraint_strs):
        self.assert_error(
            message, dummy_cs.parse, constraint_strs)

    def test_invalid_input(self):
        """Reject nonsense constraints"""
        self.assert_invalid(
            "Could not interpret 'BLAH' constraint: need more than 1 value to "
            "unpack",
            "BLAH")
        self.assert_invalid(
            "Unknown constraint: 'foo'",
            "foo=", "bar=")

    def test_invalid_constraints(self):
        """Reject nonsensical constraint values"""
        self.assert_invalid(
            "Bad 'arch' constraint 'leg': unknown architecture",
            "arch=leg")
        self.assert_invalid(
            "Bad 'cpu' constraint '-1': must be non-negative",
            "cpu=-1")
        self.assert_invalid(
            "Bad 'cpu' constraint 'fish': could not convert string to float: "
            "fish",
            "cpu=fish")
        self.assert_invalid(
            "Bad 'mem' constraint '-1': must be non-negative",
            "mem=-1")
        self.assert_invalid(
            "Bad 'mem' constraint '4P': invalid literal for float(): 4P",
            "mem=4P")

    def test_hidden_constraints(self):
        """Reject attempts to explicitly specify computed constraints"""
        self.assert_invalid(
            "Cannot set computed constraint: 'ubuntu-series'",
            "ubuntu-series=cheesy")
        self.assert_invalid(
            "Cannot set computed constraint: 'provider-type'",
            "provider-type=dummy")


class ConstraintsUpdateTest(ConstraintsTestCase):

    def assert_constraints(self, strss, expected):
        constraints = dummy_cs.parse(strss[0])
        for strs in strss[1:]:
            constraints.update(dummy_cs.parse(strs))
        expected = dict(dummy_defaults, **expected)
        self.assert_roundtrip_equal(dummy_cs, constraints, expected)

    def test_constraints(self):
        """Sane constraints dicts are generated for unknown environments"""
        self.assert_constraints([[]], {})
        self.assert_constraints([["cpu=", "mem="]], {})
        self.assert_constraints([["arch=arm"]], {"arch": "arm"})
        self.assert_constraints([["cpu=0.1"]], {"cpu": 0.1})
        self.assert_constraints([["mem=128"]], {"mem": 128})
        self.assert_constraints([["cpu=0"]], {"cpu": 0})
        self.assert_constraints([["mem=0"]], {"mem": 0})
        self.assert_constraints(
            [["arch=amd64", "cpu=6", "mem=1.5G"]],
            {"arch": "amd64", "cpu": 6, "mem": 1536})

    def test_overwriting_basic(self):
        """Later values shadow earlier values"""
        self.assert_constraints(
            [["cpu=4", "mem=512"], ["arch=i386", "mem=1G"]],
            {"arch": "i386", "cpu": 4, "mem": 1024})

    def test_reset(self):
        """Empty string resets to juju default"""
        self.assert_constraints(
            [["arch=arm", "cpu=4", "mem=1024"], ["arch=", "cpu=", "mem="]],
            {"arch": "amd64", "cpu": 1, "mem": 512})
        self.assert_constraints(
            [["arch=", "cpu=", "mem="], ["arch=arm", "cpu=4", "mem=1024"]],
            {"arch": "arm", "cpu": 4, "mem": 1024})


class ConstraintsFulfilmentTest(ConstraintsTestCase):

    def assert_match(self, c1, c2, expected):
        self.assertEquals(c1.can_satisfy(c2), expected)
        self.assertEquals(c2.can_satisfy(c1), expected)

    def test_fulfil_completeness(self):
        """
        can_satisfy needs to be called on and with complete Constraints~s to
        have any chance of working.
        """
        good = Constraints(
            dummy_cs, {"provider-type": "dummy", "ubuntu-series": "x"})
        self.assert_match(good, good, True)
        bad = [Constraints(dummy_cs, {}),
               Constraints(dummy_cs, {"provider-type": "dummy"}),
               Constraints(dummy_cs, {"ubuntu-series": "x"})]
        for i, bad1 in enumerate(bad):
            self.assert_match(bad1, good, False)
            for bad2 in bad[i:]:
                self.assert_match(bad1, bad2, False)

    def test_fulfil_matches(self):
        other_cs = ConstraintSet("other")
        other_cs.register_generics([])
        other_constraints = other_cs.parse([])
        instances = (
            dummy_constraints.with_series("x"),
            dummy_constraints.with_series("y"),
            other_constraints.with_series("x"),
            other_constraints.with_series("y"))

        for i, c1 in enumerate(instances):
            self.assert_match(c1, c1, True)
            for c2 in instances[i + 1:]:
                self.assert_match(c1, c2, False)

    def assert_can_satisfy(
            self, machine_strs, unit_strs, expected):
        machine = dummy_cs.parse(machine_strs)
        machine = machine.with_series("shiny")
        unit = dummy_cs.parse(unit_strs)
        unit = unit.with_series("shiny")
        self.assertEquals(machine.can_satisfy(unit), expected)

    def test_can_satisfy(self):
        self.assert_can_satisfy([], [], True)

        self.assert_can_satisfy(["arch=arm"], [], False)
        self.assert_can_satisfy(["arch=amd64"], [], True)
        self.assert_can_satisfy([], ["arch=arm"], False)
        self.assert_can_satisfy(["arch=i386"], ["arch=arm"], False)
        self.assert_can_satisfy(["arch=arm"], ["arch=amd64"], False)
        self.assert_can_satisfy(["arch=amd64"], ["arch=amd64"], True)
        self.assert_can_satisfy(["arch=i386"], ["arch=any"], True)
        self.assert_can_satisfy(["arch=arm"], ["arch=any"], True)
        self.assert_can_satisfy(["arch=amd64"], ["arch=any"], True)
        self.assert_can_satisfy(["arch=any"], ["arch=any"], True)
        self.assert_can_satisfy(["arch=any"], ["arch=i386"], False)
        self.assert_can_satisfy(["arch=any"], ["arch=amd64"], False)
        self.assert_can_satisfy(["arch=any"], ["arch=arm"], False)

        self.assert_can_satisfy(["cpu=64"], [], True)
        self.assert_can_satisfy([], ["cpu=64"], False)
        self.assert_can_satisfy(["cpu=64"], ["cpu=32"], True)
        self.assert_can_satisfy(["cpu=32"], ["cpu=64"], False)
        self.assert_can_satisfy(["cpu=64"], ["cpu=64"], True)
        self.assert_can_satisfy(["cpu=0.01"], ["cpu=any"], True)
        self.assert_can_satisfy(["cpu=9999"], ["cpu=any"], True)
        self.assert_can_satisfy(["cpu=any"], ["cpu=any"], True)
        self.assert_can_satisfy(["cpu=any"], ["cpu=0.01"], False)
        self.assert_can_satisfy(["cpu=any"], ["cpu=9999"], False)

        self.assert_can_satisfy(["mem=8G"], [], True)
        self.assert_can_satisfy([], ["mem=8G"], False)
        self.assert_can_satisfy(["mem=8G"], ["mem=4G"], True)
        self.assert_can_satisfy(["mem=4G"], ["mem=8G"], False)
        self.assert_can_satisfy(["mem=8G"], ["mem=8G"], True)
        self.assert_can_satisfy(["mem=2M"], ["mem=any"], True)
        self.assert_can_satisfy(["mem=256T"], ["mem=any"], True)
        self.assert_can_satisfy(["mem=any"], ["mem=any"], True)
        self.assert_can_satisfy(["mem=any"], ["mem=2M"], False)
        self.assert_can_satisfy(["mem=any"], ["mem=256T"], False)


class ConstraintSetTest(TestCase):

    def test_unregistered_name(self):
        cs = ConstraintSet("provider")
        cs.register("bar")
        e = self.assertRaises(ConstraintError, cs.parse, ["bar=2", "baz=3"])
        self.assertEquals(str(e), "Unknown constraint: 'baz'")

    def test_register_invisible(self):
        cs = ConstraintSet("provider")
        cs.register("foo", visible=False)
        e = self.assertRaises(ConstraintError, cs.parse, ["foo=bar"])
        self.assertEquals(str(e), "Cannot set computed constraint: 'foo'")

    def test_register_comparer(self):
        cs = ConstraintSet("provider")
        cs.register("foo", comparer=operator.ne)
        c1 = cs.parse(["foo=bar"]).with_series("series")
        c2 = cs.parse(["foo=bar"]).with_series("series")
        self.assertFalse(c1.can_satisfy(c2))
        self.assertFalse(c2.can_satisfy(c1))
        c3 = cs.parse(["foo=baz"]).with_series("series")
        self.assertTrue(c1.can_satisfy(c3))
        self.assertTrue(c3.can_satisfy(c1))

    def test_register_default_and_converter(self):
        cs = ConstraintSet("provider")
        cs.register("foo", default="star", converter=lambda s: "death-" + s)
        c1 = cs.parse([])
        self.assertEquals(c1["foo"], "death-star")
        c1 = cs.parse(["foo=clock"])
        self.assertEquals(c1["foo"], "death-clock")

    def test_convert_wraps_ValueError(self):
        cs = ConstraintSet("provider")
        cs.register("foo", converter=_raiser(ValueError))
        cs.register("bar", converter=_raiser(KeyError))
        self.assertRaises(ConstraintError, cs.parse, ["foo=1"])
        self.assertRaises(KeyError, cs.parse, ["bar=1"])

    def test_register_conflicts(self):
        cs = ConstraintSet("provider")
        cs.register("foo")
        cs.register("bar")
        cs.register("baz")
        cs.register("qux")
        cs.parse(["foo=1", "bar=2", "baz=3", "qux=4"])

        def assert_ambiguous(strs):
            e = self.assertRaises(ConstraintError, cs.parse, strs)
            self.assertTrue(str(e).startswith("Ambiguous constraints"))

        cs.register_conflicts(["foo"], ["bar", "baz", "qux"])
        assert_ambiguous(["foo=1", "bar=2"])
        assert_ambiguous(["foo=1", "baz=3"])
        assert_ambiguous(["foo=1", "qux=4"])
        cs.parse(["foo=1"])
        cs.parse(["bar=2", "baz=3", "qux=4"])

        cs.register_conflicts(["bar", "baz"], ["qux"])
        assert_ambiguous(["bar=2", "qux=4"])
        assert_ambiguous(["baz=3", "qux=4"])
        cs.parse(["foo=1"])
        cs.parse(["bar=2", "baz=3"])
        cs.parse(["qux=4"])

    def test_register_generics_no_instance_types(self):
        cs = ConstraintSet("provider")
        cs.register_generics([])
        c1 = cs.parse([])
        self.assertEquals(c1["arch"], "amd64")
        self.assertEquals(c1["cpu"], 1.0)
        self.assertEquals(c1["mem"], 512.0)
        self.assertFalse("instance-type" in c1)

        c2 = cs.parse(["arch=any", "cpu=0", "mem=8G"])
        self.assertEquals(c2["arch"], None)
        self.assertEquals(c2["cpu"], 0.0)
        self.assertEquals(c2["mem"], 8192.0)
        self.assertFalse("instance-type" in c2)

    def test_register_generics_with_instance_types(self):
        cs = ConstraintSet("provider")
        cs.register_generics(["a1.big", "c7.peculiar"])
        c1 = cs.parse([])
        self.assertEquals(c1["arch"], "amd64")
        self.assertEquals(c1["cpu"], 1.0)
        self.assertEquals(c1["mem"], 512.0)
        self.assertEquals(c1["instance-type"], None)

        c2 = cs.parse(["arch=any", "cpu=0", "mem=8G"])
        self.assertEquals(c2["arch"], None)
        self.assertEquals(c2["cpu"], 0.0)
        self.assertEquals(c2["mem"], 8192.0)
        self.assertEquals(c2["instance-type"], None)

        c3 = cs.parse(["instance-type=c7.peculiar", "arch=i386"])
        self.assertEquals(c3["arch"], "i386")
        self.assertEquals(c3["cpu"], None)
        self.assertEquals(c3["mem"], None)
        self.assertEquals(c3["instance-type"], "c7.peculiar")

        def assert_ambiguous(strs):
            e = self.assertRaises(ConstraintError, cs.parse, strs)
            self.assertTrue(str(e).startswith("Ambiguous constraints"))

        assert_ambiguous(["cpu=1", "instance-type=c7.peculiar"])
        assert_ambiguous(["mem=1024", "instance-type=c7.peculiar"])

        c4 = cs.parse([])
        c4.update(c2)
        self.assertEquals(c4["arch"], None)
        self.assertEquals(c4["cpu"], 0.0)
        self.assertEquals(c4["mem"], 8192.0)
        self.assertEquals(c4["instance-type"], None)

        c5 = cs.parse(["instance-type=a1.big"])
        c5.update(cs.parse(["arch=i386"]))
        self.assertEquals(c5["arch"], "i386")
        self.assertEquals(c5["cpu"], None)
        self.assertEquals(c5["mem"], None)
        self.assertEquals(c5["instance-type"], "a1.big")

        c6 = cs.parse(["instance-type=a1.big"])
        c6.update(cs.parse(["cpu=20"]))
        self.assertEquals(c6["arch"], "amd64")
        self.assertEquals(c6["cpu"], 20.0)
        self.assertEquals(c6["mem"], None)
        self.assertEquals(c6["instance-type"], None)

        c7 = cs.parse(["instance-type="])
        self.assertEquals(c7["arch"], "amd64")
        self.assertEquals(c7["cpu"], 1.0)
        self.assertEquals(c7["mem"], 512.0)
        self.assertEquals(c7["instance-type"], None)

        c8 = cs.parse(["instance-type=any"])
        self.assertEquals(c8["arch"], "amd64")
        self.assertEquals(c8["cpu"], 1.0)
        self.assertEquals(c8["mem"], 512.0)
        self.assertEquals(c8["instance-type"], None)

    def test_load_validates_known(self):
        cs = ConstraintSet("provider")
        cs.register("foo", converter=_raiser(ValueError))
        e = self.assertRaises(ConstraintError, cs.load, {"foo": "bar"})
        self.assertEquals(str(e), "Bad 'foo' constraint 'bar': bar")

    def test_load_preserves_unknown(self):
        cs = ConstraintSet("provider")
        constraints = cs.load({"foo": "bar"})
        self.assertNotIn("foo", constraints)
        self.assertEquals(constraints.data, {"foo": "bar"})
