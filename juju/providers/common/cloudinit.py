from base64 import b64encode
from subprocess import Popen, PIPE
from yaml import safe_dump

from juju.errors import CloudInitError
from juju.lib.upstart import UpstartService
from juju.providers.common.utils import format_cloud_init
from juju.state.auth import make_identity
import juju

DISTRO = "distro"
PPA = "ppa"
BRANCH = "branch"
PROPOSED = "proposed"


def _branch_install_scripts(branch):
    return [
        "sudo apt-get install -y python-txzookeeper",
        "sudo mkdir -p /usr/lib/juju",
        "cd /usr/lib/juju && sudo /usr/bin/bzr co %s juju" % branch,
        "cd /usr/lib/juju/juju && sudo python setup.py develop"]


def _install_scripts(origin, origin_url):
    scripts = []
    if origin == BRANCH:
        scripts.extend(_branch_install_scripts(origin_url))

    scripts.extend([
        "sudo mkdir -p /var/lib/juju",
        "sudo mkdir -p /var/log/juju"])
    return scripts


def _zookeeper_scripts(instance_id, secret, constraints, provider_type):
    return [
        "juju-admin initialize"
        " --instance-id=%s"
        " --admin-identity=%s"
        " --constraints-data=%s"
        " --provider-type=%s"
        % (instance_id, make_identity("admin:%s" % secret),
           b64encode(safe_dump(constraints.data)), provider_type)]


def _machine_scripts(machine_id, zookeeper_hosts):
    service = UpstartService("juju-machine-agent")
    service.set_description("Juju machine agent")
    service.set_environ(
        {"JUJU_MACHINE_ID": machine_id, "JUJU_ZOOKEEPER": zookeeper_hosts})
    service.set_command(
        "python -m juju.agents.machine --nodaemon "
        "--logfile /var/log/juju/machine-agent.log "
        "--session-file /var/run/juju/machine-agent.zksession")
    return service.get_cloud_init_commands()


def _provision_scripts(zookeeper_hosts):
    service = UpstartService("juju-provision-agent")
    service.set_description("Juju provisioning agent")
    service.set_environ({"JUJU_ZOOKEEPER": zookeeper_hosts})
    service.set_command(
        "python -m juju.agents.provision --nodaemon "
        "--logfile /var/log/juju/provision-agent.log "
        "--session-file /var/run/juju/provision-agent.zksession")
    return service.get_cloud_init_commands()


def _line_generator(data):
    for line in data.splitlines():
        stripped = line.lstrip()
        if stripped:
            yield (len(line) - len(stripped), stripped)


def parse_juju_origin(data):
    next = _line_generator(data).next
    try:
        _, line = next()
        if line == "N: Unable to locate package juju":
            return BRANCH, "lp:juju"
        if line != "juju:":
            raise StopIteration

        # Find installed version.
        while True:
            _, line = next()
            if line.startswith("Installed:"):
                version = line[10:].strip()
                if version == "(none)":
                    return BRANCH, "lp:juju"
                break

        # Find version table.
        while True:
            _, line = next()
            if line.startswith("Version table:"):
                break

        # Find installed version within the table.
        while True:
            _, line = next()
            if line.startswith("*** %s " % version):
                break

        # See if one of the sources is the PPA
        first_indent, line = next()
        while True:
            if "http://ppa.launchpad.net/juju/pkgs/" in line:
                return PPA, None
            indent, line = next()
            if indent != first_indent:
                break  # Going into a different version
    except StopIteration:
        pass
    return DISTRO, None


def get_default_origin():
    """Select the best fit for running juju on cloudinit.

    Used if not otherwise specified by juju-origin.
    """
    if not juju.__file__.startswith("/usr/"):
        return BRANCH, "lp:juju"
    try:
        popen = Popen(["apt-cache", "policy", "juju"], stdout=PIPE)
        data = popen.communicate()[0]
    except OSError:
        data = ""
    return parse_juju_origin(data)


