from base64 import b64decode
import os
import yaml

from twisted.internet.defer import inlineCallbacks

from txzookeeper import ZookeeperClient

from juju.state.initialize import StateHierarchy


def configure_subparser(subparsers):
    sub_parser = subparsers.add_parser("initialize", help=command.__doc__)
    sub_parser.add_argument(
        "--instance-id", required=True,
        help="Provider instance id for the bootstrap node")
    sub_parser.add_argument(
        "--admin-identity", required=True,
        help="Admin access control identity for zookeeper ACLs")
    sub_parser.add_argument(
        "--constraints-data", required=True,
        help="Base64-encoded yaml dump of the environment constraints data")
    sub_parser.add_argument(
        "--provider-type", required=True,
        help="Environment machine provider type")
    return sub_parser


@inlineCallbacks
def command(options):
    """
    Initialize Zookeeper hierarchy
    """
    zk_address = os.environ.get("ZOOKEEPER_ADDRESS", "127.0.0.1:2181")
    client = yield ZookeeperClient(zk_address).connect()
    try:
        constraints_data = yaml.load(b64decode(options.constraints_data))
        hierarchy = StateHierarchy(
            client,
            options.admin_identity,
            options.instance_id,
            constraints_data,
            options.provider_type)
        yield hierarchy.initialize()
    finally:
        yield client.close()
