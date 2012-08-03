import inspect
import os

from twisted.internet.defer import fail, succeed, inlineCallbacks
from twisted.web.error import Error

from juju.errors import ProviderError
from juju.lib.testing import TestCase
from juju.lib.mocker import MATCH
from juju.providers import ec2
from juju.providers.ec2.utils import (
    get_current_ami, get_instance_type, get_machine_spec)

from .common import get_constraints

from juju.providers.ec2.utils import VerifyingContextFactory


IMAGE_HOST = "cloud-images.ubuntu.com"
IMAGE_URI_TEMPLATE = "\
https://%s/query/%%s/server/released.current.txt" % (IMAGE_HOST)

IMAGE_DATA_DIR = os.path.join(
    os.path.dirname(inspect.getabsfile(ec2)), "tests", "data")


class GetCurrentAmiTest(TestCase):

    def test_bad_url(self):
        """
        If the requested page doesn't exist at all, a LookupError is raised
        """
        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "nutty", contextFactory=None)
        self.mocker.result(fail(Error("404")))
        self.mocker.replay()
        d = get_current_ami("nutty", "i386", "us-east-1", False, False)
        self.failUnlessFailure(d, LookupError)
        return d

    def test_umatched_ami(self):
        """
        If an ami is not found that matches the specifications, then
        a LookupError is raised.
        """
        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "lucid", contextFactory=None)
        self.mocker.result(succeed(""))
        self.mocker.replay()
        d = get_current_ami("lucid", "i386", "us-east-1", False, False)
        self.failUnlessFailure(d, LookupError)
        return d

    def test_current_ami(self):
        """The current server machine image can be retrieved."""
        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "lucid", contextFactory=None)
        self.mocker.result(succeed(
            open(os.path.join(IMAGE_DATA_DIR, "lucid.txt")).read()))

        self.mocker.replay()
        d = get_current_ami("lucid", "i386", "us-east-1", False, False)
        d.addCallback(self.assertEquals, "ami-714ba518")
        return d

    def test_current_ami_by_arch(self):
        """The current server machine image can be retrieved by arch."""
        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "lucid", contextFactory=None)
        self.mocker.result(
            succeed(open(
                os.path.join(IMAGE_DATA_DIR, "lucid.txt")).read()))
        self.mocker.replay()
        d = get_current_ami("lucid", "amd64", "us-east-1", False, False)
        d.addCallback(self.assertEquals, "ami-4b4ba522")
        return d

    def test_current_ami_by_region(self):
        """The current server machine image can be retrieved by region."""
        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "lucid", contextFactory=None)
        self.mocker.result(
            succeed(open(
                os.path.join(IMAGE_DATA_DIR, "lucid.txt")).read()))
        self.mocker.replay()
        d = get_current_ami("lucid", "i386", "us-west-1", False, False)
        d.addCallback(self.assertEquals, "ami-cb97c68e")
        return d

    def test_current_ami_with_virtualisation_info(self):
        """The current server machine image can be retrieved by arch."""
        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "natty", contextFactory=None)
        self.mocker.result(
            succeed(open(
                os.path.join(IMAGE_DATA_DIR, "natty.txt")).read()))
        self.mocker.replay()
        d = get_current_ami("natty", "amd64", "us-east-1", True, False)
        d.addCallback(self.assertEquals, "ami-1cad5275")
        return d

    def test_hvm_request_on_old_series(self):
        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "lucid", contextFactory=None)
        self.mocker.result(
            succeed(open(
                os.path.join(IMAGE_DATA_DIR, "lucid.txt")).read()))
        self.mocker.replay()
        d = get_current_ami("lucid", "amd64", "us-east-1", True, False)
        self.failUnlessFailure(d, LookupError)
        return d

    def test_current_ami_ssl_verify(self):
        """
        Test that the appropriate SSL verifying context factory is passed.
        """

        def match_context(value):
            if VerifyingContextFactory is None:
                # We're running against an older twisted version without
                # certificate verification.
                return value is None
            return isinstance(value, VerifyingContextFactory)

        page = self.mocker.replace("twisted.web.client.getPage")
        page(IMAGE_URI_TEMPLATE % "lucid",
                contextFactory=MATCH(match_context))
        self.mocker.result(succeed(
            open(os.path.join(IMAGE_DATA_DIR, "lucid.txt")).read()))
        self.mocker.replay()
        d = get_current_ami("lucid", "i386", "us-east-1", False, True)

        def verify_result(result):
            self.assertEqual(result, "ami-714ba518")

        d.addCallback(verify_result)
        return d