class CloudInit(object):
    """Encapsulates juju-specific machine initialisation.

    For more information on the mechanism used, see
    :func:`juju.providers.common.utils.format_cloud_init`.
    """

    def __init__(self):
        self._machine_id = None
        self._instance_id = None
        self._provider_type = None
        self._ssh_keys = []
        self._provision = False
        self._zookeeper = False
        self._zookeeper_hosts = []
        self._zookeeper_secret = None
        self._constraints = None
        self._origin, self._origin_url = get_default_origin()

    def add_ssh_key(self, key):
        """Add an SSH public key.

        :param key: an SSH key to allow to connect to the machine

        You have to set at least one SSH key.
        """
        self._ssh_keys.append(key)

    def enable_bootstrap(self):
        """Make machine run a zookeeper and a provisioning agent."""
        self._zookeeper = True
        self._provision = True

    def set_juju_source(
        self, branch=None, ppa=False, distro=False, proposed=False):
        """Set the version of juju the machine should run.

        :param branch: location from which to check out juju; for example,
            "lp:~someone/juju/some-feature-branch".
        :type branch: str or None

        :param bool ppa: if True, get the latest version of juju from its
            Private Package Archive.

        :param bool distro: if True, get the default juju version from the
            OS distribution.

        :param bool proposed: if True, get the proposed juju version
            from the OS distribution.

        :raises: :exc:`juju.errors.CloudInitError` if more than one option,
            or fewer than one options, are specified.

        Note that you don't need to call this method; the juju source
        defaults to what is returned by `get_default_origin`.
        """
        if len(filter(None, (branch, ppa, distro, proposed))) != 1:
            raise CloudInitError("Please specify one source")
        if branch:
            self._origin = BRANCH
            self._origin_url = branch
        elif ppa:
            self._origin = PPA
            self._origin_url = None
        elif distro:
            self._origin = DISTRO
            self._origin_url = None
        elif proposed:
            self._origin = PROPOSED
            self._origin_url = None

    def set_machine_id(self, id):
        """Specify the juju machine ID.

        :param str id: the desired ID.

        You have to set the machine ID.
        """
        self._machine_id = id

    def set_instance_id_accessor(self, expr):
        """Specify the provider-specific instance ID.

        :param str expr: a snippet of shell script that will, when executed on
            the machine, evaluate to the machine's instance ID.

        You have to set the instance ID.
        """
        self._instance_id = expr

    def set_provider_type(self, type_):
        """Specify the provider type for this machine.

        :param str type_: the provider type.

        You have to set the provider type.
        """
        self._provider_type = type_

    def set_zookeeper_machines(self, machines):
        """Specify master :class:`juju.machine.ProviderMachine` instances.

        :param machines: machines running zookeeper which already exist.
        :type machines: list of :class:`juju.machine.ProviderMachine`

        You don't have to set this, so long as the machine you are starting is
        itself a zookeeper instance.
        """
        self._zookeeper_hosts = [m.private_dns_name for m in machines]

    def set_zookeeper_secret(self, secret):
        """Specify the admin password for zookeepeer.

        You only need to set this if this machine will be a zookeeper instance.
        """
        self._zookeeper_secret = secret

    def set_constraints(self, constraints):
        """Specify the initial machine's constraints.

        You only need to set this if this machine will be a zookeeper instance.
        """
        self._constraints = constraints

    def render(self):
        """Get content for a cloud-init file with appropriate specifications.

        :rtype: str

        :raises: :exc:`juju.errors.CloudInitError` if there isn't enough
            information to create a useful cloud-init.
        """
        self._validate()
        return format_cloud_init(
            self._ssh_keys,
            packages=self._collect_packages(),
            repositories=self._collect_repositories(),
            scripts=self._collect_scripts(),
            data=self._collect_machine_data())

    def _validate(self):
        missing = []

        def require(attr, action):
            if not getattr(self, attr):
                missing.append(action)

        require("_ssh_keys", "add_ssh_key")
        require("_machine_id", "set_machine_id")
        require("_provider_type", "set_provider_type")
        if self._zookeeper:
            require("_instance_id", "set_instance_id_accessor")
            require("_zookeeper_secret", "set_zookeeper_secret")
            require("_constraints", "set_constraints")
        else:
            require("_zookeeper_hosts", "set_zookeeper_machines")
        if missing:
            raise CloudInitError("Incomplete cloud-init: you need to call %s"
                                 % ", ".join(missing))

    def _join_zookeeper_hosts(self):
        all_hosts = self._zookeeper_hosts[:]
        if self._zookeeper:
            all_hosts.append("localhost")
        return ",".join(["%s:2181" % host for host in all_hosts])

    def _collect_packages(self):
        packages = [
            "bzr", "byobu", "tmux", "python-setuptools", "python-twisted",
            "python-txaws", "python-zookeeper"]
        if self._zookeeper:
            packages.extend([
                "default-jre-headless", "zookeeper", "zookeeperd"])
        if self._origin in (DISTRO, PPA, PROPOSED):
            packages.append("juju")
        return packages

    def _collect_repositories(self):
        if self._origin == PROPOSED:
            return ["deb $MIRROR $RELEASE-proposed main universe"]
        if self._origin != DISTRO:
            return ["ppa:juju/pkgs"]
        return []

    def _collect_scripts(self):
        scripts = _install_scripts(self._origin, self._origin_url)
        if self._zookeeper:
            scripts.extend(_zookeeper_scripts(
                self._instance_id,
                self._zookeeper_secret,
                self._constraints,
                self._provider_type))
        scripts.extend(_machine_scripts(
            self._machine_id, self._join_zookeeper_hosts()))
        if self._provision:
            scripts.extend(_provision_scripts(self._join_zookeeper_hosts()))
        return scripts

    def _collect_machine_data(self):
        return {
            "machine-id": self._machine_id,
            "juju-provider-type": self._provider_type,
            "juju-zookeeper-hosts": self._join_zookeeper_hosts()}
