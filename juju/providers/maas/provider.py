# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Juju provider to connect to a MAAS server."""

import logging
from twisted.internet.defer import inlineCallbacks, returnValue, succeed

from juju.errors import MachinesNotFound, ProviderError
from juju.providers.common.base import MachineProviderBase
from juju.providers.maas.files import MAASFileStorage
from juju.providers.maas.launch import MAASLaunchMachine
from juju.providers.maas.maas import MAASClient
from juju.providers.maas.machine import MAASMachine


log = logging.getLogger("juju.maas")


class MachineProvider(MachineProviderBase):
    """MachineProvider for use in a MAAS environment"""

    def __init__(self, environment_name, config):
        super(MachineProvider, self).__init__(environment_name, config)
        self.maas_client = MAASClient(config)
        self._storage = MAASFileStorage(config)

    @property
    def provider_type(self):
        return "maas"

    def get_file_storage(self):
        """Return a WebDAV-backed FileStorage abstraction."""
        return self._storage

    @inlineCallbacks
    def get_constraint_set(self):
        """Return the set of constraints that are valid for this provider."""
        cs = yield super(MachineProvider, self).get_constraint_set()

        def require_precise(s):
            if s != "precise":
                raise ValueError(
                     "MAAS currently only provisions machines running precise")
            return s

        cs.register("ubuntu-series", converter=require_precise, visible=False)
        cs.register("maas-name")
        returnValue(cs)

    def get_serialization_data(self):
        """Get provider configuration suitable for serialization.

        We're overriding the base method so that we can deal with the
        maas-oauth data in a special way because when the environment file
        is parsed it writes the data in the config object as a list of
        its token parts.
        """
        data = super(MachineProvider, self).get_serialization_data()
        data["maas-oauth"] = ":".join(self.config["maas-oauth"])
        return data

    def start_machine(self, machine_data, master=False):
        """Start a MAAS machine.

        :param dict machine_data: desired characteristics of the new machine;
            it must include a "machine-id" key, and may include a "constraints"
            key (which is currently ignored by this provider).

        :param bool master: if True, machine will initialize the juju admin
            and run a provisioning agent, in addition to running a machine
            agent.
        """
        return MAASLaunchMachine.launch(self, machine_data, master)

    @inlineCallbacks
    def get_machines(self, instance_ids=()):
        """List machines running in the provider.

        :param list instance_ids: ids of instances you want to get. Leave empty
            to list every
            :class:`juju.providers.maas.MAASMachine`
            owned by this provider.

        :return: a list of
            :class:`juju.providers.maas.MAASMachine`
        :rtype: :class:`twisted.internet.defer.Deferred`

        :raises: :exc:`juju.errors.MachinesNotFound`
        """
        instances = yield self.maas_client.get_nodes(instance_ids)
        machines = [MAASMachine.from_dict(i) for i in instances]
        if instance_ids:
            instance_ids_expected = set(instance_ids)
            instance_ids_returned = set(
                machine.instance_id for machine in machines)
            instance_ids_missing = (
                instance_ids_expected - instance_ids_returned)
            instance_ids_unexpected = (
                instance_ids_returned - instance_ids_expected)
            if instance_ids_missing:
                raise MachinesNotFound(sorted(instance_ids_missing))
            if instance_ids_unexpected:
                raise ProviderError(
                    "Machines not requested returned: %s" % (
                        ", ".join(sorted(instance_ids_unexpected))))
        returnValue(machines)

    @inlineCallbacks
    def shutdown_machines(self, machines):
        """Terminate machines associated with this provider.

        :param machines: machines to shut down
        :type machines: list of
            :class:`juju.providers.maas.MAASMachine`

        :return: list of terminated
            :class:`juju.providers.maas.MAASMachine`
            instances
        :rtype: :class:`twisted.internet.defer.Deferred`
        """
        if not machines:
            returnValue([])

        for machine in machines:
            if not isinstance(machine, MAASMachine):
                raise ProviderError(
                    "Can only shut down MAASMachines; "
                    "got a %r" % type(machine))

        ids = [m.instance_id for m in machines]
        killable_machines = yield self.get_machines(ids)
        for machine in killable_machines:
            yield self.maas_client.stop_node(machine.instance_id)
            yield self.maas_client.release_node(machine.instance_id)
        returnValue(killable_machines)

    def open_port(self, machine, machine_id, port, protocol="tcp"):
        """Authorizes `port` using `protocol` for `machine`."""
        log.warn("Firewalling is not yet implemented")
        return succeed(None)

    def close_port(self, machine, machine_id, port, protocol="tcp"):
        """Revokes `port` using `protocol` for `machine`."""
        log.warn("Firewalling is not yet implemented")
        return succeed(None)

    def get_opened_ports(self, machine, machine_id):
        """Returns a set of open (port, protocol) pairs for `machine`."""
        log.warn("Firewalling is not yet implemented")
        return succeed(set())
