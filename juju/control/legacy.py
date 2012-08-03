from twisted.internet.defer import inlineCallbacks

from juju.errors import JujuError
from juju.state.environment import EnvironmentStateManager

_ERROR = """
Your environments.yaml contains deprecated keys; they must not be used other
than in legacy deployments. The affected keys are:

    %s

This error can be resolved according to the instructions available at:

    https://juju.ubuntu.com/DeprecatedEnvironmentSettings
"""


def error(keys):
    raise JujuError(_ERROR % "\n    ".join(sorted(keys)))


@inlineCallbacks
def check_environment(client, keys):
    if not keys:
        return
    esm = EnvironmentStateManager(client)
    if not (yield esm.get_in_legacy_environment()):
        error(keys)


@inlineCallbacks
def check_constraints(client, constraint_strs):
    if not constraint_strs:
        return
    esm = EnvironmentStateManager(client)
    if (yield esm.get_in_legacy_environment()):
        raise JujuError(
            "Constraints are not valid in legacy deployments. To use machine "
            "constraints, please deploy your environment again from scratch. "
            "You can continue to use this environment as before, but any "
            "attempt to set constraints will fail.")
