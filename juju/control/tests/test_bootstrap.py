
from twisted.internet.defer import inlineCallbacks, succeed
from yaml import dump

from juju.providers.dummy import MachineProvider
from juju.control import main

from .common import ControlToolTest


class ControlBootstrapTest(ControlToolTest):

    @inlineCallbacks
    def test_bootstrap(self):
        """
        'juju-control bootstrap' will invoke the bootstrap method of all
        configured machine providers in all environments.
        """
        config = {
            "environments": {
                "firstenv": {
                    "type": "dummy", "default-series": "homer"},
                "secondenv": {
                    "type": "dummy", "default-series": "marge"}}}

        self.write_config(dump(config))
        finished = self.setup_cli_reactor()
        self.setup_exit(0)

        provider = self.mocker.patch(MachineProvider)
        provider.bootstrap({
            "ubuntu-series": "homer",
            "provider-type": "dummy",
            "arch": "arm",
            "cpu": 2.0,
            "mem": 512.0})
        self.mocker.result(succeed(True))
        self.mocker.replay()

        self.capture_stream("stderr")
        main(["bootstrap", "-e", "firstenv",
              "--constraints", "arch=arm cpu=2"])
        yield finished

        lines = filter(None, self.log.getvalue().split("\n"))
        self.assertEqual(
            lines,
            [("Bootstrapping environment 'firstenv' "
             "(origin: distro type: dummy)..."),
             "'bootstrap' command finished successfully"])

    @inlineCallbacks
    def test_bootstrap_multiple_environments_no_default_specified(self):
        """
        If there are multiple environments but no default, and no environment
        specified on the cli, then an error message is given.
        """
        config = {
            "environments": {
                "firstenv": {
                    "type": "dummy", "admin-secret": "homer"},
                "secondenv": {
                    "type": "dummy", "admin-secret": "marge"}}}
        self.write_config(dump(config))
        finished = self.setup_cli_reactor()
        self.setup_exit(1)
        self.mocker.replay()

        output = self.capture_stream("stderr")
        main(["bootstrap"])
        yield finished
        msg = "There are multiple environments and no explicit default"
        self.assertIn(msg, self.log.getvalue())
        self.assertIn(msg, output.getvalue())

    @inlineCallbacks
    def test_bootstrap_invalid_environment_specified(self):
        """
        If the environment specified does not exist an error message is given.
        """
        config = {
            "environments": {
                "firstenv": {
                    "type": "dummy", "admin-secret": "homer"}}}
        self.write_config(dump(config))
        finished = self.setup_cli_reactor()
        self.setup_exit(1)
        self.mocker.replay()

        output = self.capture_stream("stderr")
        main(["bootstrap", "-e", "thirdenv"])
        yield finished

        msg = "Invalid environment 'thirdenv'"
        self.assertIn(msg, self.log.getvalue())
        self.assertIn(msg, output.getvalue())

    @inlineCallbacks
    def test_bootstrap_legacy_config_keys(self):
        """
        If the environment specified does not exist an error message is given.
        """
        config = {
            "environments": {
                "firstenv": {
                    "type": "dummy", "some-legacy-key": "blah"}}}
        self.write_config(dump(config))
        finished = self.setup_cli_reactor()
        self.setup_exit(1)
        self.mocker.replay()

        output = self.capture_stream("stderr")
        main(["bootstrap"])
        yield finished

        msg = "Your environments.yaml contains deprecated keys"
        self.assertIn(msg, self.log.getvalue())
        self.assertIn(msg, output.getvalue())
