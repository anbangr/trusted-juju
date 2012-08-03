# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Machine Launcher for MAAS."""

import logging
import sys
from twisted.internet.defer import (
    inlineCallbacks,
    returnValue,
    )

from juju.providers.common.launch import LaunchMachine
from juju.providers.maas.machine import MAASMachine


log = logging.getLogger("juju.maas")


class MAASLaunchMachine(LaunchMachine):
    """MAAS operation for launching an instance."""

    @inlineCallbacks
    def start_machine(self, machine_id, zookeepers):
        """Start an instance with MAAS.

        :param str machine_id: the juju machine ID to assign
        :param zookeepers: the machines currently running zookeeper, to which
            the new machine will need to connect
        :type zookeepers: list of
            :class:`juju.providers.maas.provider.MAASMachine`

        :return: a singe-entry list containing a
            :class:`juju.providers.maas.provider.MAASMachine`
            representing the newly-launched machine
        :rtype: :class:`twisted.internet.defer.Deferred`
        """
        maas_client = self._provider.maas_client
        instance_data = yield maas_client.acquire_node(self._constraints)
        instance_uri = instance_data["resource_uri"]
        # If anything goes wrong after the acquire and before the launch
        # actually happens, we attempt to release the node.
        try:
            cloud_init = self._create_cloud_init(machine_id, zookeepers)
            cloud_init.set_provider_type("maas")
            cloud_init.set_instance_id_accessor(instance_uri)
            node_data = yield maas_client.start_node(
                instance_uri, cloud_init.render())
            machine = MAASMachine.from_dict(node_data)
        except Exception:
            log.exception(
                "Failed to launch machine %s; attempting to release.",
                instance_uri)
            exc_info = sys.exc_info()
            yield maas_client.release_node(instance_uri)
            # Use three-expression form to ensure that the error with its
            # traceback is correctly propagated.
            raise exc_info[0], exc_info[1], exc_info[2]
        else:
            returnValue([machine])
