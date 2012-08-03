"""Implementation of destroy service subcommand"""

from twisted.internet.defer import inlineCallbacks

from juju.state.errors import UnsupportedSubordinateServiceRemoval
from juju.state.relation import RelationStateManager
from juju.state.service import ServiceStateManager
from juju.control.utils import get_environment


def configure_subparser(subparsers):
    """Configure destroy-service subcommand"""
    sub_parser = subparsers.add_parser("destroy-service", help=command.__doc__)
    sub_parser.add_argument(
        "--environment", "-e",
        help="Environment to add the relation in.")
    sub_parser.add_argument(
        "service_name",
        help="Name of the service to stop")
    return sub_parser


def command(options):
    """Destroy a running service, its units, and break its relations."""
    environment = get_environment(options)
    return destroy_service(
        options.environments,
        environment,
        options.verbose,
        options.log,
        options.service_name)


@inlineCallbacks
def destroy_service(config, environment, verbose, log, service_name):
    provider = environment.get_machine_provider()
    client = yield provider.connect()
    service_manager = ServiceStateManager(client)
    service_state = yield service_manager.get_service_state(service_name)
    if (yield service_state.is_subordinate()):
        # We can destroy the service if does not have relations.
        # That implies that principals have already been torn
        # down (or were never added).
        relation_manager = RelationStateManager(client)
        relations = yield relation_manager.get_relations_for_service(
            service_state)

        if relations:
            principal_service = None
            # if we have a container we can destroy the subordinate
            # (revisit in the future)
            for relation in relations:
                if relation.relation_scope != "container":
                    continue
                services = yield relation.get_service_states()
                remote_service = [s for s in services if s.service_name !=
                                     service_state.service_name][0]
                if not (yield remote_service.is_subordinate()):
                    principal_service = remote_service
                    break

            if principal_service:
                raise UnsupportedSubordinateServiceRemoval(
                    service_state.service_name,
                    principal_service.service_name)

    yield service_manager.remove_service_state(service_state)
    log.info("Service %r destroyed.", service_state.service_name)