class GetImageIdTest(TestCase):

    @inlineCallbacks
    def assert_image_id(self, config, constraints, series, arch, region, hvm,
                        instance_type, ssl_verify=False):
        get_current_ami_m = self.mocker.replace(get_current_ami)
        get_current_ami_m(series, arch, region, hvm, ssl_verify)
        self.mocker.result(succeed("ami-giggle"))
        self.mocker.replay()

        spec = yield get_machine_spec(config, constraints)
        self.assertEquals(spec.image_id, "ami-giggle")
        self.assertEquals(spec.instance_type, instance_type)

    def test_ssl_hostname_verification(self):
        constraints = yield get_constraints(["arch=i386"])
        yield self.assert_image_id(
            {}, constraints,
            "splendid", "i386", "us-east-1", False,
            "m1.small", ssl_verify=True)

    @inlineCallbacks
    def test_empty_config(self):
        constraints = yield get_constraints(["arch=i386"])
        yield self.assert_image_id(
            {}, constraints,
            "splendid", "i386", "us-east-1", False,
            "m1.small")

    @inlineCallbacks
    def test_series_from_config(self):
        config = {"default-series": "puissant"}
        constraints = yield get_constraints(["arch=i386"], None)
        yield self.assert_image_id(
            config, constraints,
            "puissant", "i386", "us-east-1", False,
            "m1.small")

    @inlineCallbacks
    def test_arch_from_instance_constraint(self):
        constraints = yield get_constraints([
            "instance-type=m1.large", "arch=any"])
        yield self.assert_image_id(
            {}, constraints,
            "splendid", "amd64", "us-east-1", False,
            "m1.large")

    @inlineCallbacks
    def test_arch_from_nothing(self):
        constraints = yield get_constraints([
            "arch=any", "cpu=any", "mem=any", "instance-type=any"])
        yield self.assert_image_id(
            {}, constraints,
            "splendid", "amd64", "us-east-1", False,
            "t1.micro")

    @inlineCallbacks
    def test_hvm_from_cluster_instance(self):
        constraints = yield get_constraints(["instance-type=cc2.8xlarge"])
        yield  self.assert_image_id(
            {}, constraints,
            "splendid", "amd64", "us-east-1", True,
            "cc2.8xlarge")

    @inlineCallbacks
    def test_region_from_config(self):
        config = {"region": "sa-east-1"}
        constraints = yield get_constraints(["arch=amd64"], "desperate")
        yield  self.assert_image_id(
            config, constraints,
            "desperate", "amd64", "sa-east-1", False,
            "m1.small")

    @inlineCallbacks
    def test_config_override_ami_only(self):
        self.mocker.replace(get_current_ami)
        self.mocker.replay()

        constraints = yield get_constraints(["instance-type=t1.micro"])
        spec = yield get_machine_spec(
            {"default-image-id": "ami-blobble"}, constraints)
        self.assertEquals(spec.image_id, "ami-blobble")
        self.assertEquals(spec.instance_type, "t1.micro")

    @inlineCallbacks
    def test_config_override_ami_and_instance(self):
        self.mocker.replace(get_current_ami)
        self.mocker.replay()

        constraints = yield get_constraints(["instance-type=t1.micro"])
        spec = yield get_machine_spec(
            {"default-image-id": "ami-blobble",
                "default-instance-type": "q1.arbitrary"}, constraints)
        self.assertEquals(spec.image_id, "ami-blobble")
        self.assertEquals(spec.instance_type, "q1.arbitrary")


class GetInstanceTypeTest(TestCase):

    @inlineCallbacks
    def assert_instance_type(self, strs, expected):
        constraints = yield get_constraints(strs)
        instance_type = get_instance_type({}, constraints)
        self.assertEquals(instance_type, expected)

    @inlineCallbacks
    def assert_no_instance_type(self, strs):
        constraints = yield get_constraints(strs)
        e = self.assertRaises(
            ProviderError, get_instance_type, {}, constraints)
        self.assertIn("No instance type satisfies", str(e))

    @inlineCallbacks
    def test_basic(self):
        yield self.assert_instance_type([], "m1.small")
        yield self.assert_instance_type(
            ["instance-type=cg1.4xlarge"], "cg1.4xlarge")

    @inlineCallbacks
    def test_picks_cheapest(self):
        yield self.assert_instance_type(["mem=0", "cpu=0"], "t1.micro")
        yield self.assert_instance_type(["arch=i386", "mem=0", "cpu=0"], "t1.micro")
        yield self.assert_instance_type(["arch=amd64", "mem=0", "cpu=0"], "t1.micro")
        yield self.assert_instance_type(["arch=i386", "cpu=1"], "m1.small")
        yield self.assert_instance_type(["arch=amd64", "cpu=1"], "m1.small")
        yield self.assert_instance_type(["cpu=5"], "c1.medium")
        yield self.assert_instance_type(["cpu=5", "mem=2G"], "m2.xlarge")
        yield self.assert_instance_type(["cpu=50"], "cc2.8xlarge")

    @inlineCallbacks
    def test_unsatisfiable(self):
        yield self.assert_no_instance_type(["cpu=99"])
        yield self.assert_no_instance_type(["mem=100G"])
        yield self.assert_no_instance_type(["arch=i386", "mem=5G"])
        yield self.assert_no_instance_type(["arch=arm"])

    @inlineCallbacks
    def test_config_override(self):
        constraints = yield get_constraints([])
        instance_type = get_instance_type(
            {"default-instance-type": "whatever-they-typed"}, constraints)
        self.assertEquals(instance_type, "whatever-they-typed")
