import logging
import sys

from twisted.internet.defer import inlineCallbacks, returnValue

from juju.providers.common.launch import LaunchMachine

from .machine import machine_from_dict

log = logging.getLogger("juju.orchestra")


class OrchestraLaunchMachine(LaunchMachine):
    """Orchestra operation for launching an instance"""

    @inlineCallbacks
    def start_machine(self, machine_id, zookeepers):
        """Actually launch an instance with Orchestra.

        :param str machine_id: the juju machine ID to assign

        :param zookeepers: the machines currently running zookeeper, to which
            the new machine will need to connect
        :type zookeepers: list of
            :class:`juju.providers.orchestra.machine.OrchestraMachine`

        :return: a singe-entry list containing a
            :class:`juju.providers.orchestra.machine.OrchestraMachine`
            representing the newly-launched machine
        :rtype: :class:`twisted.internet.defer.Deferred`
        """
        cobbler = self._provider.cobbler
        series = self._constraints["ubuntu-series"]
        mgmt_classes = self._constraints["orchestra-classes"]
        instance_id = yield cobbler.acquire_system(mgmt_classes)
        # If anything goes wrong after the acquire and before the launch
        # actually happens, we attempt to roll back by calling shutdown_system.
        # This is not guaranteed to work, ofc, but it's the best effort we can
        # make towards avoiding lp:894362, in which a machine gets stuck in an
        # 'acquired' state and cannot be reused without manual intervention.
        try:
            cloud_init = self._create_cloud_init(machine_id, zookeepers)
            cloud_init.set_provider_type("orchestra")
            cloud_init.set_instance_id_accessor(instance_id)

            info = yield cobbler.start_system(
                instance_id, machine_id, series, cloud_init.render())
            returnValue([machine_from_dict(info)])
        except Exception:
            log.exception(
                "Failed to launch machine %s; attempting to revert.",
                instance_id)
            exc_info = sys.exc_info()
            yield cobbler.shutdown_system(instance_id)
            # Use three-expression form to ensure that the error with its
            # traceback is correctly propagated.
            raise exc_info[0], exc_info[1], exc_info[2]
