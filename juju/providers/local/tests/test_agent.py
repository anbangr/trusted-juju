import os
import tempfile
import subprocess
import sys

from twisted.internet.defer import inlineCallbacks, succeed

from juju.lib.lxc.tests.test_lxc import uses_sudo
from juju.lib.testing import TestCase
from juju.lib.upstart import UpstartService
from juju.tests.common import get_test_zookeeper_address
from juju.providers.local.agent import ManagedMachineAgent


class ManagedAgentTest(TestCase):

    @inlineCallbacks
    def test_managed_agent_config(self):
        subprocess_calls = []

        def intercept_args(args, **kwargs):
            subprocess_calls.append(args)
            self.assertEquals(args[0], "sudo")
            if args[1] == "cp":
                return real_check_call(args[1:], **kwargs)
            return 0

        real_check_call = self.patch(subprocess, "check_call", intercept_args)
        init_dir = self.makeDir()
        self.patch(UpstartService, "init_dir", init_dir)

        # Mock out the repeated checking for unstable pid, after an initial
        # stop/waiting to induce the actual start
        getProcessOutput = self.mocker.replace(
            "twisted.internet.utils.getProcessOutput")
        getProcessOutput("/sbin/status", ["juju-ns1-machine-agent"])
        self.mocker.result(succeed("stop/waiting"))
        for _ in range(5):
            getProcessOutput("/sbin/status", ["juju-ns1-machine-agent"])
            self.mocker.result(succeed("start/running 123"))
        self.mocker.replay()

        juju_directory = self.makeDir()
        log_file = self.makeFile()
        agent = ManagedMachineAgent(
            "ns1",
            get_test_zookeeper_address(),
            juju_series="precise",
            juju_directory=juju_directory,
            log_file=log_file,
            juju_origin="lp:juju/trunk")

        try:
            os.remove("/tmp/juju-ns1-machine-agent.output")
        except OSError:
            pass  # just make sure it's not there, so the .start()
                  # doesn't insert a spurious rm

        yield agent.start()

        conf_dest = os.path.join(
            init_dir, "juju-ns1-machine-agent.conf")
        chmod, start = subprocess_calls[1:]
        self.assertEquals(chmod, ("sudo", "chmod", "644", conf_dest))
        self.assertEquals(
            start, ("sudo", "/sbin/start", "juju-ns1-machine-agent"))

        env = []
        with open(conf_dest) as f:
            for line in f:
                if line.startswith("env"):
                    env.append(line[4:-1].split("=", 1))
                if line.startswith("exec"):
                    exec_ = line[5:-1]

        expect_exec = (
            "/usr/bin/python -m juju.agents.machine --nodaemon --logfile %s "
            "--session-file /var/run/juju/ns1-machine-agent.zksession "
            ">> /tmp/juju-ns1-machine-agent.output 2>&1"
            % log_file)
        self.assertEquals(exec_, expect_exec)

        env = dict((k, v.strip('"')) for (k, v) in env)
        self.assertEquals(env, {
            "JUJU_ZOOKEEPER": get_test_zookeeper_address(),
            "JUJU_MACHINE_ID": "0",
            "JUJU_HOME": juju_directory,
            "JUJU_ORIGIN": "lp:juju/trunk",
            "JUJU_UNIT_NS": "ns1",
            "JUJU_SERIES": "precise",
            "PYTHONPATH": ":".join(sys.path)})

    @uses_sudo
    @inlineCallbacks
    def test_managed_agent_root(self):
        juju_directory = self.makeDir()
        log_file = tempfile.mktemp()

        # The pid file and log file get written as root
        def cleanup_root_file(cleanup_file):
            subprocess.check_call(
                ["sudo", "rm", "-f", cleanup_file], stderr=subprocess.STDOUT)
        self.addCleanup(cleanup_root_file, log_file)

        agent = ManagedMachineAgent(
            "test-ns", machine_id="0",
            log_file=log_file,
            juju_series="precise",
            zookeeper_hosts=get_test_zookeeper_address(),
            juju_directory=juju_directory)

        agent.agent_module = "juju.agents.dummy"
        self.assertFalse((yield agent.is_running()))
        yield agent.start()
        # Give a moment for the process to start and write its config
        yield self.sleep(0.1)
        self.assertTrue((yield agent.is_running()))

        # running start again is fine, detects the process is running
        yield agent.start()
        yield agent.stop()
        self.assertFalse((yield agent.is_running()))

        # running stop again is fine, detects the process is stopped.
        yield agent.stop()
