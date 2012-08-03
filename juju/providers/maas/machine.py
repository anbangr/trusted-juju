# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Juju machine provider for MAAS."""

from juju.machine import ProviderMachine


class MAASMachine(ProviderMachine):
    """MAAS-specific provider machine implementation."""

    @classmethod
    def from_dict(cls, d):
        """Convert a `dict` into a :class:`MAASMachine`.

        :param dict d: a dict as returned (in a list) by
        :meth:`juju.providers.maas.maas.MAASClient.start_node`
        :rtype: :class:`MAASMachine`
        """
        resource_uri, hostname = d["resource_uri"], d["hostname"]
        return cls(
            instance_id=resource_uri, dns_name=hostname,
            private_dns_name=hostname)
