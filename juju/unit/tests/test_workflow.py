import csv
import itertools
import logging
import os
import yaml

from twisted.internet.defer import inlineCallbacks, returnValue

from juju.control.tests.test_upgrade_charm import CharmUpgradeTestBase
from juju.unit.tests.test_charm import CharmPublisherTestBase
from juju.unit.tests.test_lifecycle import LifecycleTestBase

from juju.charm.directory import CharmDirectory
from juju.charm.url import CharmURL
from juju.lib.statemachine import WorkflowState
from juju.unit.lifecycle import UnitLifecycle, UnitRelationLifecycle
from juju.unit.workflow import (
    UnitWorkflowState, RelationWorkflowState, WorkflowStateClient,
    is_unit_running, is_relation_running)


class WorkflowTestBase(LifecycleTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(WorkflowTestBase, self).setUp()
        self.output = self.makeFile()

    @inlineCallbacks
    def assertState(self, workflow, state):
        workflow_state = yield workflow.get_state()
        self.assertEqual(workflow_state, state)

    @inlineCallbacks
    def read_persistent_state(self, unit=None, history_id=None, workflow=None):
        unit = unit or self.states["unit"]
        history_id = history_id or unit.unit_name
        data, stat = yield self.client.get("/units/%s" % unit.internal_id)
        workflow = workflow or self.workflow

        state = open(workflow.state_file_path).read()
        history = open(workflow.state_history_path)
        zk_state = yaml.load(data)["workflow_state"]
        returnValue((yaml.load(state),
                    [yaml.load(r[0]) for r in csv.reader(history)],
                     yaml.load(zk_state[history_id])))

    @inlineCallbacks
    def assert_history(self, expected, **kwargs):
        f_state, history, zk_state = yield self.read_persistent_state(**kwargs)
        self.assertEquals(f_state, zk_state)
        self.assertEquals(f_state, history[-1])
        self.assertEquals(history, expected)

    def assert_history_concise(self, *chunks, **kwargs):
        state = None
        history = []
        for chunk in chunks:
            for transition in chunk[:-1]:
                history.append({
                    "state": state,
                    "state_variables": {},
                    "transition_id": transition})
            state = chunk[-1]
            history.append({"state": state, "state_variables": {}})
        return self.assert_history(history, **kwargs)

    def write_exit_hook(self, name, code=0, hooks_dir=None):
        self.write_hook(
            name,
            "#!/bin/bash\necho %s >> %s\n exit %s" % (name, self.output, code),
            hooks_dir=hooks_dir)


class UnitWorkflowTestBase(WorkflowTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(UnitWorkflowTestBase, self).setUp()
        yield self.setup_default_test_relation()
        self.lifecycle = UnitLifecycle(
            self.client, self.states["unit"], self.states["service"],
            self.unit_directory, self.state_directory, self.executor)
        self.workflow = UnitWorkflowState(
            self.client, self.states["unit"], self.lifecycle,
            self.state_directory)

        self.write_exit_hook("install")
        self.write_exit_hook("start")
        self.write_exit_hook("stop")
        self.write_exit_hook("config-changed")
        self.write_exit_hook("upgrade-charm")

    @inlineCallbacks
    def assert_transition(self, transition, success=True):
        with (yield self.workflow.lock()):
            result = yield self.workflow.fire_transition(transition)
        self.assertEquals(result, success)

    @inlineCallbacks
    def assert_transition_alias(self, transition, success=True):
        with (yield self.workflow.lock()):
            result = yield self.workflow.fire_transition_alias(transition)
        self.assertEquals(result, success)

    @inlineCallbacks
    def assert_state(self, expected):
        actual = yield self.workflow.get_state()
        self.assertEquals(actual, expected)

    def assert_hooks(self, *hooks):
        with open(self.output) as f:
            lines = tuple(l.strip() for l in f)
        self.assertEquals(lines, hooks)


class UnitWorkflowTest(UnitWorkflowTestBase):

    @inlineCallbacks
    def test_install(self):
        yield self.assert_transition("install")
        yield self.assert_state("started")
        self.assert_hooks("install", "config-changed", "start")
        yield self.assert_history_concise(
            ("install", "installed"), ("start", "started"))

    @inlineCallbacks
    def test_install_with_error_and_retry(self):
        """If the install hook fails, the workflow is transition to the
        install_error state. If the install is retried, a success
        transition will take us to the started state.
        """
        self.write_exit_hook("install", 1)

        yield self.assert_transition("install", False)
        yield self.assert_state("install_error")
        yield self.assert_transition("retry_install")
        yield self.assert_state("started")
        self.assert_hooks("install", "config-changed", "start")
        yield self.assert_history_concise(
            ("install", "error_install", "install_error"),
            ("retry_install", "installed"),
            ("start", "started"))

    @inlineCallbacks
    def test_install_error_with_retry_hook(self):
        """If the install hook fails, the workflow is transition to the
        install_error state.
        """
        self.write_exit_hook("install", 1)

        yield self.assert_transition("install", False)
        yield self.assert_state("install_error")
        yield self.assert_transition("retry_install_hook", False)
        yield self.assert_state("install_error")
        self.write_exit_hook("install")
        yield self.assert_transition_alias("retry_hook")
        yield self.assert_state("started")
        self.assert_hooks(
            "install", "install", "install", "config-changed", "start")
        yield self.assert_history_concise(
            ("install", "error_install", "install_error"),
            ("retry_install_hook", "install_error"),
            ("retry_install_hook", "installed"),
            ("start", "started"))

    @inlineCallbacks
    def test_start(self):
        with (yield self.workflow.lock()):
            yield self.workflow.set_state("installed")

        yield self.assert_transition("start")
        yield self.assert_state("started")
        self.assert_hooks("config-changed", "start")
        yield self.assert_history_concise(
            ("installed",), ("start", "started"))

    @inlineCallbacks
    def test_start_with_error(self):
        """Executing the start transition with a hook error, results in the
        workflow going to the start_error state. The start can be retried.
        """
        self.write_exit_hook("start", 1)
        # The install transition succeeded; error from success_transition
        # is ignored in StateMachine
        yield self.assert_transition("install")
        yield self.assert_state("start_error")

        yield self.assert_transition("retry_start")
        yield self.assert_state("started")
        self.assert_hooks("install", "config-changed", "start")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "error_start", "start_error"),
            ("retry_start", "started"))

    @inlineCallbacks
    def test_start_error_with_retry_hook(self):
        """Executing the start transition with a hook error, results in the
        workflow going to the start_error state. The start can be retried.
        """
        self.write_exit_hook("start", 1)
        yield self.assert_transition("install")
        yield self.assert_state("start_error")

        yield self.assert_transition("retry_start_hook", False)
        yield self.assert_state("start_error")
        self.write_exit_hook("start")
        yield self.assert_transition_alias("retry_hook")
        yield self.assert_state("started")
        self.assert_hooks(
            "install", "config-changed", "start", "config-changed", "start",
            "config-changed", "start")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "error_start", "start_error"),
            ("retry_start_hook", "start_error"),
            ("retry_start_hook", "started"))

    @inlineCallbacks
    def test_stop(self):
        """Executing the stop transition, results in the workflow going
        to the down state.
        """
        yield self.assert_transition("install")

        yield self.assert_transition("stop")
        yield self.assert_state("stopped")
        self.assert_hooks("install", "config-changed", "start", "stop")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("stop", "stopped"))

    @inlineCallbacks
    def test_stop_with_error(self):
        self.write_exit_hook("stop", 1)
        yield self.assert_transition("install")

        yield self.assert_transition("stop", False)
        yield self.assert_state("stop_error")
        yield self.assert_transition("retry_stop")
        yield self.assert_state("stopped")
        self.assert_hooks("install", "config-changed", "start", "stop")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("stop", "error_stop", "stop_error"),
            ("retry_stop", "stopped"))

    @inlineCallbacks
    def test_stop_error_with_retry_hook(self):
        self.write_exit_hook("stop", 1)
        yield self.assert_transition("install")

        yield self.assert_transition("stop", False)
        yield self.assert_state("stop_error")
        yield self.assert_transition("retry_stop_hook", False)
        yield self.assert_state("stop_error")
        self.write_exit_hook("stop")
        yield self.assert_transition_alias("retry_hook")
        yield self.assert_state("stopped")
        self.assert_hooks(
            "install", "config-changed", "start", "stop", "stop", "stop")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("stop", "error_stop", "stop_error"),
            ("retry_stop_hook", "stop_error"),
            ("retry_stop_hook", "stopped"))

    @inlineCallbacks
    def test_configure(self):
        """Configuring a unit results in the config-changed hook
        being run.
        """
        yield self.assert_transition("install")

        yield self.assert_transition("configure")
        yield self.assert_state("started")
        self.assert_hooks(
            "install", "config-changed", "start", "config-changed")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("configure", "started"))

    @inlineCallbacks
    def test_configure_error_and_retry(self):
        """An error while configuring, transitions the unit and
        stops the lifecycle."""
        yield self.assert_transition("install")
        self.write_exit_hook("config-changed", 1)

        yield self.assert_transition("configure", False)
        yield self.assert_state("configure_error")
        yield self.assert_transition("retry_configure")
        yield self.assert_state("started")
        self.assert_hooks(
            "install", "config-changed", "start", "config-changed")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("configure", "error_configure", "configure_error"),
            ("retry_configure", "started"))

    @inlineCallbacks
    def test_configure_error_and_retry_hook(self):
        """An error while configuring, transitions the unit and
        stops the lifecycle."""
        yield self.assert_transition("install")
        self.write_exit_hook("config-changed", 1)

        yield self.assert_transition("configure", False)
        yield self.assert_state("configure_error")
        yield self.assert_transition("retry_configure_hook", False)
        yield self.assert_state("configure_error")
        self.write_exit_hook("config-changed")
        yield self.assert_transition_alias("retry_hook")
        yield self.assert_state("started")
        self.assert_hooks(
            "install", "config-changed", "start",
            "config-changed", "config-changed", "config-changed")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("configure", "error_configure", "configure_error"),
            ("retry_configure_hook", "error_retry_configure",
                "configure_error"),
            ("retry_configure_hook", "started"))

    @inlineCallbacks
    def test_is_unit_running(self):
        running, state = yield is_unit_running(
            self.client, self.states["unit"])
        self.assertIdentical(running, False)
        self.assertIdentical(state, None)
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("install")
        running, state = yield is_unit_running(
            self.client, self.states["unit"])
        self.assertIdentical(running, True)
        self.assertEqual(state, "started")
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("stop")
        running, state = yield is_unit_running(
            self.client, self.states["unit"])
        self.assertIdentical(running, False)
        self.assertEqual(state, "stopped")

    @inlineCallbacks
    def test_client_with_no_state(self):
        workflow_client = WorkflowStateClient(self.client, self.states["unit"])
        state = yield workflow_client.get_state()
        self.assertEqual(state, None)

    @inlineCallbacks
    def test_client_with_state(self):
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("install")
        workflow_client = WorkflowStateClient(self.client, self.states["unit"])
        self.assertEqual(
            (yield workflow_client.get_state()), "started")

    @inlineCallbacks
    def test_client_readonly(self):
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("install")
        workflow_client = WorkflowStateClient(
            self.client, self.states["unit"])

        self.assertEqual(
            (yield workflow_client.get_state()), "started")
        with (yield workflow_client.lock()):
            yield self.assertFailure(
                workflow_client.set_state("stopped"), NotImplementedError)
        self.assertEqual(
            (yield workflow_client.get_state()), "started")

    @inlineCallbacks
    def assert_synchronize(self, start_state, state, lifecycle, executor,
                           sync_lifecycle=None, sync_executor=None,
                           start_inflight=None):
        # Handle cases where we expect to be in a different state pre-sync
        # to the final state post-sync.
        if sync_lifecycle is None:
            sync_lifecycle = lifecycle
        if sync_executor is None:
            sync_executor = executor
        super_sync = WorkflowState.synchronize

        @inlineCallbacks
        def check_sync(obj):
            # We don't care about RelationWorkflowState syncing here
            if type(obj) == UnitWorkflowState:
                self.assertEquals(
                    self.lifecycle.running, sync_lifecycle)
                self.assertEquals(
                    self.executor.running, sync_executor)
            yield super_sync(obj)

        all_start_states = itertools.product((True, False), (True, False))
        for initial_lifecycle, initial_executor in all_start_states:
            if initial_executor and not self.executor.running:
                self.executor.start()
            elif not initial_executor and self.executor.running:
                yield self.executor.stop()
            if initial_lifecycle and not self.lifecycle.running:
                yield self.lifecycle.start(fire_hooks=False)
            elif not initial_lifecycle and self.lifecycle.running:
                yield self.lifecycle.stop(fire_hooks=False)
            with (yield self.workflow.lock()):
                yield self.workflow.set_state(start_state)
                yield self.workflow.set_inflight(start_inflight)

                # self.patch is not suitable because we can't unpatch until
                # the end of the test, and we don't really want [many] distinct
                # one-line test_synchronize_foo methods.
                WorkflowState.synchronize = check_sync
                try:
                    yield self.workflow.synchronize(self.executor)
                finally:
                    WorkflowState.synchronize = super_sync

            new_inflight = yield self.workflow.get_inflight()
            self.assertEquals(new_inflight, None)
            new_state = yield self.workflow.get_state()
            self.assertEquals(new_state, state)
            vars = yield self.workflow.get_state_variables()
            self.assertEquals(vars, {})
            self.assertEquals(self.lifecycle.running, lifecycle)
            self.assertEquals(self.executor.running, executor)

    def assert_default_synchronize(self, state):
        return self.assert_synchronize(state, state, False, True)

    @inlineCallbacks
    def test_synchronize_automatic(self):
        # No transition in flight
        yield self.assert_synchronize(
            None, "started", True, True, False, True)
        yield self.assert_synchronize(
            "installed", "started", True, True, False, True)
        yield self.assert_synchronize(
            "started", "started", True, True)
        yield self.assert_synchronize(
            "charm_upgrade_error", "charm_upgrade_error", True, False)

    @inlineCallbacks
    def test_synchronize_trivial(self):
        yield self.assert_default_synchronize("install_error")
        yield self.assert_default_synchronize("start_error")
        yield self.assert_default_synchronize("configure_error")
        yield self.assert_default_synchronize("stop_error")
        yield self.assert_default_synchronize("stopped")

    @inlineCallbacks
    def test_synchronize_inflight(self):
        # With transition inflight (we check the important one (upgrade_charm)
        # and a couple of others at random, but testing every single one is
        # entirely redundant).
        yield self.assert_synchronize(
            "started", "started", True, True, True, False, "upgrade_charm")
        yield self.assert_synchronize(
            None, "started", True, True, False, True, "install")
        yield self.assert_synchronize(
            "configure_error", "started", True, True, False, True,
            "retry_configure_hook")


