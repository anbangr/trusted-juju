import logging
import os

from twisted.internet.defer import inlineCallbacks

from juju.errors import JujuError
from juju.state.machine import MachineStateManager
from juju.state.service import ServiceStateManager
from juju.unit.deploy import UnitDeployer

from .base import BaseAgent


log = logging.getLogger("juju.agents.machine")


class MachineAgent(BaseAgent):
    """A juju machine agent.

    The machine agent is responsible for monitoring service units
    assigned to a machine. If a new unit is assigned to machine, the
    machine agent will download the charm, create a working
    space for the service unit agent, and then launch it.

    Additionally the machine agent will monitor the running service
    unit agents on the machine, via their ephemeral nodes, and
    restart them if they die.
    """

    name = "juju-machine-agent"
    unit_agent_module = "juju.agents.unit"

    @property
    def units_directory(self):
        return os.path.join(self.config["juju_directory"], "units")

    @property
    def unit_state_directory(self):
        return os.path.join(self.config["juju_directory"], "state")

    @inlineCallbacks
    def start(self):
        """Start the machine agent.

        Creates state directories on the machine, retrieves the machine state,
        and enables watch on assigned units.
        """
        if not os.path.exists(self.units_directory):
            os.makedirs(self.units_directory)

        if not os.path.exists(self.unit_state_directory):
            os.makedirs(self.unit_state_directory)

        # Get state managers we'll be utilizing.
        self.service_state_manager = ServiceStateManager(self.client)
        self.unit_deployer = UnitDeployer(
            self.client, self.get_machine_id(), self.config["juju_directory"])
        yield self.unit_deployer.start()

        # Retrieve the machine state for the machine we represent.
        machine_manager = MachineStateManager(self.client)
        self.machine_state = yield machine_manager.get_machine_state(
            self.get_machine_id())

        # Watch assigned units for the machine.
        if self.get_watch_enabled():
            self.machine_state.watch_assigned_units(
                self.watch_service_units)

        # Connect the machine agent, broadcasting presence to the world.
        yield self.machine_state.connect_agent()
        log.info("Machine agent started id:%s" % self.get_machine_id())

    @inlineCallbacks
    def watch_service_units(self, old_units, new_units):
        """Callback invoked when the assigned service units change.
        """
        if old_units is None:
            old_units = set()

        log.debug(
            "Units changed old:%s new:%s", old_units, new_units)

        stopped = old_units - new_units
        started = new_units - old_units

        for unit_name in stopped:
            log.debug("Stopping service unit: %s ...", unit_name)
            try:
                yield self.unit_deployer.kill_service_unit(unit_name)
            except Exception:
                log.exception("Error stopping unit: %s", unit_name)

        for unit_name in started:
            log.debug("Starting service unit: %s ...", unit_name)
            try:
                yield self.unit_deployer.start_service_unit(unit_name)
            except Exception:
                log.exception("Error starting unit: %s", unit_name)

    def get_machine_id(self):
        """Get the id of the machine as known within the zk state."""
        return self.config["machine_id"]

    def get_agent_name(self):
        return "Machine:%s" % (self.get_machine_id())

    def configure(self, options):
        super(MachineAgent, self).configure(options)
        if not options.get("machine_id"):
            msg = ("--machine-id must be provided in the command line, "
                   "or $JUJU_MACHINE_ID in the environment")
            raise JujuError(msg)

    @classmethod
    def setup_options(cls, parser):
        super(MachineAgent, cls).setup_options(parser)

        machine_id = os.environ.get("JUJU_MACHINE_ID", "")
        parser.add_argument(
            "--machine-id", default=machine_id)
        return parser


if __name__ == '__main__':
    MachineAgent().run()
