import os

import yaml

from twisted.internet.defer import inlineCallbacks

from juju.control import legacy
from juju.control.utils import (
    expand_constraints, expand_path, get_environment, sync_environment_state)

from juju.charm.errors import ServiceConfigValueError
from juju.charm.publisher import CharmPublisher
from juju.charm.repository import resolve
from juju.errors import CharmError
from juju.state.endpoint import RelationEndpoint
from juju.state.placement import place_unit
from juju.state.relation import RelationStateManager
from juju.state.service import ServiceStateManager


def configure_subparser(subparsers):
    sub_parser = subparsers.add_parser("deploy", help=command.__doc__,
            description=deploy.__doc__)

    sub_parser.add_argument(
        "--environment", "-e",
        help="Environment to deploy the charm in.")

    sub_parser.add_argument(
        "--num-units", "-n", default=1, type=int, metavar="NUM",
        help="Number of service units to deploy.")

    sub_parser.add_argument(
        "-u", "--upgrade", default=False, action="store_true",
        help="Deploy the charm on disk, increments revision if needed")

    sub_parser.add_argument(
        "--repository",
        help="Directory for charm lookup and retrieval",
        default=os.environ.get("JUJU_REPOSITORY"),
        type=expand_path)

    sub_parser.add_argument(
        "--constraints",
        help="Hardware constraints for the service",
        default=[],
        type=expand_constraints)

    sub_parser.add_argument(
        "charm", nargs=None,
        help="Charm name")

    sub_parser.add_argument(
        "service_name", nargs="?",
        help="Service name of deployed charm")

    sub_parser.add_argument(
        "--config",
        help="YAML file containing service options")

    return sub_parser


def command(options):
    """
    Deploy a charm to juju!
    """
    environment = get_environment(options)
    return deploy(
        options.environments,
        environment,
        options.repository,
        options.charm,
        options.service_name,
        options.log,
        options.constraints,
        options.config,
        options.upgrade,
        num_units=options.num_units)


def parse_config_options(config_file, service_name, charm):
    if not os.path.exists(config_file) or \
            not os.access(config_file, os.R_OK):
        raise ServiceConfigValueError(
            "Config file %r not accessible." % config_file)

    options = yaml.load(open(config_file, "r").read())
    if not options or not isinstance(options, dict) or \
            service_name not in options:
        raise ServiceConfigValueError(
            "Invalid options file passed to --config.\n"
            "Expected a YAML dict with service name (%r)." % service_name)

    # Validate and type service options and return
    return charm.config.validate(options[service_name])


@inlineCallbacks
def deploy(env_config, environment, repository_path, charm_name,
           service_name, log, constraint_strs, config_file=None, upgrade=False,
           num_units=1):
    """Deploy a charm within an environment.

    This will publish the charm to the environment, creating
    a service from the charm, and get it set to be launched
    on a new machine. If --repository is not specified, it
    will be taken from the environment variable JUJU_REPOSITORY.
    """
    repo, charm_url = resolve(
        charm_name, repository_path, environment.default_series)

    log.info("Searching for charm %s in %s" % (charm_url, repo))
    charm = yield repo.find(charm_url)
    if upgrade:
        if repo.type != "local" or charm.type != "dir":
            raise CharmError(
                charm.path,
                "Only local directory charms can be upgraded on deploy")
        charm.set_revision(charm.get_revision() + 1)

    charm_id = str(charm_url.with_revision(charm.get_revision()))

    # Validate config options prior to deployment attempt
    service_options = {}
    service_name = service_name or charm_url.name
    if config_file:
        service_options = parse_config_options(
            config_file, service_name, charm)

    charm = yield repo.find(charm_url)
    charm_id = str(charm_url.with_revision(charm.get_revision()))

    provider = environment.get_machine_provider()
    placement_policy = provider.get_placement_policy()
    constraint_set = yield provider.get_constraint_set()
    constraints = constraint_set.parse(constraint_strs)
    client = yield provider.connect()

    try:
        yield legacy.check_constraints(client, constraint_strs)
        yield legacy.check_environment(
            client, provider.get_legacy_config_keys())
        yield sync_environment_state(client, env_config, environment.name)

        # Publish the charm to juju
        storage = yield provider.get_file_storage()
        publisher = CharmPublisher(client, storage)
        yield publisher.add_charm(charm_id, charm)
        result = yield publisher.publish()

        # In future we might have multiple charms be published at
        # the same time.  For now, extract the charm_state from the
        # list.
        charm_state = result[0]

        # Create the service state
        service_manager = ServiceStateManager(client)
        service_state = yield service_manager.add_service_state(
            service_name, charm_state, constraints)

        # Use the charm's ConfigOptions instance to validate service
        # options.. Invalid options passed will thrown an exception
        # and prevent the deploy.
        state = yield service_state.get_config()
        charm_config = yield charm_state.get_config()
        # return the validated options with the defaults included
        service_options = charm_config.validate(service_options)
        state.update(service_options)
        yield state.write()

        # Create desired number of service units
        if (yield service_state.is_subordinate()):
            log.info("Subordinate %r awaiting relationship "
                     "to principal for deployment.", service_name)
        else:
            for i in xrange(num_units):
                unit_state = yield service_state.add_unit_state()
                yield place_unit(client, placement_policy, unit_state)

        # Check if we have any peer relations to establish
        if charm.metadata.peers:
            relation_manager = RelationStateManager(client)
            for peer_name, peer_info in charm.metadata.peers.items():
                yield relation_manager.add_relation_state(
                    RelationEndpoint(service_name,
                                     peer_info["interface"],
                                     peer_name,
                                     "peer"))

        log.info("Charm deployed as service: %r", service_name)
    finally:
        yield client.close()
