"""
Service Unit Deployment unit tests
"""
import logging
import os
import subprocess

from twisted.internet.defer import inlineCallbacks, succeed

from juju.charm import get_charm_from_path
from juju.charm.tests.test_repository import RepositoryTestBase
from juju.lib.lxc import LXCContainer
from juju.lib.lxc.tests.test_lxc import uses_sudo
from juju.lib.mocker import ANY, KWARGS
from juju.lib.upstart import UpstartService
from juju.machine.unit import UnitMachineDeployment, UnitContainerDeployment
from juju.machine.errors import UnitDeploymentError
from juju.tests.common import get_test_zookeeper_address


class UnitMachineDeploymentTest(RepositoryTestBase):

    def setUp(self):
        super(UnitMachineDeploymentTest, self).setUp()
        self.charm = get_charm_from_path(self.sample_dir1)
        self.bundle = self.charm.as_bundle()
        self.juju_directory = self.makeDir()
        self.units_directory = os.path.join(self.juju_directory, "units")
        os.mkdir(self.units_directory)
        self.unit_name = "wordpress/0"
        self.rootfs = self.makeDir()
        self.init_dir = os.path.join(self.rootfs, "etc", "init")
        os.makedirs(self.init_dir)
        self.real_init_dir = self.patch(
            UpstartService, "init_dir", self.init_dir)

        self.deployment = UnitMachineDeployment(
            self.unit_name,
            self.juju_directory)

        self.assertEqual(
            self.deployment.unit_agent_module, "juju.agents.unit")
        self.deployment.unit_agent_module = "juju.agents.dummy"

    def setup_mock(self):
        self.check_call = self.mocker.replace("subprocess.check_call")
        self.getProcessOutput = self.mocker.replace(
            "twisted.internet.utils.getProcessOutput")

    def mock_is_running(self, running):
        self.getProcessOutput("/sbin/status", ["juju-wordpress-0"])
        if running:
            self.mocker.result(succeed(
                "juju-wordpress-0 start/running, process 12345"))
        else:
            self.mocker.result(succeed("juju-wordpress-0 stop/waiting"))

    def _without_sudo(self, args, **_):
        self.assertEquals(args[0], "sudo")
        return subprocess.call(args[1:])

    def mock_install(self):
        self.check_call(ANY, KWARGS)  # cp to init dir
        self.mocker.call(self._without_sudo)
        self.check_call(ANY, KWARGS)  # chmod 644
        self.mocker.call(self._without_sudo)

    def mock_start(self):
        self.check_call(("sudo", "/sbin/start", "juju-wordpress-0"), KWARGS)
        self.mocker.result(0)
        for _ in range(5):
            self.mock_is_running(True)

    def mock_destroy(self):
        self.check_call(("sudo", "/sbin/stop", "juju-wordpress-0"), KWARGS)
        self.mocker.result(0)
        self.check_call(ANY, KWARGS)  # rm from init dir
        self.mocker.call(self._without_sudo)

    def assert_pid_running(self, pid, expect):
        self.assertEquals(os.path.exists("/proc/%s" % pid), expect)

    def test_unit_name_with_path_manipulation_raises_assertion(self):
        self.assertRaises(
            AssertionError,
            UnitMachineDeployment,
            "../../etc/password/zebra/0",
            self.units_directory)

    def test_unit_directory(self):
        self.assertEqual(
            self.deployment.directory,
            os.path.join(self.units_directory,
                         self.unit_name.replace("/", "-")))

    def test_service_unit_start(self):
        """
        Starting a service unit will result in a unit workspace being created
        if it does not exist and a running service unit agent.
        """
        self.setup_mock()
        self.mock_install()
        self.mock_is_running(False)
        self.mock_start()
        self.mocker.replay()

        d = self.deployment.start(
            "123", get_test_zookeeper_address(), self.bundle)

        def verify_upstart(_):
            conf_path = os.path.join(self.init_dir, "juju-wordpress-0.conf")
            with open(conf_path) as f:
                lines = f.readlines()

            env = []
            for line in lines:
                if line.startswith("env "):
                    env.append(line[4:-1].split("=", 1))
                if line.startswith("exec "):
                    exec_ = line[5:-1]

            env = dict((k, v.strip('"')) for (k, v) in env)
            env.pop("PYTHONPATH")
            self.assertEquals(env, {
                "JUJU_HOME": self.juju_directory,
                "JUJU_UNIT_NAME": self.unit_name,
                "JUJU_ZOOKEEPER": get_test_zookeeper_address(),
                "JUJU_MACHINE_ID": "123"})

            log_file = os.path.join(
                self.deployment.directory, "charm.log")
            command = " ".join([
                "/usr/bin/python", "-m", "juju.agents.dummy", "--nodaemon",
                "--logfile", log_file, "--session-file",
                "/var/run/juju/unit-wordpress-0-agent.zksession",
                ">> /tmp/juju-wordpress-0.output 2>&1"])
            self.assertEquals(exec_, command)
        d.addCallback(verify_upstart)
        return d

    @inlineCallbacks
    def test_service_unit_destroy(self):
        """
        Forcibly stop a unit, and destroy any directories associated to it
        on the machine, and kills the unit agent process.
        """
        self.setup_mock()
        self.mock_install()
        self.mock_is_running(False)
        self.mock_start()
        self.mock_is_running(True)
        self.mock_destroy()
        self.mocker.replay()

        yield self.deployment.start(
            "0", get_test_zookeeper_address(), self.bundle)

        yield self.deployment.destroy()
        self.assertFalse(os.path.exists(self.deployment.directory))

        conf_path = os.path.join(self.init_dir, "juju-wordpress-0.conf")
        self.assertFalse(os.path.exists(conf_path))

    @inlineCallbacks
    def test_service_unit_destroy_undeployed(self):
        """
        If the unit is has not been deployed, nothing happens.
        """
        yield self.deployment.destroy()
        self.assertFalse(os.path.exists(self.deployment.directory))

    @inlineCallbacks
    def test_service_unit_destroy_not_running(self):
        """
        If the unit is not running, then destroy will just remove
        its directory.
        """
        self.setup_mock()
        self.mock_install()
        self.mock_is_running(False)
        self.mock_start()
        self.mock_is_running(False)
        self.check_call(ANY, KWARGS)  # rm from init dir
        self.mocker.call(self._without_sudo)
        self.mocker.replay()

        yield self.deployment.start(
            "0", get_test_zookeeper_address(), self.bundle)
        yield self.deployment.destroy()
        self.assertFalse(os.path.exists(self.deployment.directory))

        conf_path = os.path.join(self.init_dir, "juju-wordpress-0.conf")
        self.assertFalse(os.path.exists(conf_path))

    def test_unpack_charm(self):
        """
        The deployment unpacks a charm bundle into the unit workspace.
        """
        self.deployment.unpack_charm(self.bundle)
        unit_path = os.path.join(
            self.units_directory, self.unit_name.replace("/", "-"))

        self.assertTrue(os.path.exists(unit_path))
        charm_path = os.path.join(unit_path, "charm")
        self.assertTrue(os.path.exists(charm_path))
        charm = get_charm_from_path(charm_path)
        self.assertEqual(
            charm.get_revision(), self.charm.get_revision())

    def test_unpack_charm_exception_invalid_charm(self):
        """
        If the charm bundle is corrupted or invalid a deployment specific
        error is raised.
        """
        error = self.assertRaises(
            UnitDeploymentError,
            self.deployment.unpack_charm, self.charm)
        self.assertEquals(
            str(error),
            "Invalid charm for deployment: %s" % self.charm.path)

    @inlineCallbacks
    def test_is_running_not_installed(self):
        """
        If there is no conf file the service unit is not running.
        """
        self.assertEqual((yield self.deployment.is_running()), False)

    @inlineCallbacks
    def test_is_running_not_running(self):
        """
        If the conf file exists, but job not running, unit not running
        """
        conf_path = os.path.join(self.init_dir, "juju-wordpress-0.conf")
        with open(conf_path, "w") as f:
            f.write("blah")
        self.setup_mock()
        self.mock_is_running(False)
        self.mocker.replay()
        self.assertEqual((yield self.deployment.is_running()), False)

    @inlineCallbacks
    def test_is_running_success(self):
        """
        Check running job.
        """
        conf_path = os.path.join(self.init_dir, "juju-wordpress-0.conf")
        with open(conf_path, "w") as f:
            f.write("blah")
        self.setup_mock()
        self.mock_is_running(True)
        self.mocker.replay()
        self.assertEqual((yield self.deployment.is_running()), True)

    @uses_sudo
    @inlineCallbacks
    def test_run_actual_process(self):
        # "unpatch" to use real /etc/init
        self.patch(UpstartService, "init_dir", self.real_init_dir)
        yield self.deployment.start(
            "0", get_test_zookeeper_address(), self.bundle)
        old_pid = yield self.deployment.get_pid()
        self.assert_pid_running(old_pid, True)

        # Give the job a chance to fall over and be restarted (if the
        # pid doesn't change, that hasn't hapened)
        yield self.sleep(0.1)
        self.assertEquals((yield self.deployment.get_pid()), old_pid)
        self.assert_pid_running(old_pid, True)

        # Kick the job over ourselves; check it comes back
        os.system("sudo kill -9 %s" % old_pid)
        yield self.sleep(0.1)
        self.assert_pid_running(old_pid, False)
        new_pid = yield self.deployment.get_pid()
        self.assertNotEquals(new_pid, old_pid)
        self.assert_pid_running(new_pid, True)

        yield self.deployment.destroy()
        self.assertEquals((yield self.deployment.get_pid()), None)
        self.assert_pid_running(new_pid, False)

    @uses_sudo
    @inlineCallbacks
    def test_fail_to_run_actual_process(self):
        self.deployment.unit_agent_module = "haha.disregard.that"
        self.patch(UpstartService, "init_dir", self.real_init_dir)

        d = self.deployment.start(
            "0", get_test_zookeeper_address(), self.bundle)
        e = yield self.assertFailure(d, UnitDeploymentError)
        self.assertTrue(str(e).startswith(
            "Failed to start job juju-wordpress-0; got output:\n"))
        self.assertIn("No module named haha", str(e))

        yield self.deployment.destroy()


