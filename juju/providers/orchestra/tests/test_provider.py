from twisted.internet.defer import inlineCallbacks

from juju.environment.errors import EnvironmentsConfigError
from juju.errors import ConstraintError
from juju.lib.testing import TestCase
from juju.providers.orchestra import MachineProvider

CONFIG = {"orchestra-server": "somewhe.re",
          "orchestra-user": "henricus",
          "orchestra-pass": "barbatus",
          "acquired-mgmt-class": "acquired",
          "available-mgmt-class": "available"}


class ProviderTestCase(TestCase):

    def test_create_provider(self):
        provider = MachineProvider(
            "tetrascape", CONFIG)
        self.assertEquals(provider.environment_name, "tetrascape")
        self.assertEquals(provider.config, CONFIG)

    def test_config_serialization(self):
        """
        The provider configuration can be serialized to yaml.
        """
        keys_path = self.makeFile("my-keys")

        config = {"orchestra-user": "chaosmonkey",
                  "orchestra-pass": "crash",
                  "acquired-mgmt-class": "madeup-1",
                  "available-mgmt-class": "madeup-2",
                  "orchestra-server": "127.0.0.01",
                  "authorized-keys-path": keys_path}

        expected = {"orchestra-user": "chaosmonkey",
                  "orchestra-pass": "crash",
                  "acquired-mgmt-class": "madeup-1",
                  "available-mgmt-class": "madeup-2",
                  "orchestra-server": "127.0.0.01",
                  "authorized-keys": "my-keys"}

        provider = MachineProvider("tetrascape", config)
        serialized = provider.get_serialization_data()
        self.assertEqual(serialized, expected)

    def test_conflicting_authorized_keys_options(self):
        """
        We can't handle two different authorized keys options, so deny
        constructing an environment that way.
        """
        config = CONFIG.copy()
        config["authorized-keys"] = "File content"
        config["authorized-keys-path"] = "File path"
        error = self.assertRaises(EnvironmentsConfigError,
                                  MachineProvider, "some-env-name", config)
        self.assertEquals(
            str(error),
            "Environment config cannot define both authorized-keys and "
            "authorized-keys-path. Pick one!")

    @inlineCallbacks
    def test_open_port(self):
        log = self.capture_logging("juju.orchestra")
        yield MachineProvider("blah", CONFIG).open_port(None, None, None)
        self.assertIn(
            "Firewalling is not yet implemented in Orchestra", log.getvalue())

    @inlineCallbacks
    def test_close_port(self):
        log = self.capture_logging("juju.orchestra")
        yield MachineProvider("blah", CONFIG).close_port(None, None, None)
        self.assertIn(
            "Firewalling is not yet implemented in Orchestra", log.getvalue())

    @inlineCallbacks
    def test_get_opened_ports(self):
        log = self.capture_logging("juju.orchestra")
        ports = yield MachineProvider("blah", CONFIG).get_opened_ports(None, None)
        self.assertEquals(ports, set())
        self.assertIn(
            "Firewalling is not yet implemented in Orchestra", log.getvalue())

    def constraint_set(self):
        provider = MachineProvider("some-orchestra-env", CONFIG)
        return provider.get_constraint_set()

    @inlineCallbacks
    def test_constraints(self):
        cs = yield self.constraint_set()
        self.assertEquals(cs.parse([]), {
            "provider-type": "orchestra",
            "ubuntu-series": None,
            "orchestra-classes": None})
        self.assertEquals(cs.parse(["orchestra-classes=a,b,c"]), {
            "provider-type": "orchestra",
            "ubuntu-series": None,
            "orchestra-classes": ["a", "b", "c"]})
        self.assertEquals(cs.parse(["orchestra-classes=a, b , c"]), {
            "provider-type": "orchestra",
            "ubuntu-series": None,
            "orchestra-classes": ["a", "b", "c"]})
        self.assertEquals(cs.parse(["orchestra-classes=a,"]), {
            "provider-type": "orchestra",
            "ubuntu-series": None,
            "orchestra-classes": ["a"]})
        self.assertEquals(cs.parse(["orchestra-classes=,"]), {
            "provider-type": "orchestra",
            "ubuntu-series": None,
            "orchestra-classes": None})
        e = self.assertRaises(
            ConstraintError, cs.parse, ["orchestra-classes=foo,available"])
        self.assertEquals(
            str(e),
            "Bad 'orchestra-classes' constraint 'foo,available': The "
            "management class 'available' is used internally and may not "
            "be specified directly.")
        e = self.assertRaises(
            ConstraintError, cs.parse, ["orchestra-classes=acquired,bar"])
        self.assertEquals(
            str(e),
            "Bad 'orchestra-classes' constraint 'acquired,bar': The "
            "management class 'acquired' is used internally and may not "
            "be specified directly.")

    @inlineCallbacks
    def test_satisfy_constraints(self):
        cs = yield self.constraint_set()
        a = cs.parse(["orchestra-classes=a"]).with_series("series")
        ab = cs.parse(["orchestra-classes=a,b"]).with_series("series")
        ac = cs.parse(["orchestra-classes=a,c"]).with_series("series")
        abc = cs.parse(["orchestra-classes=a,b,c"]).with_series("series")
        self.assertTrue(ab.can_satisfy(a))
        self.assertTrue(ab.can_satisfy(ab))
        self.assertTrue(ac.can_satisfy(ac))
        self.assertTrue(abc.can_satisfy(a))
        self.assertTrue(abc.can_satisfy(ab))
        self.assertTrue(abc.can_satisfy(ac))
        self.assertTrue(abc.can_satisfy(abc))
        self.assertFalse(a.can_satisfy(ab))
        self.assertFalse(a.can_satisfy(ac))
        self.assertFalse(a.can_satisfy(abc))
        self.assertFalse(ab.can_satisfy(ac))
        self.assertFalse(ab.can_satisfy(abc))
        self.assertFalse(ac.can_satisfy(ab))
        self.assertFalse(ac.can_satisfy(abc))
