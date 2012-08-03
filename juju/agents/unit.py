import os
import logging

from twisted.internet.defer import inlineCallbacks, returnValue

from juju.errors import JujuError
from juju.state.service import ServiceStateManager, RETRY_HOOKS
from juju.hooks.protocol import UnitSettingsFactory
from juju.hooks.executor import HookExecutor

from juju.unit.address import get_unit_address
from juju.unit.lifecycle import UnitLifecycle, HOOK_SOCKET_FILE
from juju.unit.workflow import UnitWorkflowState

from juju.agents.base import BaseAgent


log = logging.getLogger("juju.agents.unit")


def unit_path(juju_path, unit_state):
    return os.path.join(
        juju_path, "units", unit_state.unit_name.replace("/", "-"))


class UnitAgent(BaseAgent):
    """An juju Unit Agent.

    Provides for the management of a charm, via hook execution in response to
    external events in the coordination space (zookeeper).
    """
    name = "juju-unit-agent"

    @classmethod
    def setup_options(cls, parser):
        super(UnitAgent, cls).setup_options(parser)
        unit_name = os.environ.get("JUJU_UNIT_NAME", "")
        parser.add_argument("--unit-name", default=unit_name)

    @property
    def unit_name(self):
        return self.config["unit_name"]

    def get_agent_name(self):
        return "unit:%s" % self.unit_name

    def configure(self, options):
        """Configure the unit agent."""
        super(UnitAgent, self).configure(options)
        if not options.get("unit_name"):
            msg = ("--unit-name must be provided in the command line, "
                   "or $JUJU_UNIT_NAME in the environment")
            raise JujuError(msg)
        self.executor = HookExecutor()

        self.api_factory = UnitSettingsFactory(
            self.executor.get_hook_context,
            self.executor.get_invoker,
            logging.getLogger("unit.hook.api"))
        self.api_socket = None
        self.workflow = None

    @inlineCallbacks
    def start(self):
        """Start the unit agent process."""
        service_state_manager = ServiceStateManager(self.client)

        # Retrieve our unit and configure working directories.
        service_name = self.unit_name.split("/")[0]
        self.service_state = yield service_state_manager.get_service_state(
            service_name)

        self.unit_state = yield self.service_state.get_unit_state(
            self.unit_name)
        self.unit_directory = os.path.join(
            self.config["juju_directory"], "units",
            self.unit_state.unit_name.replace("/", "-"))
        self.state_directory = os.path.join(
            self.config["juju_directory"], "state")

        # Setup the server portion of the cli api exposed to hooks.
        socket_path = os.path.join(self.unit_directory, HOOK_SOCKET_FILE)
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        from twisted.internet import reactor
        self.api_socket = reactor.listenUNIX(socket_path, self.api_factory)

        # Setup the unit state's address
        address = yield get_unit_address(self.client)
        yield self.unit_state.set_public_address(
            (yield address.get_public_address()))
        yield self.unit_state.set_private_address(
            (yield address.get_private_address()))

        if self.get_watch_enabled():
            yield self.unit_state.watch_hook_debug(self.cb_watch_hook_debug)

        # Inform the system, we're alive.
        yield self.unit_state.connect_agent()

        # Start paying attention to the debug-log setting
        if self.get_watch_enabled():
            yield self.unit_state.watch_hook_debug(self.cb_watch_hook_debug)

        self.lifecycle = UnitLifecycle(
            self.client, self.unit_state, self.service_state,
            self.unit_directory, self.state_directory, self.executor)

        self.workflow = UnitWorkflowState(
            self.client, self.unit_state, self.lifecycle, self.state_directory)

        # Set up correct lifecycle and executor state given the persistent
        # unit workflow state, and fire any starting transitions if necessary.
        with (yield self.workflow.lock()):
            yield self.workflow.synchronize(self.executor)

        if self.get_watch_enabled():
            yield self.unit_state.watch_resolved(self.cb_watch_resolved)
            yield self.service_state.watch_config_state(
                self.cb_watch_config_changed)
            yield self.unit_state.watch_upgrade_flag(
                self.cb_watch_upgrade_flag)

    @inlineCallbacks
    def stop(self):
        """Stop the unit agent process."""
        if self.lifecycle.running:
            yield self.lifecycle.stop(fire_hooks=False, stop_relations=False)
        yield self.executor.stop()
        if self.api_socket:
            yield self.api_socket.stopListening()
        yield self.api_factory.stopFactory()

    @inlineCallbacks
    def cb_watch_resolved(self, change):
        """Update the unit's state, when its resolved.

        Resolved operations form the basis of error recovery for unit
        workflows. A resolved operation can optionally specify hook
        execution. The unit agent runs the error recovery transition
        if the unit is not in a running state.
        """
        # Would be nice if we could fold this into an atomic
        # get and delete primitive.
        # Check resolved setting
        resolved = yield self.unit_state.get_resolved()
        if resolved is None:
            returnValue(None)

        # Clear out the setting
        yield self.unit_state.clear_resolved()

        with (yield self.workflow.lock()):
            if (yield self.workflow.get_state()) == "started":
                returnValue(None)
            try:
                log.info("Resolved detected, firing retry transition")
                if resolved["retry"] == RETRY_HOOKS:
                    yield self.workflow.fire_transition_alias("retry_hook")
                else:
                    yield self.workflow.fire_transition_alias("retry")
            except Exception:
                log.exception("Unknown error while transitioning for resolved")

    @inlineCallbacks
    def cb_watch_hook_debug(self, change):
        """Update the hooks to be debugged when the settings change.
        """
        debug = yield self.unit_state.get_hook_debug()
        debug_hooks = debug and debug.get("debug_hooks") or None
        self.executor.set_debug(debug_hooks)

    @inlineCallbacks
    def cb_watch_upgrade_flag(self, change):
        """Update the unit's charm when requested.
        """
        upgrade_flag = yield self.unit_state.get_upgrade_flag()
        if not upgrade_flag:
            log.info("No upgrade flag set.")
            return

        log.info("Upgrade detected")
        # Clear the flag immediately; this means that upgrade requests will
        # be *ignored* by units which are not "started", and will need to be
        # reissued when the units are in acceptable states.
        yield self.unit_state.clear_upgrade_flag()

        new_id = yield self.service_state.get_charm_id()
        old_id = yield self.unit_state.get_charm_id()
        if new_id == old_id:
            log.info("Upgrade ignored: already running latest charm")
            return

        with (yield self.workflow.lock()):
            state = yield self.workflow.get_state()
            if state != "started":
                if upgrade_flag["force"]:
                    yield self.lifecycle.upgrade_charm(
                        fire_hooks=False, force=True)
                    log.info("Forced upgrade complete")
                    return
                log.warning(
                    "Cannot upgrade: unit is in non-started state %s. Reissue "
                    "upgrade command to try again.", state)
                return

            log.info("Starting upgrade")
            if (yield self.workflow.fire_transition("upgrade_charm")):
                log.info("Upgrade complete")
            else:
                log.info("Upgrade failed")

    @inlineCallbacks
    def cb_watch_config_changed(self, change):
        """Trigger hook on configuration change"""
        # Verify it is running
        with (yield self.workflow.lock()):
            current_state = yield self.workflow.get_state()
            log.debug("Configuration Changed")

            if current_state != "started":
                log.debug(
                    "Configuration updated on service in a non-started state")
                returnValue(None)

            yield self.workflow.fire_transition("configure")


if __name__ == '__main__':
    UnitAgent.run()
