from collections import namedtuple
import csv
import logging
import operator
from string import ascii_lowercase
import StringIO

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web.client import getPage
from twisted.web.error import Error

from juju.errors import ProviderError

# We don't actually know what's available in any given region
_PLAUSIBLE_ZONES = ascii_lowercase

# "cost" is measured in $/h in us-east-1
# "hvm" is True if an HVM image is required
_InstanceType = namedtuple("_InstanceType", "arch cpu mem cost hvm")

# some instance types can be started as 1386 or amd64
_EITHER_ARCH = "?"

INSTANCE_TYPES = {
    # t1.micro cpu is "up to 2", but in practice "very little"
    "t1.micro": _InstanceType(_EITHER_ARCH, 0.1, 613, 0.02, False),

    "m1.small": _InstanceType(_EITHER_ARCH, 1, 1740, 0.08, False),
    "m1.medium": _InstanceType(_EITHER_ARCH, 2, 3840, 0.16, False),
    "m1.large": _InstanceType("amd64", 4, 7680, 0.32, False),
    "m1.xlarge": _InstanceType("amd64", 8, 15360, 0.64, False),

    "m2.xlarge": _InstanceType("amd64", 6.5, 17510, 0.45, False),
    "m2.2xlarge": _InstanceType("amd64", 13, 35020, 0.9, False),
    "m2.4xlarge": _InstanceType("amd64", 26, 70040, 1.8, False),

    "c1.medium": _InstanceType(_EITHER_ARCH, 5, 1740, 0.165, False),
    "c1.xlarge": _InstanceType("amd64", 20, 7168, 0.66, False),

    "cc1.4xlarge": _InstanceType("amd64", 33.5, 23552, 1.3, True),
    "cc2.8xlarge": _InstanceType("amd64", 88, 61952, 2.4, True),

    # also has fancy GPUs we can't currently describe
    "cg1.4xlarge": _InstanceType("amd64", 33.5, 22528, 2.1, True)}

DEFAULT_REGION = "us-east-1"

log = logging.getLogger("juju.ec2")

try:
    from txaws.client import ssl
    from txaws.client.ssl import VerifyingContextFactory
except ImportError:
    ssl = None
    VerifyingContextFactory = None

_CURRENT_IMAGE_HOST = 'cloud-images.ubuntu.com'
_CURRENT_IMAGE_URI_TEMPLATE = (
    "https://%s/query/%s/server/released.current.txt")

MachineSpec = namedtuple("MachineSpec", "instance_type image_id")


def convert_zone(s):
    s = s.lower()
    if len(s) == 1:
        if s in _PLAUSIBLE_ZONES:
            return s
    raise ValueError("expected single ascii letter")


def get_region_uri(region):
    """Get the URL endpoint for the region."""
    return "https://ec2.%s.amazonaws.com" % region


def get_current_ami(series, arch, region, hvm, ssl_verify=False):
    required_kind = "hvm" if hvm else "paravirtual"

    def handle_404(failure):
        failure.trap(Error)
        if failure.value.status == "404":
            raise LookupError((series, arch, region))
        return failure

    def extract_ami(current_data):
        data_stream = StringIO.StringIO(current_data)
        for tokens in csv.reader(data_stream, "excel-tab"):
            if tokens[4] != "ebs":
                continue
            if len(tokens) > 10:
                if tokens[10] != required_kind:
                    continue
            elif hvm:
                raise LookupError("HVM images not available for %s" % series)
            if tokens[5] == arch and tokens[6] == region:
                return tokens[7]
        raise LookupError((series, arch, region))

    uri = _CURRENT_IMAGE_URI_TEMPLATE % (_CURRENT_IMAGE_HOST, series)
    if ssl and ssl_verify:
        contextFactory=VerifyingContextFactory(_CURRENT_IMAGE_HOST)
    else:
        contextFactory=None
    d = getPage(uri, contextFactory=contextFactory)
    d.addErrback(handle_404)
    d.addCallback(extract_ami)
    return d


def _arch_match(actual, desired):
    if actual == desired:
        return True
    if actual == _EITHER_ARCH:
        # Several instance types can be started in 32-bit mode if desired.
        if desired in ("i386", "amd64"):
            return True
    return False


def _filter_func(name, constraints, op):
    desired = constraints[name]
    if not desired:
        return lambda _: True
    def f(type_):
        value = getattr(INSTANCE_TYPES[type_], name)
        return op(value, desired)
    return f


def _get_filters(constraints):
    return (
        _filter_func("arch", constraints, _arch_match),
        _filter_func("cpu", constraints, operator.ge),
        _filter_func("mem", constraints, operator.ge))


def _cost(name):
    return INSTANCE_TYPES[name].cost


def get_instance_type(config, constraints):
    instance_type = config.get("default-instance-type")
    if instance_type is not None:
        return instance_type
    instance_type = constraints["instance-type"]
    if instance_type is not None:
        return instance_type
    possible_types = list(INSTANCE_TYPES)
    for f in _get_filters(constraints):
        possible_types = filter(f, possible_types)
    if not possible_types:
        raise ProviderError(
            "No instance type satisfies %s" % dict(constraints))
    return sorted(possible_types, key=_cost)[0]


@inlineCallbacks
def get_machine_spec(config, constraints):
    instance_type = get_instance_type(config, constraints)
    image_id = config.get("default-image-id")
    if image_id is None:
        series = constraints["ubuntu-series"] or config["default-series"]
        arch = constraints["arch"] or "amd64"
        region = config.get("region", DEFAULT_REGION)
        hvm = INSTANCE_TYPES[instance_type].hvm
        ssl_verify = config.get("ssl-hostname-verification", False)
        image_id = yield get_current_ami(series, arch, region, hvm, ssl_verify)
    returnValue(MachineSpec(instance_type, image_id))
