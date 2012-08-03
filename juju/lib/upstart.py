import os
import subprocess
from tempfile import NamedTemporaryFile

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.threads import deferToThread
from twisted.internet.utils import getProcessOutput

from juju.errors import ServiceError
from juju.lib.twistutils import sleep


_CONF_TEMPLATE = """\
description "%s"
author "Juju Team <juju@lists.ubuntu.com>"

start on runlevel [2345]
stop on runlevel [!2345]
respawn

%s

exec %s >> %s 2>&1
"""

def _silent_check_call(args):
    with open(os.devnull, "w") as f:
        return subprocess.check_call(
            args, stdout=f.fileno(), stderr=f.fileno())


class UpstartService(object):

    # on class for ease of testing
    init_dir = "/etc/init"

    def __init__(self, name, init_dir=None, use_sudo=False):
        self._name = name
        if init_dir is not None:
            self.init_dir = init_dir
        self._use_sudo = use_sudo
        self._output_path = None
        self._description = None
        self._environ = {}
        self._command = None

    @property
    def _conf_path(self):
        return os.path.join(
            self.init_dir, "%s.conf" % self._name)

    @property
    def output_path(self):
        if self._output_path is not None:
            return self._output_path
        return "/tmp/%s.output" % self._name

    def set_description(self, description):
        self._description = description

    def set_environ(self, environ):
        self._environ = environ

    def set_command(self, command):
        self._command = command

    def set_output_path(self, path):
        self._output_path = path

    @inlineCallbacks
    def _trash_output(self):
        if os.path.exists(self.output_path):
            # Just using os.unlink will fail when we're running TEST_SUDO tests
            # which hit this code path (because root will own self.output_path)
            yield self._call("rm", self.output_path)

    def _render(self):
        if self._description is None:
            raise ServiceError("Cannot render .conf: no description set")
        if self._command is None:
            raise ServiceError("Cannot render .conf: no command set")
        return _CONF_TEMPLATE % (
            self._description,
            "\n".join('env %s="%s"' % kv
                      for kv in sorted(self._environ.items())),
            self._command,
            self.output_path)

    def _call(self, *args):
        if self._use_sudo:
            args = ("sudo",) + args
        return deferToThread(_silent_check_call, args)

    def get_cloud_init_commands(self):
        return ["cat >> %s <<EOF\n%sEOF\n" % (self._conf_path, self._render()),
                "/sbin/start %s" % self._name]

    @inlineCallbacks
    def install(self):
        with NamedTemporaryFile() as f:
            f.write(self._render())
            f.flush()
            yield self._call("cp", f.name, self._conf_path)
        yield self._call("chmod", "644", self._conf_path)

    @inlineCallbacks
    def start(self):
        if not self.is_installed():
            yield self.install()
        if (yield self.is_running()):
            return
        yield self._trash_output()
        yield self._call("/sbin/start", self._name)
        if (yield self.is_stable()):
            return

        output = None
        if os.path.exists(self.output_path):
            with open(self.output_path) as f:
                output = f.read()
        if not output:
            raise ServiceError(
                "Failed to start job %s; no output detected" % self._name)
        raise ServiceError(
            "Failed to start job %s; got output:\n%s" % (self._name, output))

    @inlineCallbacks
    def destroy(self):
        if (yield self.is_running()):
            yield self._call("/sbin/stop", self._name)
        if self.is_installed():
            yield self._call("rm", self._conf_path)
        yield self._trash_output()

    @inlineCallbacks
    def get_pid(self):
        if not self.is_installed():
            returnValue(None)
        status = yield getProcessOutput("/sbin/status", [self._name])
        if "start/running" not in status:
            returnValue(None)
        pid = status.split(" ")[-1]
        returnValue(int(pid))

    @inlineCallbacks
    def is_running(self):
        pid = yield self.get_pid()
        returnValue(pid is not None)

    @inlineCallbacks
    def is_stable(self):
        """Does the process continue to run with the same pid?

        (5 times in a row, with a gap of 0.1s between each check)
        """
        pid = yield self.get_pid()
        if pid is None:
            returnValue(False)
        for _ in range(4):
            yield sleep(0.1)
            if pid != (yield self.get_pid()):
                returnValue(False)
        returnValue(True)

    def is_installed(self):
        return os.path.exists(self._conf_path)
