from twisted.internet.defer import inlineCallbacks

from juju.control import legacy
from juju.control.utils import expand_constraints, get_environment


def configure_subparser(subparsers):
    """Configure bootstrap subcommand"""
    sub_parser = subparsers.add_parser("bootstrap", help=command.__doc__)
    sub_parser.add_argument(
        "--environment", "-e",
        help="juju environment to operate in.")
    sub_parser.add_argument(
        "--constraints",
        help="default hardware constraints for this environment.",
        default=[],
        type=expand_constraints)
    return sub_parser


@inlineCallbacks
def command(options):
    """
    Bootstrap machine providers in the specified environment.
    """
    environment = get_environment(options)
    provider = environment.get_machine_provider()
    legacy_keys = provider.get_legacy_config_keys()
    if legacy_keys:
        legacy.error(legacy_keys)

    constraint_set = yield provider.get_constraint_set()
    constraints = constraint_set.parse(options.constraints)
    constraints = constraints.with_series(environment.default_series)

    options.log.info(
        "Bootstrapping environment %r (origin: %s type: %s)..." % (
        environment.name, environment.origin, environment.type))
    yield provider.bootstrap(constraints)
