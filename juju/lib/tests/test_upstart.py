import os

from twisted.internet.defer import inlineCallbacks, succeed

from juju.errors import ServiceError
from juju.lib.mocker import ANY, KWARGS
from juju.lib.testing import TestCase
from juju.lib.upstart import UpstartService


DATA_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), "data")


class UpstartServiceTest(TestCase):

    @inlineCallbacks
    def setUp(self):
        yield super(UpstartServiceTest, self).setUp()
        self.init_dir = self.makeDir()
        self.conf = os.path.join(self.init_dir, "some-name.conf")
        self.output = "/tmp/some-name.output"
        self.patch(UpstartService, "init_dir", self.init_dir)
        self.service = UpstartService("some-name")

    def setup_service(self):
        self.service.set_description("a wretched hive of scum and villainy")
        self.service.set_command("/bin/imagination-failure --no-ideas")
        self.service.set_environ({"LIGHTSABER": "civilised weapon"})

    def setup_mock(self):
        self.check_call = self.mocker.replace("subprocess.check_call")
        self.getProcessOutput = self.mocker.replace(
            "twisted.internet.utils.getProcessOutput")

    def mock_status(self, result):
        self.getProcessOutput("/sbin/status", ["some-name"])
        self.mocker.result(result)

    def mock_call(self, args, output=None):
        self.check_call(args, KWARGS)
        if output is None:
            self.mocker.result(0)
        else:
            def write(ANY, **_):
                with open(self.output, "w") as f:
                    f.write(output)
            self.mocker.call(write)

    def mock_start(self, output=None):
        self.mock_call(("/sbin/start", "some-name"), output)

    def mock_stop(self):
        self.mock_call(("/sbin/stop", "some-name"))

    def mock_check_success(self):
        for _ in range(5):
            self.mock_status(succeed("blah start/running blah 12345"))

    def mock_check_unstable(self):
        for _ in range(4):
            self.mock_status(succeed("blah start/running blah 12345"))
        self.mock_status(succeed("blah start/running blah 12346"))

    def mock_check_not_running(self):
        self.mock_status(succeed("blah"))

    def write_dummy_conf(self):
        with open(self.conf, "w") as f:
            f.write("dummy")

    def assert_dummy_conf(self):
        with open(self.conf) as f:
            self.assertEquals(f.read(), "dummy")

    def assert_no_conf(self):
        self.assertFalse(os.path.exists(self.conf))

    def assert_conf(self, name="test_standard_install"):
        with open(os.path.join(DATA_DIR, name)) as expected:
            with open(self.conf) as actual:
                self.assertEquals(actual.read(), expected.read())

    def test_is_installed(self):
        """Check is_installed depends on conf file existence"""
        self.assertFalse(self.service.is_installed())
        self.write_dummy_conf()
        self.assertTrue(self.service.is_installed())

    def test_init_dir(self):
        """
        Check is_installed still works when init_dir specified explicitly
        """
        self.patch(UpstartService, "init_dir", "/BAD/PATH")
        self.service = UpstartService("some-name", init_dir=self.init_dir)
        self.setup_service()

        self.assertFalse(self.service.is_installed())
        self.write_dummy_conf()
        self.assertTrue(self.service.is_installed())

    @inlineCallbacks
    def test_is_running(self):
        """
        Check is_running interprets status output (when service is installed)
        """
        self.setup_mock()
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mock_status(succeed("blah blob/gibbering blah"))
        self.mock_status(succeed("blah start/running blah 12345"))
        self.mocker.replay()

        # Won't hit status; conf is not installed
        self.assertFalse((yield self.service.is_running()))
        self.write_dummy_conf()

        # These 3 calls correspond to the first 3 mock_status calls above
        self.assertFalse((yield self.service.is_running()))
        self.assertFalse((yield self.service.is_running()))
        self.assertTrue((yield self.service.is_running()))

    @inlineCallbacks
    def test_is_stable_yes(self):
        self.setup_mock()
        self.mock_check_success()
        self.mocker.replay()

        self.write_dummy_conf()
        self.assertTrue((yield self.service.is_stable()))

    @inlineCallbacks
    def test_is_stable_no(self):
        self.setup_mock()
        self.mock_check_unstable()
        self.mocker.replay()

        self.write_dummy_conf()
        self.assertFalse((yield self.service.is_stable()))

    @inlineCallbacks
    def test_is_stable_not_running(self):
        self.setup_mock()
        self.mock_check_not_running()
        self.mocker.replay()

        self.write_dummy_conf()
        self.assertFalse((yield self.service.is_stable()))

    @inlineCallbacks
    def test_is_stable_not_even_installed(self):
        self.assertFalse((yield self.service.is_stable()))

    @inlineCallbacks
    def test_get_pid(self):
        """
        Check get_pid interprets status output (when service is installed)
        """
        self.setup_mock()
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mock_status(succeed("blah blob/gibbering blah"))
        self.mock_status(succeed("blah start/running blah 12345"))
        self.mocker.replay()

        # Won't hit status; conf is not installed
        self.assertEquals((yield self.service.get_pid()), None)
        self.write_dummy_conf()

        # These 3 calls correspond to the first 3 mock_status calls above
        self.assertEquals((yield self.service.get_pid()), None)
        self.assertEquals((yield self.service.get_pid()), None)
        self.assertEquals((yield self.service.get_pid()), 12345)

    @inlineCallbacks
    def test_basic_install(self):
        """Check a simple UpstartService writes expected conf file"""
        e = yield self.assertFailure(self.service.install(), ServiceError)
        self.assertEquals(str(e), "Cannot render .conf: no description set")
        self.service.set_description("uninteresting service")
        e = yield self.assertFailure(self.service.install(), ServiceError)
        self.assertEquals(str(e), "Cannot render .conf: no command set")
        self.service.set_command("/bin/false")
        yield self.service.install()

        self.assert_conf("test_basic_install")

    @inlineCallbacks
    def test_less_basic_install(self):
        """Check conf for a different UpstartService (which sets an env var)"""
        self.service.set_description("pew pew pew blam")
        self.service.set_command("/bin/deathstar --ignore-ewoks endor")
        self.service.set_environ({"FOO": "bar baz qux", "PEW": "pew"})
        self.service.set_output_path("/somewhere/else")
        yield self.service.install()

        self.assert_conf("test_less_basic_install")

    def test_install_via_script(self):
        """Check that the output-as-script form does the right thing"""
        self.setup_service()
        install, start = self.service.get_cloud_init_commands()

        os.system(install)
        self.assert_conf()
        self.assertEquals(start, "/sbin/start some-name")

    @inlineCallbacks
    def test_start_not_installed(self):
        """Check that .start() also installs if necessary"""
        self.setup_mock()
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mock_start()
        self.mock_check_success()
        self.mocker.replay()

        self.setup_service()
        yield self.service.start()
        self.assert_conf()

    @inlineCallbacks
    def test_start_not_started_stable(self):
        """Check that .start() starts if stopped, and checks for stable pid"""
        self.write_dummy_conf()
        self.setup_mock()
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mock_start("ignored")
        self.mock_check_success()
        self.mocker.replay()

        self.setup_service()
        yield self.service.start()
        self.assert_dummy_conf()

    @inlineCallbacks
    def test_start_not_started_unstable(self):
        """Check that .start() starts if stopped, and raises on unstable pid"""
        self.write_dummy_conf()
        self.setup_mock()
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mock_start("kangaroo")
        self.mock_check_unstable()
        self.mocker.replay()

        self.setup_service()
        e = yield self.assertFailure(self.service.start(), ServiceError)
        self.assertEquals(
            str(e), "Failed to start job some-name; got output:\nkangaroo")
        self.assert_dummy_conf()

    @inlineCallbacks
    def test_start_not_started_failure(self):
        """Check that .start() starts if stopped, and raises on no pid"""
        self.write_dummy_conf()
        self.setup_mock()
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mock_start()
        self.mock_check_not_running()
        self.mocker.replay()

        self.setup_service()
        e = yield self.assertFailure(self.service.start(), ServiceError)
        self.assertEquals(
            str(e), "Failed to start job some-name; no output detected")
        self.assert_dummy_conf()

    @inlineCallbacks
    def test_start_started(self):
        """Check that .start() does nothing if already running"""
        self.write_dummy_conf()
        self.setup_mock()
        self.mock_status(succeed("blah start/running blah 12345"))
        self.mocker.replay()

        self.setup_service()
        yield self.service.start()
        self.assert_dummy_conf()

    @inlineCallbacks
    def test_destroy_not_installed(self):
        """Check .destroy() does nothing if not installed"""
        yield self.service.destroy()
        self.assert_no_conf()

    @inlineCallbacks
    def test_destroy_not_started(self):
        """Check .destroy just deletes conf if not running"""
        self.write_dummy_conf()
        self.setup_mock()
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mocker.replay()

        yield self.service.destroy()
        self.assert_no_conf()

    @inlineCallbacks
    def test_destroy_started(self):
        """Check .destroy() stops running service and deletes conf file"""
        self.write_dummy_conf()
        self.setup_mock()
        self.mock_status(succeed("blah start/running blah 54321"))
        self.mock_stop()
        self.mocker.replay()

        yield self.service.destroy()
        self.assert_no_conf()

    @inlineCallbacks
    def test_use_sudo(self):
        """Check that expected commands are generated when use_sudo is set"""
        self.setup_mock()
        self.service = UpstartService("some-name", use_sudo=True)
        self.setup_service()
        with open(self.output, "w") as f:
            f.write("clear this file out...")

        def verify_cp(args, **kwargs):
            sudo, cp, src, dst = args
            self.assertEquals(sudo, "sudo")
            self.assertEquals(cp, "cp")
            with open(os.path.join(DATA_DIR, "test_standard_install")) as exp:
                with open(src) as actual:
                    self.assertEquals(actual.read(), exp.read())
            self.assertEquals(dst, self.conf)
            self.write_dummy_conf()

        self.check_call(ANY, KWARGS)
        self.mocker.call(verify_cp)
        self.mock_call(("sudo", "rm", self.output))
        self.mock_call(("sudo", "chmod", "644", self.conf))
        self.mock_status(succeed("blah stop/waiting blah"))
        self.mock_call(("sudo", "/sbin/start", "some-name"))
        # 5 for initial stability check; 1 for final do-we-need-to-stop check
        for _ in range(6):
            self.mock_status(succeed("blah start/running blah 12345"))
        self.mock_call(("sudo", "/sbin/stop", "some-name"))
        self.mock_call(("sudo", "rm", self.conf))
        self.mock_call(("sudo", "rm", self.output))

        self.mocker.replay()
        yield self.service.start()
        yield self.service.destroy()
