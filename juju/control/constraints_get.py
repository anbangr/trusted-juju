import argparse
import sys
import yaml

from twisted.internet.defer import inlineCallbacks

from juju.control.utils import get_environment, sync_environment_state
from juju.state.environment import EnvironmentStateManager
from juju.state.machine import MachineStateManager
from juju.state.service import ServiceStateManager


def configure_subparser(subparsers):
    sub_parser = subparsers.add_parser(
        "get-constraints",
        help=command.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=constraints_get.__doc__)

    sub_parser.add_argument(
        "--environment", "-e",
        help="Environment to affect")

    sub_parser.add_argument(
        "entities",
        nargs="*",
        help="names of machines, units or services")

    return sub_parser


def command(options):
    """Show currently applicable constraints"""
    environment = get_environment(options)
    return constraints_get(
        options.environments, environment, options.entities, options.log)


@inlineCallbacks
def constraints_get(env_config, environment, entity_names, log):
    """
    Show the complete set of applicable constraints for each specified entity.

    This will show the final computed values of all constraints (including
    internal constraints which cannot be set directly via set-constraints).
    """
    provider = environment.get_machine_provider()
    client = yield provider.connect()
    result = {}
    try:
        yield sync_environment_state(client, env_config, environment.name)
        if entity_names:
            msm = MachineStateManager(client)
            ssm = ServiceStateManager(client)
            for name in entity_names:
                if name.isdigit():
                    kind = "machine"
                    entity = yield msm.get_machine_state(name)
                elif "/" in name:
                    kind = "service unit"
                    entity = yield ssm.get_unit_state(name)
                else:
                    kind = "service"
                    entity = yield ssm.get_service_state(name)
                log.info("Fetching constraints for %s %s", kind, name)
                constraints = yield entity.get_constraints()
                result[name] = dict(constraints)
        else:
            esm = EnvironmentStateManager(client)
            log.info("Fetching constraints for environment")
            constraints = yield esm.get_constraints()
            result = dict(constraints)
        yaml.safe_dump(result, sys.stdout)
    finally:
        yield client.close()
