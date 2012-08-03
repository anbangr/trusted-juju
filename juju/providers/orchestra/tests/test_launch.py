from twisted.internet.defer import succeed, fail

from juju.errors import EnvironmentNotFound, ProviderError
from juju.lib.testing import TestCase
from juju.providers.orchestra.machine import OrchestraMachine
from juju.providers.orchestra.tests.common import OrchestraTestMixin


class SomeError(Exception):
    pass


class LaunchMachineTest(TestCase, OrchestraTestMixin):

    def test_no_machine_id(self):
        d = self.get_provider().start_machine({})
        self.assertFailure(d, ProviderError)

        def verify(error):
            self.assertEquals(
                str(error),
                "Cannot launch a machine without specifying a machine-id")
        d.addCallback(verify)
        return d

    def test_no_constraints(self):
        d = self.get_provider().start_machine({"machine-id": "1"})
        self.assertFailure(d, ProviderError)

        def verify(error):
            self.assertEquals(
                str(error),
                "Cannot launch a machine without specifying constraints")
        d.addCallback(verify)
        return d

    def test_no_zookeeper(self):
        self.setup_mocks()
        self.mock_find_zookeepers()
        self.mocker.replay()

        d = self.get_provider().start_machine({
            "machine-id": "12345", "constraints": {}})
        self.assertFailure(d, EnvironmentNotFound)
        return d

    def test_no_available_machines(self):
        self.setup_mocks()
        self.mock_find_zookeepers(("jennifer-uid", "jennifer"))
        self.mock_get_systems(acceptable=False, unacceptable=False)
        self.mocker.replay()

        constraints = {"orchestra-classes": None, "ubuntu-series": "blah"}
        d = self.get_provider().start_machine({
            "machine-id": "32767", "constraints": constraints})
        self.assertFailure(d, ProviderError)

        def verify(error):
            self.assertEquals(
                str(error),
                "Could not find a suitable Cobbler system (set to netboot, "
                "and a member of the following management classes: available)")
        d.addCallback(verify)
        return d

    def test_no_acceptable_machines(self):
        self.setup_mocks()
        self.mock_find_zookeepers(("jennifer-uid", "jennifer"))
        self.mock_get_systems(
            acceptable=False, mgmt_classes="available ping pong")
        self.mocker.replay()

        constraints = {
            "orchestra-classes": ["ping", "pong"], "ubuntu-series": "blah"}
        d = self.get_provider().start_machine({
            "machine-id": "32767", "constraints": constraints})
        self.assertFailure(d, ProviderError)

        def verify(error):
            self.assertEquals(
                str(error),
                "All available Cobbler systems were also marked as acquired "
                "(instances: bad-system-uid).")
        d.addCallback(verify)
        return d

    def test_actually_launch(self):
        self.setup_mocks()
        self.mock_find_zookeepers(("jennifer-uid", "jennifer"))
        self.mock_get_systems()
        self.mock_acquire_system()
        self.mock_start_system(
            self.get_verify_ks_meta(42, "launch_user_data"))
        self.mock_describe_systems(succeed([{
            "uid": "winston-uid",
            "name": "winston",
            "mgmt_classes": ["acquired"],
            "netboot_enabled": True}]))
        self.mocker.replay()

        def verify_machines(machines):
            (machine,) = machines
            self.assertTrue(isinstance(machine, OrchestraMachine))
            self.assertEquals(machine.instance_id, "winston-uid")
            self.assertEquals(machine.dns_name, "winston")
        constraints = {"orchestra-classes": None, "ubuntu-series": "splendid"}
        d = self.get_provider().start_machine({
            "machine-id": "42", "constraints": constraints})
        d.addCallback(verify_machines)
        return d

    def test_launch_error_rollback(self):
        """
        Check that a failure to launch the machine attempts to roll back
        cobbler state such that the machine is not stuck in an "acquired"
        state.
        """
        # Note, rollback may fail -- who knows why we got the first error --
        # but I don't think there's a huge amount of value to verifying every
        # single way it could fail. We log the initial error; should be enough.
        self.setup_mocks()
        self.mock_find_zookeepers(("jennifer-uid", "jennifer"))
        self.mock_get_systems()
        self.mock_acquire_system()
        self.mock_start_system(
            self.get_verify_ks_meta(42, "launch_user_data"))
        self.mock_describe_systems(fail(SomeError("pow!")))

        self.proxy_m.callRemote("get_systems")
        self.mocker.result(succeed([{
            "uid": "winston-uid",
            "ks_meta": {
                "MACHINE_ID": "blah",
                "USER_DATA_BASE64": "userdata",
                "KEEP": "keep"},
            "mgmt_classes": ["acquired", "PRESERVE"]}]))
        self.proxy_m.callRemote("find_system", {"uid": "winston-uid"})
        self.mocker.result(succeed(["winston"]))
        self.proxy_m.callRemote("get_system_handle", "winston", "TOKEN")
        self.mocker.result(succeed("some-handle"))
        self.proxy_m.callRemote(
            "modify_system", "some-handle", "ks_meta", {"KEEP": "keep"},
            "TOKEN")
        self.mocker.result(succeed(True))
        self.proxy_m.callRemote(
            "modify_system", "some-handle",
            "mgmt_classes", ["available", "PRESERVE"], "TOKEN")
        self.mocker.result(succeed(True))
        self.proxy_m.callRemote(
            "modify_system", "some-handle", "netboot_enabled", True, "TOKEN")
        self.mocker.result(succeed(True))
        self.proxy_m.callRemote("save_system", "some-handle", "TOKEN")
        self.mocker.result(succeed(True))
        self.proxy_m.callRemote(
            "background_power_system",
            {"power": "off", "systems": ["winston"]}, "TOKEN")
        self.mocker.result(succeed("ignored"))
        self.mocker.replay()

        constraints = {"orchestra-classes": None, "ubuntu-series": "splendid"}
        d = self.get_provider().start_machine({
            "machine-id": "42", "constraints": constraints})
        return self.assertFailure(d, SomeError)
