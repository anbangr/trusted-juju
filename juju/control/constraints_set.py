import argparse

from twisted.internet.defer import inlineCallbacks

from juju.control import legacy
from juju.control.utils import get_environment, sync_environment_state
from juju.state.environment import EnvironmentStateManager
from juju.state.service import ServiceStateManager


def configure_subparser(subparsers):
    sub_parser = subparsers.add_parser(
        "set-constraints",
        help=command.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=constraints_set.__doc__)

    sub_parser.add_argument(
        "--environment", "-e",
        help="Environment to affect")

    sub_parser.add_argument(
        "--service", "-s", default=None,
        help="Service to set constraints on")

    sub_parser.add_argument(
        "constraints",
        nargs="+",
        help="name=value for constraint to set")

    return sub_parser


def command(options):
    """Set machine constraints for the environment, or for a named service.
    """
    environment = get_environment(options)
    env_config = options.environments
    return constraints_set(
        env_config, environment, options.service, options.constraints)


@inlineCallbacks
def constraints_set(env_config, environment, service_name, constraint_strs):
    """
    Machine constraints allow you to pick the hardware to which your services
    will be deployed. Examples:

    $ juju set-constraints --service-name mysql mem=8G cpu=4

    $ juju set-constraints instance-type=t1.micro

    Available constraints vary by provider type, and will be ignored if not
    understood by the current environment's provider. The current set of
    available constraints across all providers is:

    On Amazon EC2:

        * arch (CPU architecture: i386/amd64/arm; amd64 by default)
        * cpu (processing power in Amazon ECU; 1 by default)
        * mem (memory in [MGT]iB; 512M by default)
        * instance-type (unset by default)
        * ec2-zone (unset by default)

    On Orchestra:

        * orchestra-classes (unset by default)

    On MAAS:

        * maas-name (unset by default)

    Service settings, if specified, will override environment settings, which
    will in turn override the juju defaults of mem=512M, cpu=1, arch=amd64.

    New constraints set on an entity will completely replace that entity's
    pre-existing constraints.

    To override an environment constraint with the juju default when setting
    service constraints, just specify "name=" (rather than just not specifying
    the constraint at all, which will cause it to inherit the environment's
    value).

    To entirely unset a constraint, specify "name=any".
    """
    provider = environment.get_machine_provider()
    constraint_set = yield provider.get_constraint_set()
    constraints = constraint_set.parse(constraint_strs)
    client = yield provider.connect()
    try:
        yield legacy.check_constraints(client, constraint_strs)
        yield sync_environment_state(client, env_config, environment.name)
        if service_name is None:
            esm = EnvironmentStateManager(client)
            yield esm.set_constraints(constraints)
        else:
            ssm = ServiceStateManager(client)
            service = yield ssm.get_service_state(service_name)
            yield service.set_constraints(constraints)
    finally:
        yield client.close()
