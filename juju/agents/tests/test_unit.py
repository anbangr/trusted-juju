import argparse
import logging
import os
import yaml

from twisted.internet.defer import inlineCallbacks, returnValue

from juju.agents.unit import UnitAgent
from juju.agents.base import TwistedOptionNamespace
from juju.charm import get_charm_from_path
from juju.charm.url import CharmURL
from juju.errors import JujuError
from juju.state.environment import GlobalSettingsStateManager
from juju.state.errors import ServiceStateNotFound
from juju.state.service import NO_HOOKS, RETRY_HOOKS
from juju.unit.lifecycle import UnitLifecycle
from juju.unit.workflow import UnitWorkflowState

from juju.agents.tests.common import AgentTestBase
from juju.control.tests.test_upgrade_charm import CharmUpgradeTestBase
from juju.hooks.tests.test_invoker import get_cli_environ_path
from juju.tests.common import get_test_zookeeper_address
from juju.unit.tests.test_charm import CharmPublisherTestBase
from juju.unit.tests.test_workflow import WorkflowTestBase


class UnitAgentTestBase(AgentTestBase, WorkflowTestBase):

    agent_class = UnitAgent

    @inlineCallbacks
    def setUp(self):
        yield super(UnitAgentTestBase, self).setUp()
        settings = GlobalSettingsStateManager(self.client)
        yield settings.set_provider_type("dummy")
        self.change_environment(
            PATH=get_cli_environ_path(),
            JUJU_UNIT_NAME="mysql/0")

    @inlineCallbacks
    def tearDown(self):
        if self.agent.api_socket:
            yield self.agent.api_socket.stopListening()
        yield super(UnitAgentTestBase, self).tearDown()

    @inlineCallbacks
    def get_agent_config(self):
        yield self.setup_default_test_relation()
        options = yield super(UnitAgentTestBase, self).get_agent_config()
        options["unit_name"] = str(self.states["unit"].unit_name)
        returnValue(options)

    def write_empty_hooks(self, start=True, stop=True, install=True, **kw):
        # NB Tests that use this helper method must properly wait on
        # the agent being stopped (yield self.agent.stopService()) to
        # avoid the environment being restored while asynchronously
        # the stop hook continues to execute. Otherwise
        # JUJU_UNIT_NAME, which hook invocation depends on, will
        # not be available and the stop hook will fail (somewhat
        # mysteriously!). The alternative is to set stop=False so that
        # the stop hook will not be created when writing the empty
        # hooks.
        output_file = self.makeFile()

        if install:
            self.write_hook(
                "install", "#!/bin/bash\necho install >> %s" % output_file)
        if start:
            self.write_hook(
                "start", "#!/bin/bash\necho start >> %s" % output_file)
        if stop:
            self.write_hook(
                "stop", "#!/bin/bash\necho stop >> %s" % output_file)

        for k in kw.keys():
            hook_name = k.replace("_", "-")
            self.write_hook(
                hook_name,
                "#!/bin/bash\necho %s >> %s" % (hook_name, output_file))

        return output_file

    def parse_output(self, output_file):
        return filter(None, open(output_file).read().split("\n"))


