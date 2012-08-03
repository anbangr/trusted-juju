from twisted.internet.defer import inlineCallbacks

from juju.environment.errors import EnvironmentsConfigError
from juju.errors import ConstraintError
from juju.lib.testing import TestCase
from juju.providers.ec2.files import FileStorage
from juju.providers.ec2 import MachineProvider
from juju.environment.errors import EnvironmentsConfigError
import logging

from .common import EC2TestMixin
from juju.providers.ec2 import ssl


class ProviderTestCase(EC2TestMixin, TestCase):

    def setUp(self):
        super(ProviderTestCase, self).setUp()
        self.mocker.replay()

    def test_default_service_factory_construction(self):
        """
        Ensure that the AWSServiceRegion gets called by the MachineProvider
        with the right arguments.  This explores the mocking which is already
        happening within EC2TestMixin.
        """
        expected_kwargs = {"access_key": "foo",
                           "secret_key": "bar",
                           "ec2_uri": "https://ec2.us-east-1.amazonaws.com",
                           "s3_uri": ""}

        MachineProvider(self.env_name, {"access-key": "foo", "secret-key": "bar"})
        self.assertEquals(self.service_factory_kwargs, expected_kwargs)

    def test_service_factory_construction(self):
        """
        Ensure that the AWSServiceRegion gets called by the MachineProvider
        with the right arguments when they are present in the configuration.
        This explores the mocking which is already happening within
        EC2TestMixin.
        """
        config = {"access-key": "secret-123",
                  "secret-key": "secret-abc",
                  "ec2-uri": "the-ec2-uri",
                  "s3-uri": "the-ec2-uri"}
        expected_kwargs = {}
        for key, value in config.iteritems():
            expected_kwargs[key.replace("-", "_")] = value
        MachineProvider(self.env_name, config)
        self.assertEquals(self.service_factory_kwargs, expected_kwargs)

    def test_service_factory_construction_region_provides_ec2_uri(self):
        """
        The EC2 service URI can be dereferenced by region name alone.
        This explores the mocking which is already happening within
        EC2TestMixin.
        """
        config = {"access-key": "secret-123",
                  "secret-key": "secret-abc",
                  "s3-uri": "the-ec2-uri",
                  "region": "eu-west-1"}
        expected_kwargs = {}
        for key, value in config.iteritems():
            expected_kwargs[key.replace("-", "_")] = value
        del expected_kwargs["region"]
        expected_kwargs["ec2_uri"] = "https://ec2.eu-west-1.amazonaws.com"
        MachineProvider(self.env_name, config)
        self.assertEquals(self.service_factory_kwargs, expected_kwargs)

    def test_provider_attributes(self):
        """
        The provider environment name and config should be available as
        parameters in the provider.
        """
        provider = self.get_provider()
        self.assertEqual(provider.environment_name, self.env_name)
        self.assertEqual(provider.config.get("type"), "ec2")
        self.assertEqual(provider.provider_type, "ec2")

    def test_get_file_storage(self):
        """The file storage is accessible via the machine provider."""
        provider = self.get_provider()
        storage = provider.get_file_storage()
        self.assertTrue(isinstance(storage, FileStorage))

    def test_config_serialization(self):
        """
        The provider configuration can be serialized to yaml.
        """
        keys_path = self.makeFile("my-keys")

        config = {"access-key": "secret-123",
                  "secret-key": "secret-abc",
                  "authorized-keys-path": keys_path}

        expected_serialization = config.copy()
        expected_serialization.pop("authorized-keys-path")
        expected_serialization["authorized-keys"] = "my-keys"

        provider = MachineProvider(self.env_name, config)
        serialized = provider.get_serialization_data()
        self.assertEqual(serialized, expected_serialization)

    def test_config_environment_extraction(self):
        """
        The provider serialization loads keys as needed from the environment.

        Variables from the configuration take precendence over those from
        the environment, when serializing.
        """
        config = {"access-key": "secret-12345",
                  "secret-key": "secret-abc",
                  "authorized-keys": "0123456789abcdef"}

        environ = {
            "AWS_SECRET_ACCESS_KEY": "secret-abc",
            "AWS_ACCESS_KEY_ID": "secret-123"}

        self.change_environment(**environ)
        provider = MachineProvider(
            self.env_name, {"access-key": "secret-12345",
                            "authorized-keys": "0123456789abcdef"})
        serialized = provider.get_serialization_data()
        self.assertEqual(config, serialized)

    def test_ssl_hostname_verification_config(self):
        """
        Tests that SSL hostname verification is enabled in txaws
        when the config setting is set to true
        """

        config = {"access-key": "secret-12345",
                  "secret-key": "secret-abc",
                  "authorized-keys": "0123456789abcdef",
                  "ssl-hostname-verification": True}
        provider = MachineProvider(self.env_name, config)

        if ssl:
            self.assertTrue(
                    provider._service.ec2_endpoint.ssl_hostname_verification)
            self.assertTrue(
                    provider._service.s3_endpoint.ssl_hostname_verification)
        else:
            self.assertFalse(hasattr(provider._service.ec2_endpoint,
                             'ssl_hostname_verification'))
            self.assertFalse(hasattr(provider._service.s3_endpoint,
                             'ssl_hostname_verification'))

    def test_warn_on_no_ssl_hostname_verification(self):
        """
        We should warn the user whenever they are not using hostname
        verification.
        """
        config = {"access-key": "secret-12345",
                  "secret-key": "secret-abc",
                  "authorized-keys": "0123456789abcdef",
                  "ssl-hostname-verification": False}
        output = self.capture_logging("juju.ec2", level=logging.WARN)
        provider = MachineProvider(self.env_name, config)

        self.assertIn('EC2 API calls encrypted but not authenticated',
                output.getvalue())
        self.assertIn('S3 API calls encrypted but not authenticated',
                output.getvalue())
        self.assertIn(
                'Ubuntu Cloud Image lookups encrypted but not authenticated',
                output.getvalue())
        if ssl:
            self.assertIn('ssl-hostname-verification is disabled',
                          output.getvalue())
            self.assertFalse(
                    provider._service.ec2_endpoint.ssl_hostname_verification)
            self.assertFalse(
                    provider._service.s3_endpoint.ssl_hostname_verification)
        else:
            self.assertIn('txaws.client.ssl unavailable', output.getvalue())

    def test_get_legacy_config_keys(self):
        provider = MachineProvider(self.env_name, {
            "access-key": "foo", "secret-key": "bar",
            # Note: these keys *will* at some stage be considered legacy keys;
            # they're included here to make sure the tests are updated when we
            # make that change.
            "default-series": "foo", "placement": "bar"})
        self.assertEquals(provider.get_legacy_config_keys(), set())

        # These keys are not valid on Amazon EC2...
        provider.config.update({
            "default-instance-type": "baz", "default-image-id": "qux"})
        self.assertEquals(provider.get_legacy_config_keys(), set((
            "default-instance-type", "default-image-id")))

        # ...but they still are when using private clouds.
        provider.config.update({"ec2-uri": "anything"})
        self.assertEquals(provider.get_legacy_config_keys(), set())


