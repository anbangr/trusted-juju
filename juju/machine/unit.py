import os
import shutil
import logging

import juju

from twisted.internet.defer import inlineCallbacks, returnValue

from juju.charm.bundle import CharmBundle
from juju.errors import ServiceError
from juju.lib.lxc import LXCContainer, get_containers, LXCError
from juju.lib.twistutils import get_module_directory
from juju.lib.upstart import UpstartService

from .errors import UnitDeploymentError

log = logging.getLogger("unit.deploy")


def get_deploy_factory(provider_type):
    if provider_type == "local":
        return UnitContainerDeployment
    elif provider_type == "subordinate":
        return SubordinateContainerDeployment

    return UnitMachineDeployment


def _get_environment(unit_name, juju_home, machine_id, zookeeper_hosts):
    environ = dict()
    environ["JUJU_MACHINE_ID"] = str(machine_id)
    environ["JUJU_UNIT_NAME"] = unit_name
    environ["JUJU_HOME"] = juju_home
    environ["JUJU_ZOOKEEPER"] = zookeeper_hosts
    environ["PYTHONPATH"] = ":".join(
        filter(None, [
            os.path.dirname(get_module_directory(juju)),
            os.environ.get("PYTHONPATH")]))
    return environ


class UnitMachineDeployment(object):
    """ Deploy a unit directly onto a machine.

    A service unit deployed directly to a machine has full access
    to the machine resources.

    Units deployed in such a manner have no isolation from other units
    on the machine, and may leave artifacts on the machine even upon
    service destruction.
    """

    unit_agent_module = "juju.agents.unit"

    def __init__(self, unit_name, juju_home):
        assert ".." not in unit_name, "Invalid Unit Name"
        self.unit_name = unit_name
        self.juju_home = juju_home
        self.unit_path_name = unit_name.replace("/", "-")
        self.directory = os.path.join(
            self.juju_home, "units", self.unit_path_name)
        self.service = UpstartService(
            # NOTE: we need use_sudo to work correctly during tests that
            # launch actual processes (rather than just mocking/trusting).
            "juju-%s" % self.unit_path_name, use_sudo=True)

    @inlineCallbacks
    def start(self, machine_id, zookeeper_hosts, bundle):
        """Start a service unit agent."""
        self.unpack_charm(bundle)
        self.service.set_description(
            "Juju unit agent for %s" % self.unit_name)
        self.service.set_environ(_get_environment(
            self.unit_name, self.juju_home, machine_id, zookeeper_hosts))
        self.service.set_command(" ".join((
            "/usr/bin/python", "-m", self.unit_agent_module,
            "--nodaemon",
            "--logfile", os.path.join(self.directory, "charm.log"),
            "--session-file",
            "/var/run/juju/unit-%s-agent.zksession" % self.unit_path_name)))
        try:
            yield self.service.start()
        except ServiceError as e:
            raise UnitDeploymentError(str(e))

    @inlineCallbacks
    def destroy(self):
        """Forcibly terminate a service unit agent, and clean disk state.

        This will destroy/unmount any state on disk.
        """
        yield self.service.destroy()
        if os.path.exists(self.directory):
            shutil.rmtree(self.directory)

    def get_pid(self):
        """Get the service unit's process id."""
        return self.service.get_pid()

    def is_running(self):
        """Is the service unit running."""
        return self.service.is_running()

    def unpack_charm(self, charm):
        """Unpack a charm to the service units directory."""
        if not isinstance(charm, CharmBundle):
            raise UnitDeploymentError(
                "Invalid charm for deployment: %s" % charm.path)

        charm.extract_to(os.path.join(self.directory, "charm"))


class SubordinateContainerDeployment(UnitMachineDeployment):
    """Deploy a subordinate unit.

    Assumes the basic runtime has been built for/by the principal
    service or its machine agent.
    """
    def __init__(self, unit_name, juju_home):
        assert ".." not in unit_name, "Invalid Unit Name"
        self.unit_name = unit_name
        self.juju_home = juju_home
        self.unit_path_name = unit_name.replace("/", "-")
        self.directory = os.path.join(
            self.juju_home, "units", self.unit_path_name)
        self.service = UpstartService(
            # NOTE: we need use_sudo to work correctly during tests that
            # launch actual processes (rather than just mocking/trusting).
            "juju-%s" % self.unit_path_name, use_sudo=True)


