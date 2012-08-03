import logging
import os

from twisted.internet.defer import inlineCallbacks

from juju.machine.unit import get_deploy_factory
from juju.state.charm import CharmStateManager
from juju.state.environment import GlobalSettingsStateManager
from juju.state.service import ServiceStateManager
from juju.unit.charm import download_charm


log = logging.getLogger("unit.deploy")


class UnitDeployer(object):
    """Manages service unit deployment for an agent.
    """

    def __init__(self, client, machine_id, juju_directory):
        """Initialize a Unit Deployer.

        :param client: A connected zookeeper client.
        :param str machine_id: the ID of the machine the agent is being run on.
        :param str juju_directory: the directory the agent is running in.
        """
        self.client = client
        self.machine_id = machine_id
        self.juju_directory = juju_directory
        self.service_state_manager = ServiceStateManager(self.client)
        self.charm_state_manager = CharmStateManager(self.client)

    @property
    def charms_directory(self):
        return os.path.join(self.juju_directory, "charms")

    @inlineCallbacks
    def start(self, provider_type=None):
        """Starts the unit deployer."""
        # Find out what provided the machine, and how to deploy units.
        if provider_type is None:
            settings = GlobalSettingsStateManager(self.client)
            provider_type = yield settings.get_provider_type()
        self.deploy_factory = get_deploy_factory(provider_type)

        if not os.path.exists(self.charms_directory):
            os.makedirs(self.charms_directory)

    def download_charm(self, charm_state):
        """Retrieve a charm from the provider storage to the local machine.

        :param charm_state: Charm to be downloaded
        """
        log.debug("Downloading charm %s to %s",
                  charm_state.id, self.charms_directory)
        return download_charm(
            self.client, charm_state.id, self.charms_directory)

    @inlineCallbacks
    def start_service_unit(self, service_unit_name):
        """Start a service unit on the machine.

        Downloads the charm, and extract it to the service unit directory,
        and launch the service unit agent within the unit directory.

        :param str service_unit_name: Service unit name to be started
        """

        # Retrieve the charm state to get at the charm.
        unit_state = yield self.service_state_manager.get_unit_state(
            service_unit_name)
        charm_id = yield unit_state.get_charm_id()
        charm_state = yield self.charm_state_manager.get_charm_state(
            charm_id)

        # Download the charm.
        bundle = yield self.download_charm(charm_state)

        # Use deployment to setup the workspace and start the unit agent.
        deployment = self.deploy_factory(
            service_unit_name, self.juju_directory)

        log.debug("Using %r for %s in %s",
                  deployment,
                  service_unit_name,
                  self.juju_directory)

        running = yield deployment.is_running()
        if not running:
            log.debug("Starting service unit %s...", service_unit_name)
            yield deployment.start(
                self.machine_id, self.client.servers, bundle)
            log.info("Started service unit %s", service_unit_name)

    @inlineCallbacks
    def kill_service_unit(self, service_unit_name):
        """Stop service unit and destroy disk state, ala SIGKILL or lxc-destroy

        :param str service_unit_name: Service unit name to be killed
        """
        deployment = self.deploy_factory(
            service_unit_name, self.juju_directory)
        log.info("Stopping service unit %s...", service_unit_name)
        yield deployment.destroy()
        log.info("Stopped service unit %s", service_unit_name)
