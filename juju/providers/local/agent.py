import sys

from juju.lib.upstart import UpstartService
from juju.providers.common.cloudinit import get_default_origin, BRANCH


class ManagedMachineAgent(object):

    agent_module = "juju.agents.machine"

    def __init__(
            self, juju_unit_namespace, zookeeper_hosts=None,
            machine_id="0", log_file=None, juju_directory="/var/lib/juju",
            public_key=None, juju_origin="ppa", juju_series=None):
        """
        :param juju_series: The release series to use (maverick, natty, etc).
        :param machine_id: machine id for the local machine.
        :param zookeeper_hosts: Zookeeper hosts to connect.
        :param log_file: A file to use for the agent logs.
        :param juju_directory: The directory to use for all state and logs.
        :param juju_unit_namespace: The machine agent will create units with
               a known a prefix to allow for multiple users and multiple
               environments to create containers. The namespace should be
               unique per user and per environment.
        :param public_key: An SSH public key (string) that will be
               used in the container for access.
        """
        self._juju_origin = juju_origin
        if self._juju_origin is None:
            origin, source = get_default_origin()
            if origin == BRANCH:
                origin = source
            self._juju_origin = origin

        env = {"JUJU_MACHINE_ID": machine_id,
               "JUJU_ZOOKEEPER": zookeeper_hosts,
               "JUJU_HOME": juju_directory,
               "JUJU_ORIGIN": self._juju_origin,
               "JUJU_UNIT_NS": juju_unit_namespace,
               "JUJU_SERIES": juju_series,
               "PYTHONPATH": ":".join(sys.path)}
        if public_key:
            env["JUJU_PUBLIC_KEY"] = public_key

        self._service = UpstartService(
            "juju-%s-machine-agent" % juju_unit_namespace, use_sudo=True)
        self._service.set_description(
            "Juju machine agent for %s" % juju_unit_namespace)
        self._service.set_environ(env)
        self._service_args = [
            "/usr/bin/python", "-m", self.agent_module,
            "--nodaemon", "--logfile", log_file,
            "--session-file",
            "/var/run/juju/%s-machine-agent.zksession" % juju_unit_namespace]

    @property
    def juju_origin(self):
        return self._juju_origin

    def start(self):
        """Start the machine agent."""
        self._service.set_command(" ".join(self._service_args))
        return self._service.start()

    def stop(self):
        """Stop the machine agent."""
        return self._service.destroy()

    def is_running(self):
        """Boolean value, true if the machine agent is running."""
        return self._service.is_running()