class ProviderConstraintsTestCase(TestCase):

    def constraint_set(self):
        provider = MachineProvider(
            "some-ec2-env", {"access-key": "f", "secret-key": "x"})
        return provider.get_constraint_set()

    @inlineCallbacks
    def assert_invalid(self, msg, *strs):
        cs = yield self.constraint_set()
        e = self.assertRaises(ConstraintError, cs.parse, strs)
        self.assertEquals(str(e), msg)

    @inlineCallbacks
    def test_constraints(self):
        cs = yield self.constraint_set()
        self.assertEquals(cs.parse([]), {
            "provider-type": "ec2",
            "ubuntu-series": None,
            "instance-type": None,
            "ec2-zone": None,
            "arch": "amd64",
            "cpu": 1.0,
            "mem": 512.0})
        self.assertEquals(cs.parse(["ec2-zone=X", "instance-type=m1.small"]), {
            "provider-type": "ec2",
            "ubuntu-series": None,
            "instance-type": "m1.small",
            "ec2-zone": "x",
            "arch": "amd64",
            "cpu": None,
            "mem": None})

        yield self.assert_invalid(
            "Bad 'ec2-zone' constraint '7': expected single ascii letter",
            "ec2-zone=7")
        yield self.assert_invalid(
            "Bad 'ec2-zone' constraint 'blob': expected single ascii letter",
            "ec2-zone=blob")
        yield self.assert_invalid(
            "Bad 'instance-type' constraint 'qq1.moar': unknown instance type",
            "instance-type=qq1.moar")
        yield self.assert_invalid(
            "Ambiguous constraints: 'cpu' overlaps with 'instance-type'",
            "instance-type=m1.small", "cpu=1")
        yield self.assert_invalid(
            "Ambiguous constraints: 'instance-type' overlaps with 'mem'",
            "instance-type=m1.small", "mem=2G")

    @inlineCallbacks
    def test_satisfy_zone_constraint(self):
        cs = yield self.constraint_set()
        a = cs.parse(["ec2-zone=a"]).with_series("series")
        b = cs.parse(["ec2-zone=b"]).with_series("series")
        self.assertTrue(a.can_satisfy(a))
        self.assertTrue(b.can_satisfy(b))
        self.assertFalse(a.can_satisfy(b))
        self.assertFalse(b.can_satisfy(a))

    @inlineCallbacks
    def xtest_non_amazon_constraints(self):
        # Disabled because the ec2 provider requires these keys (instance-type
        # and ec2-zone)
        provider = MachineProvider("some-non-ec2-env", {
            "ec2-uri": "blah", "secret-key": "foobar", "access-key": "bar"})
        cs = yield provider.get_constraint_set()
        self.assertEquals(cs.parse([]), {
            "provider-type": "ec2",
            "ubuntu-series": None})


class FailCreateTest(TestCase):

    def test_conflicting_authorized_keys_options(self):
        """
        We can't handle two different authorized keys options, so deny
        constructing an environment that way.
        """
        config = {}
        config["authorized-keys"] = "File content"
        config["authorized-keys-path"] = "File path"
        error = self.assertRaises(EnvironmentsConfigError,
                                  MachineProvider, "some-env-name", config)
        self.assertEquals(
            str(error),
            "Environment config cannot define both authorized-keys and "
            "authorized-keys-path. Pick one!")