class UnitAgentTest(UnitAgentTestBase):

    @inlineCallbacks
    def test_agent_start_stop_start_service(self):
        """Verify workflow state when starting and stopping the unit agent."""
        self.write_empty_hooks()
        yield self.agent.startService()
        current_state = yield self.agent.workflow.get_state()
        self.assertEqual(current_state, "started")
        self.assertTrue(self.agent.lifecycle.running)
        self.assertTrue(self.agent.executor.running)
        workflow = self.agent.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        relation_state = yield workflow.get_state()
        self.assertEquals(relation_state, "up")

        yield self.agent.stopService()
        current_state = yield self.agent.workflow.get_state()
        # NOTE: stopping the unit agent does *not* imply that the service
        # should not continue to run; ie don't transition to "stopped", and
        # don't mark the relation states as "down"
        self.assertEqual(current_state, "started")
        self.assertFalse(self.agent.lifecycle.running)
        self.assertFalse(self.agent.executor.running)
        relation_state = yield workflow.get_state()
        self.assertEquals(relation_state, "up")

        # and check we can restart as well
        yield self.agent.startService()
        current_state = yield self.agent.workflow.get_state()
        self.assertEqual(current_state, "started")
        self.assertTrue(self.agent.lifecycle.running)
        self.assertTrue(self.agent.executor.running)
        relation_state = yield workflow.get_state()
        self.assertEquals(relation_state, "up")

        yield self.agent.stopService()
        current_state = yield self.agent.workflow.get_state()
        self.assertEqual(current_state, "started")
        self.assertFalse(self.agent.lifecycle.running)
        self.assertFalse(self.agent.executor.running)
        relation_state = yield workflow.get_state()
        self.assertEquals(relation_state, "up")

    @inlineCallbacks
    def test_agent_start_from_started_workflow(self):
        lifecycle = UnitLifecycle(
            self.client, self.states["unit"], self.states["service"],
            self.unit_directory, self.state_directory, self.executor)
        workflow = UnitWorkflowState(
            self.client, self.states["unit"], lifecycle,
            os.path.join(self.juju_directory, "state"))

        with (yield workflow.lock()):
            yield workflow.fire_transition("install")
        yield lifecycle.stop(fire_hooks=False, stop_relations=False)

        yield self.agent.startService()
        current_state = yield self.agent.workflow.get_state()
        self.assertEqual(current_state, "started")
        self.assertTrue(self.agent.lifecycle.running)
        self.assertTrue(self.agent.executor.running)

    @inlineCallbacks
    def test_agent_start_from_error_workflow(self):
        lifecycle = UnitLifecycle(
            self.client, self.states["unit"], self.states["service"],
            self.unit_directory, self.state_directory, self.executor)
        workflow = UnitWorkflowState(
            self.client, self.states["unit"], lifecycle,
            os.path.join(self.juju_directory, "state"))

        with (yield workflow.lock()):
            yield workflow.fire_transition("install")
            self.write_exit_hook("stop", 1)
            yield workflow.fire_transition("stop")

        yield self.agent.startService()
        current_state = yield self.agent.workflow.get_state()
        self.assertEqual(current_state, "stop_error")
        self.assertFalse(self.agent.lifecycle.running)
        self.assertTrue(self.agent.executor.running)

    def test_agent_unit_name_environment_extraction(self):
        """Verify extraction of unit name from the environment."""
        self.change_args("unit-agent")
        self.change_environment(JUJU_UNIT_NAME="rabbit/1")
        parser = argparse.ArgumentParser()
        self.agent.setup_options(parser)
        options = parser.parse_args(namespace=TwistedOptionNamespace())
        self.assertEqual(options["unit_name"], "rabbit/1")

    def test_agent_unit_name_cli_extraction_error(self):
        """Failure to extract the unit name, results in a nice error message.
        """
        # We don't want JUJU_UNIT_NAME set, so that the expected
        # JujuError will be raised
        self.change_environment(
            PATH=get_cli_environ_path())
        self.change_args(
            "unit-agent",
            "--juju-directory", self.makeDir(),
            "--zookeeper-servers", get_test_zookeeper_address(),
            "--session-file", self.makeFile())

        parser = argparse.ArgumentParser()
        self.agent.setup_options(parser)
        options = parser.parse_args(namespace=TwistedOptionNamespace())

        e = self.assertRaises(JujuError,
                              self.agent.configure,
                              options)
        self.assertEquals(
            str(e),
            "--unit-name must be provided in the command line, or "
            "$JUJU_UNIT_NAME in the environment")

    def test_agent_unit_name_cli_extraction(self):
        """The unit agent can parse its unit-name from the cli.
        """
        self.change_args("unit-agent", "--unit-name", "rabbit/1")
        parser = argparse.ArgumentParser()
        self.agent.setup_options(parser)
        options = parser.parse_args(namespace=TwistedOptionNamespace())
        self.assertEqual(options["unit_name"], "rabbit/1")

    def test_get_agent_name(self):
        self.assertEqual(self.agent.get_agent_name(), "unit:mysql/0")

    def test_agent_invalid_unit_name(self):
        """If the unit agent is given an invalid unit name, an error
        message is raised."""
        options = {}
        options["juju_directory"] = self.juju_directory
        options["zookeeper_servers"] = get_test_zookeeper_address()
        options["session_file"] = self.makeFile()
        options["unit_name"] = "rabbit-1"
        agent = self.agent_class()
        agent.configure(options)
        return self.assertFailure(agent.startService(), ServiceStateNotFound)

    @inlineCallbacks
    def test_agent_records_address_on_startup(self):
        """On startup the agent will record the unit's addresses.
        """
        yield self.agent.startService()
        self.assertEqual(
            (yield self.agent.unit_state.get_public_address()),
            "localhost")
        self.assertEqual(
            (yield self.agent.unit_state.get_private_address()),
            "localhost")

    @inlineCallbacks
    def test_agent_executes_install_and_start_hooks_on_startup(self):
        """On initial startup, the unit agent executes install and start hooks.
        """
        output_file = self.write_empty_hooks()
        hooks_complete = self.wait_on_hook(
            sequence=["install", "config-changed", "start"],
            executor=self.agent.executor)
        yield self.agent.startService()
        # Verify the hook has executed.
        yield hooks_complete
        # config-changed is not mentioned in the output below as the
        # hook is optional and not written by default
        self.assertEqual(self.parse_output(output_file),
                         ["install", "start"])
        yield self.assertState(self.agent.workflow, "started")
        yield self.agent.stopService()

    @inlineCallbacks
    def test_agent_install_error_transitions_install_error(self):
        self.write_hook("install", "!/bin/bash\nexit 1\n")

        hooks_complete = self.wait_on_hook(
            "install",
            executor=self.agent.executor)

        yield self.agent.startService()

        # Verify the hook has executed.
        yield hooks_complete
        yield self.assertState(self.agent.workflow, "install_error")

    @inlineCallbacks
    def test_agent_executes_relation_changed_hook(self):
        """If a relation changes after the unit is started, a relation change
        hook is executed."""
        self.write_empty_hooks()
        file_path = self.makeFile()
        self.write_hook("app-relation-changed",
                ("#!/bin/sh\n"
                 "echo $JUJU_REMOTE_UNIT >> %s\n" % file_path))
        yield self.agent.startService()

        hook_complete = self.wait_on_hook(
            "app-relation-changed", executor=self.agent.executor)
        wordpress_states = yield self.add_opposite_service_unit(
            self.states)

        # Verify the hook has executed.
        yield hook_complete
        self.assertEqual(open(file_path).read().strip(),
                         wordpress_states["unit"].unit_name)

    @inlineCallbacks
    def _test_agent_executes_config_changed_hook(self):
        """Service config changes fire a config-changed hook."""
        self.agent.set_watch_enabled(True)
        self.write_empty_hooks()
        file_path = self.makeFile()
        self.write_hook("config-changed",
                ("#!/bin/sh\n"
                 "config-get foo >> %s\n" % file_path))
        yield self.agent.startService()

        transition_complete = self.wait_on_state(
            self.agent.workflow, "started")

        service = self.states["service"]
        config = yield service.get_config()
        config["foo"] = "bar"
        yield config.write()

        # Verify the hook has executed, and transition has completed.
        yield transition_complete
        self.assertEqual(open(file_path).read().strip(), "bar")

    @inlineCallbacks
    def test_agent_can_execute_config_changed_in_relation_hook(self):
        """Service config changes fire a config-changed hook."""
        self.agent.set_watch_enabled(True)

        self.write_empty_hooks()
        file_path = self.makeFile()

        self.write_hook("app-relation-changed",
                ("#!/bin/sh\n"
                 "config-get foo >> %s\n" % file_path))

        # set service config
        service = self.states["service"]
        config = yield service.get_config()
        config["foo"] = "bar"
        yield config.write()

        yield self.agent.startService()
        hook_complete = self.wait_on_hook(
            "app-relation-changed", executor=self.agent.executor)

        # trigger the hook that will read service options
        yield self.add_opposite_service_unit(self.states)

        # Verify the hook has executed.
        yield hook_complete
        self.assertEqual(open(file_path).read().strip(), "bar")

    @inlineCallbacks
    def test_agent_hook_api_usage(self):
        """If a relation changes after the unit is started, a relation change
        hook is executed."""
        self.write_empty_hooks()
        file_path = self.makeFile()
        self.write_hook("app-relation-changed",
                        "\n".join(
                            ["#!/bin/sh",
                             "echo `relation-list` >> %s" % file_path,
                             "echo `relation-set greeting=hello`",
                             "echo `relation-set planet=earth`",
                             "echo `relation-get planet %s` >> %s" % (
                                 self.states["unit"].unit_name, file_path)]))

        yield self.agent.startService()

        hook_complete = self.wait_on_hook(
            "app-relation-changed", executor=self.agent.executor)
        yield self.add_opposite_service_unit(self.states)

        # Verify the hook has executed.
        yield hook_complete

        # Verify hook output
        output = open(file_path).read().strip().split("\n")
        self.assertEqual(output, ["wordpress/0", "earth"])

        # Verify zookeeper state
        contents = yield self.states["unit_relation"].get_data()
        self.assertEqual(
            {"greeting": "hello", "planet": "earth",
             "private-address": "mysql-0.example.com"},
            yaml.load(contents))

        self.failUnlessIn("wordpress/0", output)

    @inlineCallbacks
    def test_agent_executes_depart_hook(self):
        """If a relation changes after the unit is started, a relation change
        hook is executed."""
        self.write_empty_hooks(app_relation_changed=True)
        file_path = self.makeFile()
        self.write_hook("app-relation-broken",
                ("#!/bin/sh\n"
                 "echo broken hook >> %s\n" % file_path))
        yield self.agent.startService()

        hook_complete = self.wait_on_hook(
            "app-relation-changed", executor=self.agent.executor)

        yield self.add_opposite_service_unit(self.states)
        yield hook_complete

        # Watch the unit relation workflow complete
        workflow_complete = self.wait_on_state(
            self.agent.lifecycle.get_relation_workflow(
                self.states["relation"].internal_id),
            "departed")

        yield self.relation_manager.remove_relation_state(
            self.states["relation"])

        hook_complete = self.wait_on_hook(
            "app-relation-broken", executor=self.agent.executor)

        # Verify the hook has executed.
        yield hook_complete
        self.assertEqual(open(file_path).read().strip(), "broken hook")

        # Wait for the workflow transition to complete.
        yield workflow_complete

    @inlineCallbacks
    def test_agent_debug_watch(self):
        """The unit agent subscribes to changes to the hook debug settings.
        """
        self.agent.set_watch_enabled(True)
        yield self.agent.startService()
        yield self.states["unit"].enable_hook_debug(["*"])
        # Wait for watch to fire invoke callback and reset
        yield self.sleep(0.1)
        # Check the propogation to the executor
        self.assertNotEquals(
            self.agent.executor.get_hook_path("x"), "x")


