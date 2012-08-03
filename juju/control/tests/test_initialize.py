from base64 import b64encode
from yaml import safe_dump

from twisted.internet.defer import succeed

from txzookeeper import ZookeeperClient
from juju.state.initialize import StateHierarchy

from juju.control import admin
from .common import ControlToolTest


class AdminInitializeTest(ControlToolTest):

    def test_initialize(self):
        """The admin cli dispatches the initialize method with arguments."""

        client = self.mocker.patch(ZookeeperClient)
        hierarchy = self.mocker.patch(StateHierarchy)

        self.setup_cli_reactor()
        client.connect()
        self.mocker.result(succeed(client))

        hierarchy.initialize()
        self.mocker.result(succeed(True))
        client.close()
        self.capture_stream('stderr')
        self.setup_exit(0)
        self.mocker.replay()

        constraints_data = b64encode(safe_dump({
            "ubuntu-series": "foo", "provider-type": "bar"}))

        admin(["initialize",
               "--instance-id", "foobar",
               "--admin-identity", "admin:genie",
               "--constraints-data", constraints_data,
               "--provider-type", "dummy"])

    def test_bad_constraints_data(self):
        """Test that failing to unpack --constraints-data aborts initialize"""
        client = self.mocker.patch(ZookeeperClient)
        self.setup_cli_reactor()
        client.connect()
        self.mocker.result(succeed(client))
        self.capture_stream('stderr')
        self.setup_exit(1)
        self.mocker.replay()

        admin(["initialize",
               "--instance-id", "foobar",
               "--admin-identity", "admin:genie",
               "--constraints-data", "zaphod's just this guy, you know?",
               "--provider-type", "dummy"])
