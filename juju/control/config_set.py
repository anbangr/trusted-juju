import argparse

import yaml

from twisted.internet.defer import inlineCallbacks

from juju.charm.errors import ServiceConfigValueError
from juju.control.utils import get_environment
from juju.hooks.cli import parse_keyvalue_pairs
from juju.state.service import ServiceStateManager


def configure_subparser(subparsers):
    sub_parser = subparsers.add_parser(
        "set",
        help=config_set.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=command.__doc__)

    sub_parser.add_argument(
        "--environment", "-e",
        help="Environment to status.")

    sub_parser.add_argument(
        "service_name",
        help="The name of the service the options apply to.")

    sub_parser.add_argument("--config",
                            type=argparse.FileType("r"),
                            help=(
                                "a filename containing a YAML dict of values "
                                "for the current service_name"))

    sub_parser.add_argument("service_options",
                            nargs="*",
                            help="""name=value for option to set""")

    return sub_parser


def command(options):
    """Set service options.

    Service charms may define dynamic options which may be tweaked
    at deployment time, or over the lifetime of the service.  This
    command allows changing these settings.

    $ juju set <service_name> option=value [option=value]

    or

    $ juju set <service_name> --config local.yaml

    """
    environment = get_environment(options)

    if options.config:
        if options.service_options:
            raise ServiceConfigValueError(
                "--config and command line options cannot "
                "be used in a single invocation")

        yaml_data = options.config.read()
        try:
            data = yaml.load(yaml_data)
        except yaml.YAMLError:
            raise ServiceConfigValueError(
                "Config file %r not valid YAML" % options.config.name)

        if not data or not isinstance(data, dict):
            raise ServiceConfigValueError(
                "Config file %r invalid" % options.config.name
            )
        data = data.get(options.service_name)

        if data:
            # set data directly
            options.service_options = data

    return config_set(environment,
                      options.service_name,
                      options.service_options)


@inlineCallbacks
def config_set(environment, service_name, service_options):
    """Set service settings.
    """
    provider = environment.get_machine_provider()
    client = yield provider.connect()

    # Get the service and the charm
    service_manager = ServiceStateManager(client)
    service = yield service_manager.get_service_state(service_name)
    charm = yield service.get_charm_state()

    # Use the charm's ConfigOptions instance to validate the
    # arguments to config_set. Invalid options passed to this method
    # will thrown an exception.
    if isinstance(service_options, dict):
        options = service_options
    else:
        options = parse_keyvalue_pairs(service_options)

    config = yield charm.get_config()
    # ignore the output of validate, we run it so it might throw an exception
    config.validate(options)

    # Apply the change
    state = yield service.get_config()
    state.update(options)
    yield state.write()