class UnitAgentResolvedTest(UnitAgentTestBase):

    @inlineCallbacks
    def test_resolved_unit_already_running(self):
        """If the unit already running the setting is cleared,
        and no transition is performed.
        """
        self.write_empty_hooks()
        start_deferred = self.wait_on_hook(
            "start", executor=self.agent.executor)

        self.agent.set_watch_enabled(True)
        yield self.agent.startService()
        yield start_deferred
        self.assertEqual(
            "started", (yield self.agent.workflow.get_state()))

        yield self.agent.unit_state.set_resolved(RETRY_HOOKS)
        # Wait for watch to fire and reset
        yield self.sleep(0.1)

        self.assertEqual(
            "started", (yield self.agent.workflow.get_state()))
        self.assertEqual(
            None, (yield self.agent.unit_state.get_resolved()))

    @inlineCallbacks
    def test_resolved_install_error(self):
        """If the unit has an install error it will automatically
        be transitioned to the installed state after the recovery.
        """
        self.write_empty_hooks()
        install_deferred = self.wait_on_hook(
            "install", executor=self.agent.executor)
        self.write_hook("install", "#!/bin/sh\nexit 1")

        self.agent.set_watch_enabled(True)
        yield self.agent.startService()

        yield install_deferred
        self.assertEqual(
            "install_error", (yield self.agent.workflow.get_state()))

        install_deferred = self.wait_on_state(self.agent.workflow, "started")
        self.write_hook("install", "#!/bin/sh\nexit 0")
        yield self.agent.unit_state.set_resolved(RETRY_HOOKS)
        yield install_deferred
        self.assertEqual("started", (yield self.agent.workflow.get_state()))
        # Ensure we clear out background activity from the watch firing
        yield self.poke_zk()

    @inlineCallbacks
    def test_resolved_start_error(self):
        """If the unit has a start error it will automatically
        be transitioned to started after the recovery.
        """
        self.write_empty_hooks()
        hook_deferred = self.wait_on_hook(
            "start", executor=self.agent.executor)
        self.write_hook("start", "#!/bin/sh\nexit 1")

        self.agent.set_watch_enabled(True)
        yield self.agent.startService()

        yield hook_deferred
        self.assertEqual(
            "start_error", (yield self.agent.workflow.get_state()))

        state_deferred = self.wait_on_state(self.agent.workflow, "started")
        yield self.agent.unit_state.set_resolved(NO_HOOKS)
        yield state_deferred
        self.assertEqual("started", (yield self.agent.workflow.get_state()))
        # Resolving to the started state from the resolved watch will cause the
        # lifecycle start to execute in the background context, wait
        # for it to finish.
        yield self.sleep(0.1)

    @inlineCallbacks
    def test_resolved_stopped(self):
        """If the unit has a stop error it will automatically
        be transitioned to stopped after the recovery.
        """
        self.write_empty_hooks()
        self.write_hook("stop", "#!/bin/sh\nexit 1")

        hook_deferred = self.wait_on_hook(
            "start", executor=self.agent.executor)
        self.agent.set_watch_enabled(True)
        yield self.agent.startService()
        yield hook_deferred

        hook_deferred = self.wait_on_hook("stop", executor=self.agent.executor)
        with (yield self.agent.workflow.lock()):
            yield self.agent.workflow.fire_transition("stop")
        yield hook_deferred

        self.assertEqual("stop_error", (yield self.agent.workflow.get_state()))

        state_deferred = self.wait_on_state(self.agent.workflow, "stopped")
        self.write_hook("stop", "#!/bin/sh\nexit 0")
        yield self.agent.unit_state.set_resolved(RETRY_HOOKS)
        yield state_deferred
        self.assertEqual("stopped", (yield self.agent.workflow.get_state()))
        # Ensure we clear out background activity from the watch firing
        yield self.poke_zk()

    @inlineCallbacks
    def test_hook_error_on_resolved_retry_remains_in_error_state(self):
        """If the unit has an install error it will automatically
        be transitioned to started after the recovery.
        """
        self.write_empty_hooks()
        self.write_hook("stop", "#!/bin/sh\nexit 1")

        hook_deferred = self.wait_on_hook(
            "start", executor=self.agent.executor)
        self.agent.set_watch_enabled(True)
        yield self.agent.startService()
        yield hook_deferred

        hook_deferred = self.wait_on_hook("stop", executor=self.agent.executor)
        with (yield self.agent.workflow.lock()):
            yield self.agent.workflow.fire_transition("stop")
        yield hook_deferred

        self.assertEqual("stop_error", (yield self.agent.workflow.get_state()))

        hook_deferred = self.wait_on_hook("stop", executor=self.agent.executor)
        yield self.agent.unit_state.set_resolved(RETRY_HOOKS)
        yield hook_deferred
        # Ensure we clear out background activity from the watch firing
        yield self.poke_zk()
        self.assertEqual("stop_error", (yield self.agent.workflow.get_state()))