class UnitContainerDeploymentTest(RepositoryTestBase):

    unit_name = "riak/0"

    @inlineCallbacks
    def setUp(self):
        yield super(UnitContainerDeploymentTest, self).setUp()
        self.juju_home = self.makeDir()
        # Setup unit namespace
        environ = dict(os.environ)
        environ["JUJU_UNIT_NS"] = "ns1"
        environ["JUJU_SERIES"] = "precise"

        self.change_environment(**environ)

        self.unit_deploy = UnitContainerDeployment(
            self.unit_name, self.juju_home)
        self.charm = get_charm_from_path(self.sample_dir1)
        self.bundle = self.charm.as_bundle()
        self.output = self.capture_logging("unit.deploy", level=logging.DEBUG)

    def get_normalized(self, output):
        # strip python path for comparison
        return "\n".join(filter(None, output.split("\n"))[:-2])

    def test_get_container_name(self):
        self.assertEqual(
            "ns1-riak-0",
            self.unit_deploy.container_name)

    @inlineCallbacks
    def test_destroy(self):
        mock_container = self.mocker.patch(self.unit_deploy.container)
        mock_container.destroy()
        self.mocker.replay()

        yield self.unit_deploy.destroy()

        output = self.output.getvalue()
        self.assertIn("Destroying container...", output)
        self.assertIn("Destroyed container for riak/0", output)

    @inlineCallbacks
    def test_origin_usage(self):
        """The machine agent is started with a origin environment variable
        """
        mock_container = self.mocker.patch(LXCContainer)
        mock_container.is_constructed()
        self.mocker.result(True)
        mock_container.is_constructed()
        self.mocker.result(True)
        self.mocker.replay()

        environ = dict(os.environ)
        environ["JUJU_ORIGIN"] = "lp:~juju/foobar"

        self.change_environment(**environ)
        unit_deploy = UnitContainerDeployment(
            self.unit_name, self.juju_home)
        container = yield unit_deploy._get_master_template(
            "local", "abc")
        self.assertEqual(container.origin, "lp:~juju/foobar")
        self.assertEqual(
            container.customize_log,
            os.path.join(self.juju_home, "units", "master-customize.log"))

    @inlineCallbacks
    def test_start(self):
        container = LXCContainer(self.unit_name, None, "precise", None)
        rootfs = self.makeDir()
        env = dict(os.environ)
        env["JUJU_PUBLIC_KEY"] = "dsa ..."
        self.change_environment(**env)

        mock_deploy = self.mocker.patch(self.unit_deploy)
        # this minimally validates that we are also called with the
        # expect public key
        mock_deploy._get_container(ANY, ANY, env["JUJU_PUBLIC_KEY"])
        self.mocker.result((container, rootfs))

        mock_container = self.mocker.patch(container)
        mock_container.run()

        self.mocker.replay()

        self.unit_deploy.directory = rootfs
        os.makedirs(os.path.join(rootfs, "etc", "init"))

        yield self.unit_deploy.start("0", "127.0.1.1:2181", self.bundle)

        # Verify the upstart job
        upstart_agent_name = "juju-%s.conf" % (
            self.unit_name.replace("/", "-"))
        content = open(
            os.path.join(rootfs, "etc", "init", upstart_agent_name)).read()
        job = self.get_normalized(content)
        self.assertIn('JUJU_ZOOKEEPER="127.0.1.1:2181"', job)
        self.assertIn('JUJU_MACHINE_ID="0"', job)
        self.assertIn('JUJU_UNIT_NAME="riak/0"', job)

        # Verify the symlinks exist
        self.assertTrue(os.path.lexists(os.path.join(
            self.unit_deploy.juju_home, "units",
            self.unit_deploy.unit_path_name, "unit.log")))
        self.assertTrue(os.path.lexists(os.path.join(
            self.unit_deploy.juju_home, "units",
            self.unit_deploy.unit_path_name, "output.log")))

        # Verify the charm is on disk.
        self.assertTrue(os.path.exists(os.path.join(
            self.unit_deploy.directory, "var", "lib", "juju", "units",
            self.unit_deploy.unit_path_name, "charm", "metadata.yaml")))

        # Verify the directory structure in the unit.
        self.assertTrue(os.path.exists(os.path.join(
            self.unit_deploy.directory, "var", "lib", "juju", "state")))
        self.assertTrue(os.path.exists(os.path.join(
            self.unit_deploy.directory, "var", "log", "juju")))

        # Verify log output
        output = self.output.getvalue()
        self.assertIn("Charm extracted into container", output)
        self.assertIn("Started container for %s" % self.unit_deploy.unit_name,
                      output)

    @inlineCallbacks
    def test_get_container(self):
        rootfs = self.makeDir()
        container = LXCContainer(self.unit_name, None, None, None)

        mock_deploy = self.mocker.patch(self.unit_deploy)
        mock_deploy._get_master_template(ANY, ANY)
        self.mocker.result(container)

        mock_container = self.mocker.patch(container)
        mock_container.clone(ANY)
        self.mocker.result(container)

        self.mocker.replay()

        container, rootfs = yield self.unit_deploy._get_container(
            "0", None, "dsa...")

        output = self.output.getvalue()
        self.assertIn("Container created for %s" % self.unit_deploy.unit_name,
                      output)