class UnitWorkflowUpgradeTest(
        UnitWorkflowTestBase, CharmPublisherTestBase, CharmUpgradeTestBase):

    expected_upgrade = None

    @inlineCallbacks
    def ready_upgrade(self, bad_hook):
        repository = self.increment_charm(self.charm)
        hooks_dir = os.path.join(repository.path, "series", "mysql", "hooks")
        self.write_exit_hook(
            "upgrade-charm", int(bad_hook), hooks_dir=hooks_dir)

        charm = yield repository.find(CharmURL.parse("local:series/mysql"))
        charm, charm_state = yield self.publish_charm(charm.path)
        yield self.states["service"].set_charm_id(charm_state.id)
        self.expected_upgrade = charm_state.id

    @inlineCallbacks
    def assert_charm_upgraded(self, expect_upgraded):
        charm_id = yield self.states["unit"].get_charm_id()
        self.assertEquals(charm_id == self.expected_upgrade, expect_upgraded)
        if expect_upgraded:
            expect_revision = CharmURL.parse(self.expected_upgrade).revision
            charm = CharmDirectory(os.path.join(self.unit_directory, "charm"))
            self.assertEquals(charm.get_revision(), expect_revision)

    @inlineCallbacks
    def test_upgrade_not_available(self):
        """Upgrading when there's no new version runs the hook anyway"""
        yield self.assert_transition("install")
        yield self.assert_state("started")

        yield self.assert_transition("upgrade_charm")
        yield self.assert_state("started")
        self.assert_hooks(
            "install", "config-changed", "start", "upgrade-charm")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("upgrade_charm", "started"))

    @inlineCallbacks
    def test_upgrade(self):
        """Upgrading a workflow results in the upgrade hook being
        executed.
        """
        yield self.assert_transition("install")
        yield self.assert_state("started")
        yield self.ready_upgrade(False)

        yield self.assert_charm_upgraded(False)
        yield self.assert_transition("upgrade_charm")
        yield self.assert_state("started")
        yield self.assert_charm_upgraded(True)
        self.assert_hooks(
            "install", "config-changed", "start", "upgrade-charm")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("upgrade_charm", "started"))

    @inlineCallbacks
    def test_upgrade_error_retry(self):
        """A hook error during an upgrade transitions to
        upgrade_error.
        """
        yield self.assert_transition("install")
        self.write_exit_hook("upgrade-charm", 1)
        yield self.assert_state("started")
        yield self.ready_upgrade(True)

        yield self.assert_charm_upgraded(False)
        yield self.assert_transition("upgrade_charm", False)
        yield self.assert_state("charm_upgrade_error")
        self.assertFalse(self.executor.running)

        # The upgrade should have completed before the hook blew up.
        yield self.assert_charm_upgraded(True)

        # The bad hook is still in place, but we don't run it again
        yield self.assert_transition("retry_upgrade_charm")
        yield self.assert_state("started")
        yield self.assert_charm_upgraded(True)
        self.assertTrue(self.executor.running)
        self.assert_hooks(
            "install", "config-changed", "start", "upgrade-charm")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("upgrade_charm", "upgrade_charm_error", "charm_upgrade_error"),
            ("retry_upgrade_charm", "started"))

    @inlineCallbacks
    def test_upgrade_error_retry_hook(self):
        """A hook error during an upgrade transitions to
        upgrade_error, and can be re-tried with hook execution.
        """
        yield self.assert_transition("install")
        yield self.assert_state("started")
        yield self.ready_upgrade(True)
        yield self.assert_charm_upgraded(False)
        yield self.assert_transition("upgrade_charm", False)
        yield self.assert_state("charm_upgrade_error")
        self.assertFalse(self.executor.running)

        # The upgrade should have completed before the hook blew up.
        yield self.assert_charm_upgraded(True)

        yield self.assert_transition("retry_upgrade_charm_hook", False)
        yield self.assert_state("charm_upgrade_error")
        self.assertFalse(self.executor.running)
        yield self.assert_charm_upgraded(True)

        self.write_exit_hook("upgrade-charm")
        yield self.assert_transition_alias("retry_hook")
        yield self.assert_state("started")
        self.assertTrue(self.executor.running)
        yield self.assert_charm_upgraded(True)

        self.assert_hooks(
            "install", "config-changed", "start",
            "upgrade-charm", "upgrade-charm", "upgrade-charm")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("upgrade_charm", "upgrade_charm_error", "charm_upgrade_error"),
            ("retry_upgrade_charm_hook", "retry_upgrade_charm_error",
                "charm_upgrade_error"),
            ("retry_upgrade_charm_hook", "started"))

    @inlineCallbacks
    def test_upgrade_error_before_hook(self):
        """If we blow up during the critical pre-hook bits, we should still
        end up in the same error state"""
        self.capture_logging("charm.upgrade")
        yield self.assert_transition("install")
        yield self.assert_state("started")
        yield self.ready_upgrade(False)

        # Induce a surprising error
        with self.frozen_charm():
            yield self.assert_charm_upgraded(False)
            yield self.assert_transition("upgrade_charm", False)
            yield self.assert_state("charm_upgrade_error")
            self.assertFalse(self.executor.running)
            # The upgrade did not complete
            yield self.assert_charm_upgraded(False)

        yield self.assert_transition("retry_upgrade_charm")
        yield self.assert_state("started")
        self.assertTrue(self.executor.running)
        yield self.assert_charm_upgraded(True)

        # The hook must run here, even though it's a retry, because the actual
        # charm only just got overwritten: and so we know that we've never even
        # tried to execute a hook for this upgrade, and we must do so to fulfil
        # the guarantee that that hook runs first after upgrade.

        self.assert_hooks(
            "install", "config-changed", "start", "upgrade-charm")
        yield self.assert_history_concise(
            ("install", "installed"),
            ("start", "started"),
            ("upgrade_charm", "upgrade_charm_error", "charm_upgrade_error"),
            ("retry_upgrade_charm", "started"))