class UnitAgentUpgradeTest(
    UnitAgentTestBase, CharmPublisherTestBase, CharmUpgradeTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(UnitAgentTestBase, self).setUp()
        settings = GlobalSettingsStateManager(self.client)
        yield settings.set_provider_type("dummy")
        self.makeDir(path=os.path.join(self.juju_directory, "charms"))

    @inlineCallbacks
    def wait_for_log(self, logger_name, message, level=logging.DEBUG):
        output = self.capture_logging(logger_name, level=level)
        while message not in output.getvalue():
            yield self.sleep(0.1)

    @inlineCallbacks
    def mark_charm_upgrade(self):
        # Create a new version of the charm
        repository = self.increment_charm(self.charm)

        # Upload the new charm version
        charm = yield repository.find(CharmURL.parse("local:series/mysql"))
        charm, charm_state = yield self.publish_charm(charm.path)

        # Mark the unit for upgrade
        yield self.states["service"].set_charm_id(charm_state.id)
        yield self.states["unit"].set_upgrade_flag()

    @inlineCallbacks
    def test_agent_upgrade_watch(self):
        """The agent watches for unit upgrades."""
        yield self.mark_charm_upgrade()
        self.agent.set_watch_enabled(True)
        hook_done = self.wait_on_hook(
            "upgrade-charm", executor=self.agent.executor)
        yield self.agent.startService()
        yield hook_done
        yield self.assertState(self.agent.workflow, "started")

    @inlineCallbacks
    def test_agent_upgrade(self):
        """The agent can succesfully upgrade its charm."""
        log_written = self.wait_for_log("juju.agents.unit", "Upgrade complete")
        hook_done = self.wait_on_hook(
            "upgrade-charm", executor=self.agent.executor)

        self.agent.set_watch_enabled(True)
        yield self.agent.startService()
        yield self.mark_charm_upgrade()
        yield hook_done
        yield log_written

        self.assertIdentical(
            (yield self.states["unit"].get_upgrade_flag()),
            False)
        new_charm = get_charm_from_path(
            os.path.join(self.agent.unit_directory, "charm"))
        self.assertEqual(
            self.charm.get_revision() + 1, new_charm.get_revision())

    @inlineCallbacks
    def test_agent_upgrade_version_current(self):
        """If the unit is running the latest charm, do nothing."""
        log_written = self.wait_for_log(
            "juju.agents.unit",
            "Upgrade ignored: already running latest charm")

        old_charm_id = yield self.states["unit"].get_charm_id()
        self.agent.set_watch_enabled(True)
        yield self.agent.startService()
        yield self.states["unit"].set_upgrade_flag()
        yield log_written

        self.assertIdentical(
            (yield self.states["unit"].get_upgrade_flag()), False)
        self.assertEquals(
            (yield self.states["unit"].get_charm_id()), old_charm_id)

    @inlineCallbacks
    def test_agent_upgrade_bad_unit_state(self):
        """The upgrade fails if the unit is in a bad state."""
        # Upload a new version of the unit's charm
        repository = self.increment_charm(self.charm)
        charm = yield repository.find(CharmURL.parse("local:series/mysql"))
        charm, charm_state = yield self.publish_charm(charm.path)
        old_charm_id = yield self.states["unit"].get_charm_id()

        log_written = self.wait_for_log(
            "juju.agents.unit",
            "Cannot upgrade: unit is in non-started state configure_error. "
            "Reissue upgrade command to try again.")
        self.agent.set_watch_enabled(True)
        yield self.agent.startService()

        # Mark the unit for upgrade, with an invalid state.
        with (yield self.agent.workflow.lock()):
            yield self.agent.workflow.fire_transition("error_configure")
        yield self.states["service"].set_charm_id(charm_state.id)
        yield self.states["unit"].set_upgrade_flag()
        yield log_written

        self.assertIdentical(
            (yield self.states["unit"].get_upgrade_flag()), False)
        self.assertEquals(
            (yield self.states["unit"].get_charm_id()), old_charm_id)

    @inlineCallbacks
    def test_agent_force_upgrade_bad_unit_state(self):
        """The upgrade runs if forced and the unit is in a bad state."""
        # Upload a new version of the unit's charm
        repository = self.increment_charm(self.charm)
        charm = yield repository.find(CharmURL.parse("local:series/mysql"))
        charm, charm_state = yield self.publish_charm(charm.path)
        old_charm_id = yield self.states["unit"].get_charm_id()

        output = self.capture_logging("juju.agents.unit", level=logging.DEBUG)

        self.agent.set_watch_enabled(True)
        yield self.agent.startService()

        # Mark the unit for upgrade, with an invalid state.
        with (yield self.agent.workflow.lock()):
            yield self.agent.workflow.fire_transition("error_configure")
        yield self.states["service"].set_charm_id(charm_state.id)
        yield self.states["unit"].set_upgrade_flag(force=True)

        # Its hard to watch something with no hooks and no state changes.
        yield self.sleep(0.1)

        self.assertIdentical(
            (yield self.states["unit"].get_upgrade_flag()), False)
        self.assertIn("Forced upgrade complete", output.getvalue())
        self.assertEquals(
            (yield self.states["unit"].get_charm_id()), "local:series/mysql-2")
        self.assertEquals(old_charm_id, "local:series/dummy-1")

    @inlineCallbacks
    def test_agent_upgrade_no_flag(self):
        """An upgrade stops if there is no upgrade flag set."""
        log_written = self.wait_for_log(
            "juju.agents.unit", "No upgrade flag set")
        old_charm_id = yield self.states["unit"].get_charm_id()
        self.agent.set_watch_enabled(True)
        yield self.agent.startService()
        yield log_written

        self.assertIdentical(
            (yield self.states["unit"].get_upgrade_flag()),
            False)
        new_charm_id = yield self.states["unit"].get_charm_id()
        self.assertEquals(new_charm_id, old_charm_id)
