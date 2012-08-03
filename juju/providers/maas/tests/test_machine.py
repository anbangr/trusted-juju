# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for juju.providers.maas.machine."""

from juju.providers.maas.machine import MAASMachine
from juju.providers.maas.tests.testing import TestCase


class MAASMachineTest(TestCase):
    "Tests for `MAASMachine`."""

    def test_from_dict(self):
        """
        Given a dict of node data from the MAAS API, `from_dict()` returns a
        `MAASMachine` instance representing that machine.
        """
        data = {
            "resource_uri": "/an/example/uri",
            "hostname": "machine.example.com",
            }
        machine = MAASMachine.from_dict(data)
        self.assertEqual("/an/example/uri", machine.instance_id)
        self.assertEqual("machine.example.com", machine.dns_name)
        self.assertEqual("machine.example.com", machine.private_dns_name)