class UnitRelationWorkflowTest(WorkflowTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(UnitRelationWorkflowTest, self).setUp()
        yield self.setup_default_test_relation()
        self.relation_name = self.states["service_relation"].relation_name
        self.relation_ident = self.states["service_relation"].relation_ident
        self.log_stream = self.capture_logging(
            "unit.relation.lifecycle", logging.DEBUG)

        self.lifecycle = UnitRelationLifecycle(
            self.client,
            self.states["unit"].unit_name,
            self.states["unit_relation"],
            self.relation_ident,
            self.unit_directory,
            self.state_directory,
            self.executor)

        self.workflow = RelationWorkflowState(
            self.client, self.states["unit_relation"],
            self.states["unit"].unit_name, self.lifecycle,
            self.state_directory)

    @inlineCallbacks
    def tearDown(self):
        yield self.lifecycle.stop()
        yield super(UnitRelationWorkflowTest, self).tearDown()

    @inlineCallbacks
    def test_is_relation_running(self):
        """The unit relation's workflow state can be categorized as a
        boolean.
        """
        with (yield self.workflow.lock()):
            running, state = yield is_relation_running(
                self.client, self.states["unit_relation"])
            self.assertIdentical(running, False)
            self.assertIdentical(state, None)
            relation_state = self.workflow.get_relation_info()
            self.assertEquals(relation_state,
                              {"relation-0000000000":
                               {"relation_name": "mysql/0",
                                "relation_scope": "global"}})

            yield self.workflow.fire_transition("start")
            running, state = yield is_relation_running(
                self.client, self.states["unit_relation"])
            self.assertIdentical(running, True)
            self.assertEqual(state, "up")

            relation_state = self.workflow.get_relation_info()
            self.assertEquals(relation_state,
                              {"relation-0000000000":
                               {"relation_name": "mysql/0",
                                "relation_scope": "global"}})

            yield self.workflow.fire_transition("stop")
            running, state = yield is_relation_running(
                self.client, self.states["unit_relation"])
            self.assertIdentical(running, False)
            self.assertEqual(state, "down")
            relation_state = self.workflow.get_relation_info()
            self.assertEquals(relation_state,
                              {"relation-0000000000":
                               {"relation_name": "mysql/0",
                                "relation_scope": "global"}})

    @inlineCallbacks
    def test_up_down_cycle(self):
        """The workflow can be transition from up to down, and back.
        """
        self.write_hook("%s-relation-changed" % self.relation_name,
                        "#!/bin/bash\nexit 0\n")

        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
        yield self.assertState(self.workflow, "up")

        hook_executed = self.wait_on_hook("app-relation-changed")

        # Add a new unit, while we're stopped.
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("stop")
        yield self.add_opposite_service_unit(self.states)
        yield self.assertState(self.workflow, "down")
        self.assertFalse(hook_executed.called)

        # Come back up; check unit add detected.
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("restart")
        yield self.assertState(self.workflow, "up")
        yield hook_executed

        self.assert_history_concise(
            ("start", "up"), ("stop", "down"), ("restart", "up"),
            history_id=self.workflow.zk_state_id)

    @inlineCallbacks
    def test_join_hook_with_error(self):
        """A join hook error stops execution of synthetic change hooks.
        """
        self.capture_logging("unit.relation.lifecycle", logging.DEBUG)
        self.write_hook("%s-relation-joined" % self.relation_name,
                        "#!/bin/bash\nexit 1\n")
        self.write_hook("%s-relation-changed" % self.relation_name,
                        "#!/bin/bash\nexit 1\n")

        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
        yield self.assertState(self.workflow, "up")

        # Add a new unit, and wait for the broken hook to result in
        # the transition to the down state.
        yield self.add_opposite_service_unit(self.states)
        yield self.wait_on_state(self.workflow, "error")

        f_state, history, zk_state = yield self.read_persistent_state(
            history_id=self.workflow.zk_state_id)

        self.assertEqual(f_state, zk_state)
        error = "Error processing '%s': exit code 1." % (
            os.path.join(self.unit_directory,
                         "charm", "hooks", "app-relation-joined"))

        self.assertEqual(f_state,
                         {"state": "error",
                          "state_variables": {
                              "change_type": "joined",
                              "error_message": error}})

    @inlineCallbacks
    def test_change_hook_with_error(self):
        """An error while processing a change hook, results
        in the workflow transitioning to the down state.
        """
        self.capture_logging("unit.relation.lifecycle", logging.DEBUG)
        self.write_hook("%s-relation-joined" % self.relation_name,
                        "#!/bin/bash\nexit 0\n")
        self.write_hook("%s-relation-changed" % self.relation_name,
                        "#!/bin/bash\nexit 1\n")

        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
        yield self.assertState(self.workflow, "up")

        # Add a new unit, and wait for the broken hook to result in
        # the transition to the down state.
        yield self.add_opposite_service_unit(self.states)
        yield self.wait_on_state(self.workflow, "error")

        f_state, history, zk_state = yield self.read_persistent_state(
            history_id=self.workflow.zk_state_id)

        self.assertEqual(f_state, zk_state)
        error = "Error processing '%s': exit code 1." % (
            os.path.join(self.unit_directory,
                         "charm", "hooks", "app-relation-changed"))

        self.assertEqual(f_state,
                         {"state": "error",
                          "state_variables": {
                              "change_type": "modified",
                              "error_message": error}})

    @inlineCallbacks
    def test_depart(self):
        """When the workflow is transition to the down state, a relation
        broken hook is executed, and the unit stops responding to relation
        changes.
        """
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
        yield self.assertState(self.workflow, "up")

        wait_on_hook = self.wait_on_hook("app-relation-changed")
        states = yield self.add_opposite_service_unit(self.states)
        yield wait_on_hook

        wait_on_hook = self.wait_on_hook("app-relation-broken")
        wait_on_state = self.wait_on_state(self.workflow, "departed")
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("depart")
        yield wait_on_hook
        yield wait_on_state

        # verify further changes to the related unit don't result in
        # hook executions.
        results = []

        def collect_executions(*args):
            results.append(args)

        self.executor.set_observer(collect_executions)
        yield states["unit_relation"].set_data(dict(a=1))

        # Sleep to give errors a chance.
        yield self.sleep(0.1)
        self.assertFalse(results)

    def test_lifecycle_attribute(self):
        """The workflow lifecycle is accessible from the workflow."""
        self.assertIdentical(self.workflow.lifecycle, self.lifecycle)

    @inlineCallbacks
    def test_client_read_none(self):
        workflow = WorkflowStateClient(
            self.client, self.states["unit_relation"])
        self.assertEqual(None, (yield workflow.get_state()))

    @inlineCallbacks
    def test_client_read_state(self):
        """The relation workflow client can read the state of a unit
        relation."""
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
        yield self.assertState(self.workflow, "up")

        self.write_hook("%s-relation-changed" % self.relation_name,
                        "#!/bin/bash\necho hello\n")
        wait_on_hook = self.wait_on_hook("app-relation-changed")
        yield self.add_opposite_service_unit(self.states)
        yield wait_on_hook

        workflow = WorkflowStateClient(
            self.client, self.states["unit_relation"])
        self.assertEqual("up", (yield workflow.get_state()))

    @inlineCallbacks
    def test_client_read_only(self):
        workflow_client = WorkflowStateClient(
            self.client, self.states["unit_relation"])
        with (yield workflow_client.lock()):
            yield self.assertFailure(
                workflow_client.set_state("up"),
                NotImplementedError)

    @inlineCallbacks
    def assert_synchronize(self, start_state, state, watches, scheduler,
                           sync_watches=None, sync_scheduler=None,
                           start_inflight=None):
        # Handle cases where we expect to be in a different state pre-sync
        # to the final state post-sync.
        if sync_watches is None:
            sync_watches = watches
        if sync_scheduler is None:
            sync_scheduler = scheduler
        super_sync = WorkflowState.synchronize

        @inlineCallbacks
        def check_sync(obj):
            self.assertEquals(
                self.workflow.lifecycle.watching, sync_watches)
            self.assertEquals(
                self.workflow.lifecycle.executing, sync_scheduler)
            yield super_sync(obj)

        start_states = itertools.product((True, False), (True, False))
        for (initial_watches, initial_scheduler) in start_states:
            yield self.workflow.lifecycle.stop()
            yield self.workflow.lifecycle.start(
                start_watches=initial_watches,
                start_scheduler=initial_scheduler)
            self.assertEquals(
                self.workflow.lifecycle.watching, initial_watches)
            self.assertEquals(
                self.workflow.lifecycle.executing, initial_scheduler)
            with (yield self.workflow.lock()):
                yield self.workflow.set_state(start_state)
                yield self.workflow.set_inflight(start_inflight)

                # self.patch is not suitable because we can't unpatch until
                # the end of the test, and we don't really want 13 distinct
                # one-line test_synchronize_foo methods.
                WorkflowState.synchronize = check_sync
                try:
                    yield self.workflow.synchronize()
                finally:
                    WorkflowState.synchronize = super_sync

            new_inflight = yield self.workflow.get_inflight()
            self.assertEquals(new_inflight, None)
            new_state = yield self.workflow.get_state()
            self.assertEquals(new_state, state)
            self.assertEquals(self.workflow.lifecycle.watching, watches)
            self.assertEquals(self.workflow.lifecycle.executing, scheduler)

    @inlineCallbacks
    def test_synchronize(self):
        # No transition in flight
        yield self.assert_synchronize(None, "up", True, True, False, False)
        yield self.assert_synchronize("down", "down", False, False)
        yield self.assert_synchronize("departed", "departed", False, False)
        yield self.assert_synchronize("error", "error", True, False)
        yield self.assert_synchronize("up", "up", True, True)

        # With transition inflight
        yield self.assert_synchronize(
            None, "up", True, True, False, False, "start")
        yield self.assert_synchronize(
            "up", "down", False, False, True, True, "stop")
        yield self.assert_synchronize(
            "down", "up", True, True, False, False, "restart")
        yield self.assert_synchronize(
            "up", "error", True, False, True, True, "error")
        yield self.assert_synchronize(
            "error", "up", True, True, True, False, "reset")
        yield self.assert_synchronize(
            "up", "departed", False, False, True, True, "depart")
        yield self.assert_synchronize(
            "down", "departed", False, False, False, False, "down_depart")
        yield self.assert_synchronize(
            "error", "departed", False, False, True, False, "error_depart")

    @inlineCallbacks
    def test_depart_hook_error(self):
        """A depart hook error, still results in a transition to the
        departed state with a state variable noting the error."""

        self.write_hook("%s-relation-broken" % self.relation_name,
                        "#!/bin/bash\nexit 1\n")
        error_output = self.capture_logging("unit.relation.workflow")

        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
        yield self.assertState(self.workflow, "up")

        wait_on_hook = self.wait_on_hook("app-relation-changed")
        states = yield self.add_opposite_service_unit(self.states)
        yield wait_on_hook

        wait_on_hook = self.wait_on_hook("app-relation-broken")
        wait_on_state = self.wait_on_state(self.workflow, "departed")
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("depart")
        yield wait_on_hook
        yield wait_on_state

        # verify further changes to the related unit don't result in
        # hook executions.
        results = []

        def collect_executions(*args):
            results.append(args)

        self.executor.set_observer(collect_executions)
        yield states["unit_relation"].set_data(dict(a=1))

        # Sleep to give errors a chance.
        yield self.sleep(0.1)
        self.assertFalse(results)

        # Verify final state and log output.
        msg = "Depart hook error, ignoring: "
        error_msg = "Error processing "
        error_msg += repr(os.path.join(
            self.unit_directory, "charm", "hooks",
            "app-relation-broken"))
        error_msg += ": exit code 1."

        self.assertEqual(
            error_output.getvalue(), (msg + error_msg + "\n"))
        current_state = yield self.workflow.get_state()
        self.assertEqual(current_state, "departed")

        f_state, history, zk_state = yield self.read_persistent_state(
            history_id=self.workflow.zk_state_id)

        self.assertEqual(f_state, zk_state)
        self.assertEqual(f_state,
                         {"state": "departed",
                          "state_variables": {
                              "change_type": "depart",
                              "error_message": error_msg}})

    def test_depart_down(self):
        """When the workflow is transitioned from down to departed, a relation
        broken hook is executed, and the unit stops responding to relation
        changes.
        """
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
            yield self.assertState(self.workflow, "up")
            yield self.workflow.fire_transition("stop")
            yield self.assertState(self.workflow, "down")

        states = yield self.add_opposite_service_unit(self.states)
        wait_on_hook = self.wait_on_hook("app-relation-broken")
        wait_on_state = self.wait_on_state(self.workflow, "departed")
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("depart")
        yield wait_on_hook
        yield wait_on_state

        # Verify further changes to the related unit don't result in
        # hook executions.
        results = []

        def collect_executions(*args):
            results.append(args)

        self.executor.set_observer(collect_executions)
        yield states["unit_relation"].set_data(dict(a=1))

        # Sleep to give errors a chance.
        yield self.sleep(0.1)
        self.assertFalse(results)

    def test_depart_error(self):
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("start")
            yield self.assertState(self.workflow, "up")
            yield self.workflow.fire_transition("error")
            yield self.assertState(self.workflow, "error")

        states = yield self.add_opposite_service_unit(self.states)
        wait_on_hook = self.wait_on_hook("app-relation-broken")
        wait_on_state = self.wait_on_state(self.workflow, "departed")
        with (yield self.workflow.lock()):
            yield self.workflow.fire_transition("depart")
        yield wait_on_hook
        yield wait_on_state

        # Verify further changes to the related unit don't result in
        # hook executions.
        results = []

        def collect_executions(*args):
            results.append(args)

        self.executor.set_observer(collect_executions)
        yield states["unit_relation"].set_data(dict(a=1))

        # Sleep to give errors a chance.
        yield self.sleep(0.1)
        self.assertFalse(results)