class UnitContainerDeployment(object):
    """Deploy a service unit in a container.

    Units deployed in a container have strong isolation between
    others units deployed in a container on the same machine.

    From the perspective of the service unit, the container deployment
    should be indistinguishable from a machine deployment.

    Note, strong isolation properties are still fairly trivial
    to escape for a user with a root account within the container.
    This is an ongoing development topic for LXC.
    """

    def __init__(self, unit_name, juju_home):
        self.unit_name = unit_name
        self.juju_home = juju_home
        self.unit_path_name = unit_name.replace("/", "-")

        self._juju_origin = os.environ.get("JUJU_ORIGIN")
        self._juju_series = os.environ.get("JUJU_SERIES")
        assert self._juju_series is not None, "Required juju series not found"
        self._unit_namespace = os.environ.get("JUJU_UNIT_NS")
        assert self._unit_namespace is not None, "Required unit ns not found"
        self.container_name = "%s-%s" % (
            self._unit_namespace, self.unit_path_name)

        self.container = LXCContainer(self.container_name, None, None, None)
        self.directory = None

    def setup_directories(self):
        # Create state directories for unit in the container
        # Move to juju-create script
        base = self.directory
        dirs = ((base, "var", "lib", "juju", "units", self.unit_path_name),
                (base, "var", "lib", "juju", "state"),
                (base, "var", "log", "juju"),
                (self.juju_home, "units", self.unit_path_name))

        for parts in dirs:
            dir_ = os.path.join(*parts)
            if not os.path.exists(dir_):
                os.makedirs(dir_)

    @inlineCallbacks
    def _get_master_template(self, machine_id, public_key):
        container_template_name = "%s-%s-template" % (
            self._unit_namespace, machine_id)

        master_template = LXCContainer(
            container_template_name, origin=self._juju_origin,
            public_key=public_key, series=self._juju_series)

        # Debug log for the customize script, customize is only run on master.
        customize_log_path = os.path.join(
            self.juju_home, "units", "master-customize.log")
        master_template.customize_log = customize_log_path

        if not master_template.is_constructed():
            log.debug("Creating master container...")
            yield master_template.create()
            log.debug("Created master container %s", container_template_name)

        # it wasn't constructed and we couldn't construct it
        if not master_template.is_constructed():
            raise LXCError("Unable to create master container")

        returnValue(master_template)

    @inlineCallbacks
    def _get_container(self, machine_id, bundle, public_key):
        master_template = yield self._get_master_template(
            machine_id, public_key)
        log.info(
            "Creating container %s...", self.unit_path_name)

        container = yield master_template.clone(self.container_name)
        directory = container.rootfs
        log.info("Container created for %s", self.unit_name)
        returnValue((container, directory))

    @inlineCallbacks
    def start(self, machine_id, zookeeper_hosts, bundle):
        """Start the unit.

        Creates and starts an lxc container for the unit.
        """
        # remove any quoting around the key
        public_key = os.environ.get("JUJU_PUBLIC_KEY", "")
        public_key = public_key.strip("'\"")

        # Build a template container that can be cloned in deploy
        # we leave the loosely initialized self.container in place for
        # the class as thats all we need for methods other than start.
        self.container, self.directory = yield self._get_container(
            machine_id, bundle, public_key)

        # Create state directories for unit in the container
        self.setup_directories()

        # Extract the charm bundle
        charm_path = os.path.join(
            self.directory, "var", "lib", "juju", "units",
            self.unit_path_name, "charm")
        bundle.extract_to(charm_path)
        log.debug("Charm extracted into container")

        # Write upstart file for the agent into the container
        service_name = "juju-%s" % self.unit_path_name
        init_dir = os.path.join(self.directory, "etc", "init")
        service = UpstartService(service_name, init_dir=init_dir)
        service.set_description(
            "Juju unit agent for %s" % self.unit_name)
        service.set_environ(_get_environment(
            self.unit_name, "/var/lib/juju", machine_id, zookeeper_hosts))
        service.set_output_path(
            "/var/log/juju/unit-%s-output.log" % self.unit_path_name)
        service.set_command(" ".join((
            "/usr/bin/python",
            "-m", "juju.agents.unit",
            "--nodaemon",
            "--logfile", "/var/log/juju/unit-%s.log" % self.unit_path_name,
            "--session-file",
            "/var/run/juju/unit-%s-agent.zksession" % self.unit_path_name)))
        yield service.install()

        # Create symlinks on the host for easier access to the unit log files
        unit_log_path_host = os.path.join(
            self.juju_home, "units", self.unit_path_name, "unit.log")
        if not os.path.lexists(unit_log_path_host):
            os.symlink(
                os.path.join(self.directory, "var", "log", "juju",
                             "unit-%s.log" % self.unit_path_name),
                unit_log_path_host)
        unit_output_path_host = os.path.join(
            self.juju_home, "units", self.unit_path_name, "output.log")
        if not os.path.lexists(unit_output_path_host):
            os.symlink(
                os.path.join(self.directory, "var", "log", "juju",
                             "unit-%s-output.log" % self.unit_path_name),
                unit_output_path_host)

        # Debug log for the container
        container_log_path = os.path.join(
            self.juju_home, "units", self.unit_path_name, "container.log")
        self.container.debug_log = container_log_path

        log.debug("Starting container...")
        yield self.container.run()
        log.info("Started container for %s", self.unit_name)

    @inlineCallbacks
    def destroy(self):
        """Destroy the unit container."""
        log.debug("Destroying container...")
        yield self.container.destroy()
        log.info("Destroyed container for %s", self.unit_name)

    @inlineCallbacks
    def is_running(self):
        """Is the unit container running?"""
        # TODO: container running may not imply agent running.
        # query zookeeper for the unit agent presence node?
        if not self.container:
            returnValue(False)
        container_map = yield get_containers(
            prefix=self.container.container_name)
        returnValue(container_map.get(self.container.container_name, False))
