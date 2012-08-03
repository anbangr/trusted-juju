import StringIO
import itertools
import logging
import os
import shutil
import stat
import sys

import yaml
import zookeeper

from twisted.internet.defer import (inlineCallbacks, Deferred,
                                    fail, returnValue)

from juju.charm.directory import CharmDirectory
from juju.charm.tests.test_repository import unbundled_repository
from juju.charm.url import CharmURL

from juju.control.tests.test_upgrade_charm import CharmUpgradeTestBase
from juju.errors import CharmInvocationError, CharmError, CharmUpgradeError
from juju.hooks.invoker import Invoker
from juju.hooks.executor import HookExecutor
from juju.machine.tests.test_constraints import series_constraints

from juju.state.endpoint import RelationEndpoint
from juju.state.errors import UnitRelationStateNotFound
from juju.state.machine import MachineStateManager
from juju.state.relation import ClientServerUnitWatcher
from juju.state.service import NO_HOOKS
from juju.state.tests.test_relation import RelationTestBase
from juju.state.hook import RelationChange
from juju.unit.lifecycle import (
    UnitLifecycle, UnitRelationLifecycle, RelationInvoker)
from juju.unit.tests.test_charm import CharmPublisherTestBase

from juju.lib.testing import TestCase
from juju.lib.mocker import (ANY, MATCH, Mocker)


class UnwriteablePath(object):

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.mode = os.stat(self.path).st_mode
        os.chmod(self.path, 0000)

    def __exit__(self, *exc_info):
        os.chmod(self.path, self.mode)


class LifecycleTestBase(RelationTestBase):

    juju_directory = None

    @inlineCallbacks
    def setUp(self):
        yield super(LifecycleTestBase, self).setUp()
        if self.juju_directory is None:
            self.juju_directory = self.makeDir()

        self.hook_log = self.capture_logging("hook.output",
                                             level=logging.DEBUG)
        self.agent_log = self.capture_logging("unit-agent",
                                              level=logging.DEBUG)
        self.executor = HookExecutor()
        self.executor.start()
        self.change_environment(
            PATH=os.environ["PATH"],
            JUJU_UNIT_NAME="service-unit/0")

    @inlineCallbacks
    def setup_default_test_relation(self):
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "app", "server")
        wordpress_ep = RelationEndpoint(
            "wordpress", "client-server", "db", "client")
        self.states = yield self.add_relation_service_unit_from_endpoints(
            mysql_ep, wordpress_ep)
        self.unit_directory = os.path.join(self.juju_directory,
            "units",
            self.states["unit"].unit_name.replace("/", "-"))
        os.makedirs(os.path.join(self.unit_directory, "charm", "hooks"))
        self.state_directory = os.path.join(self.juju_directory, "state")
        os.makedirs(self.state_directory)

    def frozen_charm(self):
        return UnwriteablePath(os.path.join(self.unit_directory, "charm"))

    def write_hook(self, name, text, no_exec=False, hooks_dir=None):
        if hooks_dir is None:
            hooks_dir = os.path.join(self.unit_directory, "charm", "hooks")
        if not os.path.exists(hooks_dir):
            os.makedirs(hooks_dir)
        hook_path = os.path.join(hooks_dir, name)
        hook_file = open(hook_path, "w")
        hook_file.write(text.strip())
        hook_file.flush()
        hook_file.close()
        if not no_exec:
            os.chmod(hook_path, stat.S_IRWXU)
        return hook_path

    def wait_on_hook(self, name=None, count=None, sequence=(), debug=False,
                     executor=None):
        """Wait on the given named hook to be executed.

        @param: name: if specified only one hook name can be waited on
        at a given time.

        @param: count: Multiples of the same name can be captured by specifying
        the count parameter.

        @param: sequence: A list of hook names executed in sequence to
        be waited on

        @param: debug: This parameter enables debug stdout loogging.

        @param: executor: A HookExecutor instance to use instead of the default
        """
        d = Deferred()
        results = []
        assert name is not None or sequence, "Hook match must be specified"

        def observer(hook_path):
            hook_name = os.path.basename(hook_path)
            results.append(hook_name)
            if debug:
                print "-> exec hook", hook_name
            if d.called:
                return
            if results == sequence:
                d.callback(True)
            if hook_name == name and count is None:
                d.callback(True)
            if hook_name == name and results.count(hook_name) == count:
                d.callback(True)

        executor = executor or self.executor
        executor.set_observer(observer)
        return d

    def wait_on_state(self, workflow, state, debug=False):
        state_changed = Deferred()

        def observer(workflow_state, state_variables):
            if debug:
                print " workflow state", state, workflow
            if workflow_state == state:
                state_changed.callback(True)

        workflow.set_observer(observer)
        return state_changed

    def capture_output(self, stdout=True, channels=()):
        """Convience method to capture log output.

        Useful tool for observing interaction between components.
        """
        if stdout:
            output = sys.stdout
        else:
            output = StringIO.StringIO()
        channels = zip(channels, itertools.repeat(0))

        for log_name, indent in itertools.chain((
            ("statemachine", 0),
            ("hook.executor", 2),
            ("hook.scheduler", 1),
            ("unit.deploy", 2),
            ("unit.lifecycle", 1),
            ("unit.relation.watch", 1),
            ("unit.relation.lifecycle", 1)),
            channels):
            formatter = logging.Formatter(
                (" " * indent) + "%(name)s: %(message)s")
            self.capture_logging(
                log_name, level=logging.DEBUG,
                log_file=output, formatter=formatter)
        print
        return output


class LifecycleResolvedTest(LifecycleTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(LifecycleResolvedTest, self).setUp()
        yield self.setup_default_test_relation()
        self.lifecycle = UnitLifecycle(
            self.client, self.states["unit"], self.states["service"],
            self.unit_directory, self.state_directory, self.executor)

    @inlineCallbacks
    def tearDown(self):
        if self.lifecycle.running:
            yield self.lifecycle.stop(fire_hooks=False)
        yield super(LifecycleResolvedTest, self).tearDown()

    @inlineCallbacks
    def wb_test_start_with_relation_errors(self):
        """
        White box testing to ensure that an error when starting the
        lifecycle is propogated appropriately, and that we collect
        all results before returning.
        """
        mock_service = self.mocker.patch(self.lifecycle._service)
        mock_service.watch_relation_states(MATCH(lambda x: callable(x)))
        self.mocker.result(fail(SyntaxError()))

        mock_unit = self.mocker.patch(self.lifecycle._unit)
        mock_unit.watch_relation_resolved(MATCH(lambda x: callable(x)))
        results = []
        wait = Deferred()

        @inlineCallbacks
        def complete(*args):
            yield wait
            results.append(True)
            returnValue(True)

        self.mocker.call(complete)
        self.mocker.replay()

        # Start the unit, assert a failure, and capture the deferred
        wait_failure = self.assertFailure(self.lifecycle.start(), SyntaxError)

        # Verify we have no results for the second callback or the start call
        self.assertFalse(results)
        self.assertFalse(wait_failure.called)

        # Let the second callback complete
        wait.callback(True)

        # Wait for the start error to bubble up.
        yield wait_failure

        # Verify the second deferred was waited on.
        self.assertTrue(results)

    @inlineCallbacks
    def test_resolved_relation_watch_unit_lifecycle_not_running(self):
        """If the unit is not running then no relation resolving is performed.
        However the resolution value remains the same.
        """
        # Start the unit.
        yield self.lifecycle.start()

        # Simulate relation down on an individual unit relation
        workflow = self.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        self.assertEqual("up", (yield workflow.get_state()))

        with (yield workflow.lock()):
            yield workflow.transition_state("down")
        resolved = self.wait_on_state(workflow, "up")

        # Stop the unit lifecycle
        yield self.lifecycle.stop()

        # Set the relation to resolved
        yield self.states["unit"].set_relation_resolved(
            {self.states["unit_relation"].internal_relation_id: NO_HOOKS})

        # Ensure we didn't attempt a transition.
        yield self.sleep(0.1)
        self.assertFalse(resolved.called)
        self.assertEqual(
            {self.states["unit_relation"].internal_relation_id: NO_HOOKS},
            (yield self.states["unit"].get_relation_resolved()))

    @inlineCallbacks
    def test_resolved_relation_watch_relation_up(self):
        """If a relation marked as to be resolved is already running,
        then no work is performed.
        """
        # Start the unit.
        yield self.lifecycle.start()

        # get a hold of the unit relation and verify state
        workflow = self.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        self.assertEqual("up", (yield workflow.get_state()))

        # Set the relation to resolved
        yield self.states["unit"].set_relation_resolved(
            {self.states["unit_relation"].internal_relation_id: NO_HOOKS})

        # Give a moment for the watch to fire, invoke callback, and reset.
        yield self.sleep(0.1)

        # Ensure we're still up and the relation resolved setting has been
        # cleared.
        self.assertEqual(
            None, (yield self.states["unit"].get_relation_resolved()))
        self.assertEqual("up", (yield workflow.get_state()))

    @inlineCallbacks
    def test_resolved_relation_watch_from_error(self):
        """Unit lifecycle's will process a unit relation resolved
        setting, and transition a down relation back to a running
        state.
        """
        log_output = self.capture_logging(
            "unit.lifecycle", level=logging.DEBUG)

        # Start the unit.
        yield self.lifecycle.start()

        # Simulate an error condition
        workflow = self.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        self.assertEqual("up", (yield workflow.get_state()))
        with (yield workflow.lock()):
            yield workflow.fire_transition("error")

        resolved = self.wait_on_state(workflow, "up")

        # Set the relation to resolved
        yield self.states["unit"].set_relation_resolved(
            {self.states["unit_relation"].internal_relation_id: NO_HOOKS})

        # Wait for the relation to come back up
        value = yield self.states["unit"].get_relation_resolved()

        yield resolved

        # Verify state
        value = yield workflow.get_state()
        self.assertEqual(value, "up")

        self.assertIn(
            "processing relation resolved changed", log_output.getvalue())

    @inlineCallbacks
    def test_resolved_relation_watch(self):
        """Unit lifecycle's will process a unit relation resolved
        setting, and transition a down relation back to a running
        state.
        """
        log_output = self.capture_logging(
            "unit.lifecycle", level=logging.DEBUG)

        # Start the unit.
        yield self.lifecycle.start()

        # Simulate an error condition
        workflow = self.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        self.assertEqual("up", (yield workflow.get_state()))
        with (yield workflow.lock()):
            yield workflow.transition_state("down")

        resolved = self.wait_on_state(workflow, "up")

        # Set the relation to resolved
        yield self.states["unit"].set_relation_resolved(
            {self.states["unit_relation"].internal_relation_id: NO_HOOKS})


        # Wait for the relation to come back up
        value = yield self.states["unit"].get_relation_resolved()

        yield resolved

        # Verify state
        value = yield workflow.get_state()
        self.assertEqual(value, "up")

        self.assertIn(
            "processing relation resolved changed", log_output.getvalue())


class UnitLifecycleTest(LifecycleTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(UnitLifecycleTest, self).setUp()
        yield self.setup_default_test_relation()
        self.lifecycle = UnitLifecycle(
            self.client, self.states["unit"], self.states["service"],
            self.unit_directory, self.state_directory, self.executor)

    @inlineCallbacks
    def tearDown(self):
        if self.lifecycle.running:
            yield self.lifecycle.stop(fire_hooks=False)
        yield super(UnitLifecycleTest, self).tearDown()

    @inlineCallbacks
    def test_hook_invocation(self):
        """Verify lifecycle methods invoke corresponding charm hooks.
        """
        # install hook
        file_path = self.makeFile()
        self.write_hook(
            "install",
            '#!/bin/sh\n echo "hello world" > %s' % file_path)

        yield self.lifecycle.install()
        self.assertEqual(open(file_path).read().strip(), "hello world")

        # Start hook
        file_path = self.makeFile()
        self.write_hook(
            "start",
            '#!/bin/sh\n echo "sugarcane" > %s' % file_path)
        yield self.lifecycle.start()
        self.assertEqual(open(file_path).read().strip(), "sugarcane")

        # Stop hook
        file_path = self.makeFile()
        self.write_hook(
            "stop",
            '#!/bin/sh\n echo "siesta" > %s' % file_path)
        yield self.lifecycle.stop()
        self.assertEqual(open(file_path).read().strip(), "siesta")

        # verify the sockets are cleaned up.
        self.assertEqual(os.listdir(self.unit_directory), ["charm"])

    @inlineCallbacks
    def test_start_sans_hook(self):
        """The lifecycle start can be invoked without firing hooks."""
        self.write_hook("start", "#!/bin/sh\n exit 1")
        start_executed = self.wait_on_hook("start")
        yield self.lifecycle.start(fire_hooks=False)
        self.assertFalse(start_executed.called)

    @inlineCallbacks
    def test_stop_sans_hook(self):
        """The lifecycle stop can be invoked without firing hooks."""
        self.write_hook("stop", "#!/bin/sh\n exit 1")
        stop_executed = self.wait_on_hook("stop")
        yield self.lifecycle.start()
        yield self.lifecycle.stop(fire_hooks=False)
        self.assertFalse(stop_executed.called)

    @inlineCallbacks
    def test_install_sans_hook(self):
        """The lifecycle install can be invoked without firing hooks."""
        self.write_hook("install", "#!/bin/sh\n exit 1")
        install_executed = self.wait_on_hook("install")
        yield self.lifecycle.install(fire_hooks=False)
        self.assertFalse(install_executed.called)

    @inlineCallbacks
    def test_running(self):
        self.assertFalse(self.lifecycle.running)
        yield self.lifecycle.install()
        self.assertFalse(self.lifecycle.running)
        yield self.lifecycle.start()
        self.assertTrue(self.lifecycle.running)
        yield self.lifecycle.stop()
        self.assertFalse(self.lifecycle.running)

    def test_hook_error(self):
        """Verify hook execution error, raises an exception."""
        self.write_hook("install", '#!/bin/sh\n exit 1')
        d = self.lifecycle.install()
        return self.failUnlessFailure(d, CharmInvocationError)

    def test_hook_not_executable(self):
        """A hook not executable, raises an exception."""
        self.write_hook("install", '#!/bin/sh\n exit 0', no_exec=True)
        return self.failUnlessFailure(
            self.lifecycle.install(), CharmError)

    def test_hook_not_formatted_correctly(self):
        """Hook execution error, raises an exception."""
        self.write_hook("install", '!/bin/sh\n exit 0')
        return self.failUnlessFailure(
            self.lifecycle.install(), CharmInvocationError)

    def write_start_and_relation_hooks(self, relation_name=None):
        """Write some minimal start, and relation-changed hooks.

        Returns the output file of the relation hook.
        """
        file_path = self.makeFile()
        if relation_name is None:
            relation_name = self.states["service_relation"].relation_name
        self.write_hook("start", ("#!/bin/bash\n" "echo hello"))
        self.write_hook("config-changed", ("#!/bin/bash\n" "echo configure"))
        self.write_hook("stop", ("#!/bin/bash\n" "echo goodbye"))
        self.write_hook(
            "%s-relation-joined" % relation_name,
            ("#!/bin/bash\n" "echo joined >> %s\n" % file_path))
        self.write_hook(
            "%s-relation-changed" % relation_name,
            ("#!/bin/bash\n" "echo changed >> %s\n" % file_path))
        self.write_hook(
            "%s-relation-departed" % relation_name,
            ("#!/bin/bash\n" "echo departed >> %s\n" % file_path))

        self.assertFalse(os.path.exists(file_path))
        return file_path

    @inlineCallbacks
    def test_config_hook_invoked_on_configure(self):
        """Invoke the configure lifecycle method will execute the
        config-changed hook.
        """
        output = self.capture_logging("unit.lifecycle", level=logging.DEBUG)
        # configure hook requires a running unit lifecycle..
        yield self.assertFailure(self.lifecycle.configure(), AssertionError)

        # Config hook
        file_path = self.makeFile()
        self.write_hook(
            "config-changed",
            '#!/bin/sh\n echo "palladium" > %s' % file_path)

        yield self.lifecycle.start()
        yield self.lifecycle.configure()

        self.assertEqual(open(file_path).read().strip(), "palladium")
        self.assertIn("configured unit", output.getvalue())

    @inlineCallbacks
    def test_service_relation_watching(self):
        """When the unit lifecycle is started, the assigned relations
        of the service are watched, with unit relation lifecycles
        created for each.

        Relation hook invocation do not maintain global order or determinism
        across relations. They only maintain ordering and determinism within
        a relation. A shared scheduler across relations would be needed to
        maintain such behavior.
        """
        file_path = self.write_start_and_relation_hooks()
        wordpress1_states = yield self.add_opposite_service_unit(self.states)
        yield self.lifecycle.start()
        yield self.wait_on_hook("app-relation-changed")

        self.assertTrue(os.path.exists(file_path))
        self.assertEqual([x.strip() for x in open(file_path).readlines()],
                         ["joined", "changed"])

        # Queue up our wait condition, of 4 hooks firing
        hooks_complete = self.wait_on_hook(
            sequence=[
                "app-relation-joined",   # joined event fires join hook,
                "app-relation-changed",  # followed by changed hook
                "app-relation-departed"])

        # add another.
        wordpress2_states = yield self.add_opposite_service_unit(
            (yield self.add_relation_service_unit_to_another_endpoint(
                self.states,
                RelationEndpoint(
                    "wordpress-2", "client-server", "db", "client"))))

        # modify one.
        wordpress1_states["unit_relation"].set_data(
            {"hello": "world"})

        # delete one.
        self.client.delete(
            "/relations/%s/client/%s" % (
                wordpress2_states["relation"].internal_id,
                wordpress2_states["unit"].internal_id))

        # verify results, waiting for hooks to complete
        yield hooks_complete
        self.assertEqual(
            set([x.strip() for x in open(file_path).readlines()]),
            set(["joined", "changed", "joined", "changed", "departed"]))

    @inlineCallbacks
    def test_removed_relation_depart(self):
        """
        If a removed relation is detected, the unit relation lifecycle is
        stopped.
        """
        file_path = self.write_start_and_relation_hooks()
        self.write_hook("app-relation-broken", "#!/bin/bash\n echo broken")

        yield self.lifecycle.start()
        wordpress_states = yield self.add_opposite_service_unit(self.states)

        # Wait for the watch and hook to fire.
        yield self.wait_on_hook("app-relation-changed")

        self.assertTrue(os.path.exists(file_path))
        self.assertEqual([x.strip() for x in open(file_path).readlines()],
                         ["joined", "changed"])

        self.assertTrue(self.lifecycle.get_relation_workflow(
            self.states["relation"].internal_id))

        # Remove the relation between mysql and wordpress
        yield self.relation_manager.remove_relation_state(
            self.states["relation"])

        # Wait till the unit relation workflow has been processed the event.
        yield self.wait_on_state(
            self.lifecycle.get_relation_workflow(
                self.states["relation"].internal_id),
            "departed")

        # Modify the unit relation settings, to generate a spurious event.
        yield wordpress_states["unit_relation"].set_data(
            {"hello": "world"})

        # Verify no notice was recieved for the modify before we where stopped.
        self.assertEqual([x.strip() for x in open(file_path).readlines()],
                         ["joined", "changed"])

        # Verify the unit relation lifecycle has been disposed of.
        self.assertRaises(KeyError,
                          self.lifecycle.get_relation_workflow,
                          self.states["relation"].internal_id)

    @inlineCallbacks
    def test_lifecycle_start_stop_starts_relations(self):
        """Starting a stopped lifecycle will restart relation events.
        """
        wordpress1_states = yield self.add_opposite_service_unit(self.states)
        wordpress2_states = yield self.add_opposite_service_unit(
            (yield self.add_relation_service_unit_to_another_endpoint(
                self.states,
                RelationEndpoint(
                    "wordpress-2", "client-server", "db", "client"))))
        yield wordpress1_states['service_relations'][-1].add_unit_state(
            self.states['unit'])
        yield wordpress2_states['service_relations'][-1].add_unit_state(
            self.states['unit'])

        # Start and stop lifecycle
        file_path = self.write_start_and_relation_hooks()
        yield self.lifecycle.start()
        yield self.wait_on_hook("app-relation-changed", count=2)
        self.assertTrue(os.path.exists(file_path))
        yield self.lifecycle.stop()

        ########################################################
        # Add, remove relations, and modify related unit settings.

        # The following isn't enough to trigger a hook notification.
        # yield wordpress1_states["relation"].unassign_service(
        #    wordpress1_states["service"])
        #
        # The removal of the external relation, means we stop getting notifies
        # of it, but the underlying unit agents of the service are responsible
        # for removing their presence nodes within the relationship, which
        # triggers a hook invocation.
        yield self.client.delete("/relations/%s/client/%s" % (
            wordpress1_states["relation"].internal_id,
            wordpress1_states["unit"].internal_id))

        yield wordpress2_states["unit_relation"].set_data(
            {"hello": "world"})

        yield self.add_opposite_service_unit(
            (yield self.add_relation_service_unit_to_another_endpoint(
                self.states,
                RelationEndpoint(
                    "wordpress-3", "client-server", "db", "client"))))

        # Verify no hooks are executed.
        yield self.sleep(0.1)

        res = [x.strip() for x in open(file_path)]
        if ((res != ["joined", "changed", "joined", "changed"])
            and
            (res != ["joined", "joined", "changed", "changed"])):
            self.fail("Invalid join sequence %s" % res)

        # XXX - With scheduler local state recovery, we should get the modify.

        # Start and verify events.
        hooks_executed = self.wait_on_hook(
            sequence=[
                "config-changed",
                "start",
                "app-relation-departed",
                "app-relation-joined",   # joined event fires joined hook,
                "app-relation-changed"   # followed by changed hook
                ])
        yield self.lifecycle.start()
        yield hooks_executed
        res.extend(["departed", "joined", "changed"])
        self.assertEqual([x.strip() for x in open(file_path)],
                         res)

    @inlineCallbacks
    def test_lock_start_stop_watch(self):
        """The lifecycle, internally employs lock to prevent simulatenous
        execution of methods which modify internal state. This allows
        for a long running hook to be called safely, even if the other
        invocations of the lifecycle, the subsequent invocations will
        block till they can acquire the lock.
        """
        self.write_hook("start", "#!/bin/bash\necho start\n")
        self.write_hook("stop", "#!/bin/bash\necho stop\n")
        results = []
        finish_callback = [Deferred() for i in range(4)]

        # Control the speed of hook execution
        original_invoker = Invoker.__call__
        invoker = self.mocker.patch(Invoker)

        @inlineCallbacks
        def long_hook(ctx, hook_path):
            results.append(os.path.basename(hook_path))
            yield finish_callback[len(results) - 1]
            yield original_invoker(ctx, hook_path)

        for i in range(4):
            invoker(
                MATCH(lambda x: x.endswith("start") or x.endswith("stop")))
            self.mocker.call(long_hook, with_object=True)

        self.mocker.replay()

        # Hook execution sequence to match on.
        test_complete = self.wait_on_hook(sequence=["config-changed",
                                                    "start",
                                                    "stop",
                                                    "config-changed",
                                                    "start"])

        # Fire off the lifecycle methods
        execution_callbacks = [self.lifecycle.start(),
                               self.lifecycle.stop(),
                               self.lifecycle.start(),
                               self.lifecycle.stop()]

        self.assertEqual([0, 0, 0, 0],
                         [x.called for x in execution_callbacks])

        # kill the delay on the second
        finish_callback[1].callback(True)
        finish_callback[2].callback(True)

        self.assertEqual([0, 0, 0, 0],
                         [x.called for x in execution_callbacks])

        # let them pass, kill the delay on the first
        finish_callback[0].callback(True)
        yield test_complete

        self.assertEqual([False, True, True, False],
                         [x.called for x in execution_callbacks])

        # Finish the last hook
        finish_callback[3].callback(True)
        yield self.wait_on_hook("stop")

        self.assertEqual([True, True, True, True],
                         [x.called for x in execution_callbacks])

    @inlineCallbacks
    def test_start_stop_relations(self):
        yield self.lifecycle.start()

        # Simulate relation down on an individual unit relation
        workflow = self.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        self.assertEqual("up", (yield workflow.get_state()))

        # Stop the unit lifecycle
        yield self.lifecycle.stop()
        self.assertEqual("down", (yield workflow.get_state()))

        # Start again
        yield self.lifecycle.start()
        self.assertEqual("up", (yield workflow.get_state()))

    @inlineCallbacks
    def test_start_without_relations(self):
        yield self.lifecycle.start()

        # Simulate relation down on an individual unit relation
        workflow = self.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        self.assertEqual("up", (yield workflow.get_state()))
        with (yield workflow.lock()):
            yield workflow.transition_state("down")
        resolved = self.wait_on_state(workflow, "up")

        # Stop the unit lifecycle
        yield self.lifecycle.stop()
        self.assertEqual("down", (yield workflow.get_state()))

        # Start again without start_relations
        yield self.lifecycle.start(start_relations=False)
        self.assertEqual("down", (yield workflow.get_state()))

        # Give a moment for the watch to fire erroneously
        yield self.sleep(0.1)

        # Ensure we didn't attempt a transition.
        self.assertFalse(resolved.called)

    @inlineCallbacks
    def test_stop_without_relations(self):
        yield self.lifecycle.start()

        # Simulate relation down on an individual unit relation
        workflow = self.lifecycle.get_relation_workflow(
            self.states["unit_relation"].internal_relation_id)
        self.assertEqual("up", (yield workflow.get_state()))

        # Stop the unit lifecycle
        yield self.lifecycle.stop(stop_relations=False)
        self.assertEqual("up", (yield workflow.get_state()))

        # Start again without start_relations
        yield self.lifecycle.start(start_relations=False)
        self.assertEqual("up", (yield workflow.get_state()))

    @inlineCallbacks
    def test_remembers_relation_removes(self):
        # Add another relation that *won't* be trashed; there's no way to tell
        # the diference between a running relation that's loaded from disk (as
        # this one should be) and one that's just picked up from the call to
        # watch_relation_states, but this should ensure the tests at least hit
        # the correct path.
        other_states = yield self.add_opposite_service_unit(
            (yield self.add_relation_service_unit_to_another_endpoint(
                self.states,
                RelationEndpoint(
                    "wordpress-2", "client-server", "db", "client"))))

        yield self.lifecycle.start()

        going_workflow = self.lifecycle.get_relation_workflow(
            self.states["service_relations"][0].internal_relation_id)
        staying_workflow = self.lifecycle.get_relation_workflow(
            other_states["service_relations"][0].internal_relation_id)

        # Stop the lifecycle as though the process were being shut down
        yield self.lifecycle.stop(fire_hooks=False, stop_relations=False)
        self.assertEqual("up", (yield going_workflow.get_state()))
        self.assertEqual("up", (yield staying_workflow.get_state()))

        # This lifecycle is not responding to events; while it's not looking,
        # trash one of the relations.
        broken_complete = self.wait_on_hook("app-relation-broken")
        yield self.relation_manager.remove_relation_state(
            self.states["relation"])

        # Check it's really not responding to events.
        yield self.sleep(0.1)
        self.assertFalse(broken_complete.called)

        # Trash the unit relation presence nodes
        yield self.client.close()
        yield self.client.connect()

        # Verify that the unit relation presence nodes have been trashed
        staying_service_relation = other_states["service_relations"][1]
        going_service_relation = self.states["service_relations"][1]
        yield self.assertFailure(
            staying_service_relation.get_unit_state(self.states["unit"]),
            UnitRelationStateNotFound)
        yield self.assertFailure(
            going_service_relation.get_unit_state(self.states["unit"]),
            UnitRelationStateNotFound)

        # Create a new lifecycle with the same params; ie one that doesn't
        # share memory state.
        new_lifecycle = UnitLifecycle(
            self.client, self.states["unit"], self.states["service"],
            self.unit_directory, self.state_directory, self.executor)

        # Demonstrate that state was stored by the first one and picked up
        # by the second one, by showing that "missing" relations still have
        # depart hooks fired despite the second one having no direct knowledge
        # of the departed relation's existence..
        yield new_lifecycle.start(fire_hooks=False, start_relations=False)
        yield broken_complete

        # Verify that the unit relation presence node has been restored...
        yield staying_service_relation.get_unit_state(self.states["unit"])

        # ...but that we didn't create one for the departed relation.
        yield self.assertFailure(
            going_service_relation.get_unit_state(self.states["unit"]),
            UnitRelationStateNotFound)

        # The workflow, which we grabbed earlier from the original lifecycle,
        # should now be "departed" (rather than "up", which it was when we
        # stopped the original lifecycle).
        self.assertEqual("departed", (yield going_workflow.get_state()))
        self.assertEqual("up", (yield staying_workflow.get_state()))
        yield new_lifecycle.stop()


class UnitLifecycleUpgradeTest(
        LifecycleTestBase, CharmPublisherTestBase, CharmUpgradeTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(UnitLifecycleUpgradeTest, self).setUp()
        yield self.setup_default_test_relation()
        self.lifecycle = UnitLifecycle(
            self.client, self.states["unit"], self.states["service"],
            self.unit_directory, self.state_directory, self.executor)

    @inlineCallbacks
    def test_no_actual_upgrade_bad_hook(self):
        self.write_hook("upgrade-charm", "#!/bin/bash\nexit 1\n")
        done = self.wait_on_hook("upgrade-charm")
        yield self.assertFailure(
            self.lifecycle.upgrade_charm(), CharmInvocationError)
        yield done
        self.assertFalse(self.executor.running)

    @inlineCallbacks
    def test_no_actual_upgrade_good_hook(self):
        self.write_hook("upgrade-charm", "#!/bin/bash\nexit 0\n")
        # Ensure we don't actually upgrade
        with self.frozen_charm():
            done = self.wait_on_hook("upgrade-charm")
            yield self.lifecycle.upgrade_charm()
            yield done
            self.assertTrue(self.executor.running)

    @inlineCallbacks
    def test_no_actual_upgrade_dont_fire_hooks(self):
        self.write_hook("upgrade-charm", "#!/bin/bash\nexit 1\n")
        with self.frozen_charm():
            done = self.wait_on_hook("upgrade-charm")
            yield self.lifecycle.upgrade_charm(fire_hooks=False)
            yield self.sleep(0.1)
            self.assertFalse(done.called)

    @inlineCallbacks
    def prepare_real_upgrade(self, hook_exit):
        repo = self.increment_charm(self.charm)
        hooks_dir = os.path.join(repo.path, "series", "mysql", "hooks")
        self.write_hook(
            "upgrade-charm",
            "#!/bin/bash\nexit %s\n" % hook_exit,
            hooks_dir=hooks_dir)
        charm = yield repo.find(CharmURL.parse("local:series/mysql"))
        charm, charm_state = yield self.publish_charm(charm.path)
        yield self.states["service"].set_charm_id(charm_state.id)

    @inlineCallbacks
    def test_full_run_bad_write(self):
        self.capture_logging("charm.upgrade")
        yield self.prepare_real_upgrade(0)
        with self.frozen_charm():
            yield self.assertFailure(
                self.lifecycle.upgrade_charm(), CharmUpgradeError)
            self.assertFalse(self.executor.running)

    @inlineCallbacks
    def test_full_run_bad_hook(self):
        yield self.prepare_real_upgrade(1)
        done = self.wait_on_hook("upgrade-charm")
        yield self.assertFailure(
            self.lifecycle.upgrade_charm(), CharmInvocationError)
        yield done
        self.assertFalse(self.executor.running)

    @inlineCallbacks
    def test_full_run_good_hook(self):
        yield self.prepare_real_upgrade(0)
        done = self.wait_on_hook("upgrade-charm")
        yield self.lifecycle.upgrade_charm()
        yield done
        self.assertTrue(self.executor.running)


class RelationInvokerTest(TestCase):

    def test_relation_invoker_environment(self):
        """Verify the relation invoker has populated the environment as per
        charm specification of hook invocation."""
        self.change_environment(
            PATH=os.environ["PATH"],
            JUJU_UNIT_NAME="service-unit/0")
        change = RelationChange("clients:42", "joined", "s/2")
        unit_hook_path = self.makeDir()
        invoker = RelationInvoker(None, change, "", "", unit_hook_path, None)
        environ = invoker.get_environment()
        self.assertEqual(environ["JUJU_RELATION"], "clients")
        self.assertEqual(environ["JUJU_RELATION_ID"], "clients:42")
        self.assertEqual(environ["JUJU_REMOTE_UNIT"], "s/2")
        self.assertEqual(environ["CHARM_DIR"],
                         os.path.join(unit_hook_path, "charm"))


class UnitRelationLifecycleTest(LifecycleTestBase):

    hook_template = (
        "#!/bin/bash\n"
        "echo %(change_type)s >> %(file_path)s\n"
        "echo JUJU_RELATION=$JUJU_RELATION >> %(file_path)s\n"
        "echo JUJU_RELATION_ID=$JUJU_RELATION_ID >> %(file_path)s\n"
        "echo JUJU_REMOTE_UNIT=$JUJU_REMOTE_UNIT >> %(file_path)s")

    @inlineCallbacks
    def setUp(self):
        yield super(UnitRelationLifecycleTest, self).setUp()
        yield self.setup_default_test_relation()
        self.relation_name = self.states["service_relation"].relation_name
        self.lifecycle = UnitRelationLifecycle(
            self.client,
            self.states["unit"].unit_name,
            self.states["unit_relation"],
            self.states["service_relation"].relation_ident,
            self.unit_directory,
            self.state_directory,
            self.executor)
        self.log_stream = self.capture_logging("unit.relation.lifecycle",
                                               logging.DEBUG)

    @inlineCallbacks
    def tearDown(self):
        yield self.lifecycle.stop()
        yield super(UnitRelationLifecycleTest, self).tearDown()

    @inlineCallbacks
    def test_initial_start_lifecycle_no_related_no_exec(self):
        """
        If there are no related units on startup, the relation joined hook
        is not invoked.
        """
        file_path = self.makeFile()
        self.write_hook(
            "%s-relation-changed" % self.relation_name,
            ("/bin/bash\n" "echo executed >> %s\n" % file_path))
        yield self.lifecycle.start()
        self.assertFalse(os.path.exists(file_path))
        self.assertTrue(self.lifecycle.watching)
        self.assertTrue(self.lifecycle.executing)

    @inlineCallbacks
    def test_stop_can_continue_watching(self):
        """
        """
        file_path = self.makeFile()
        self.write_hook(
            "%s-relation-changed" % self.relation_name,
            ("#!/bin/bash\n" "echo executed >> %s\n" % file_path))
        rel_states = yield self.add_opposite_service_unit(self.states)
        yield self.lifecycle.start()
        self.assertTrue(self.lifecycle.watching)
        self.assertTrue(self.lifecycle.executing)

        yield self.wait_on_hook(
            sequence=["app-relation-joined", "app-relation-changed"])
        changed_executed = self.wait_on_hook("app-relation-changed")
        yield self.lifecycle.stop(stop_watches=False)
        self.assertTrue(self.lifecycle.watching)
        self.assertFalse(self.lifecycle.executing)

        rel_states["unit_relation"].set_data(yaml.dump(dict(hello="world")))
        # Sleep to give an error a chance.
        yield self.sleep(0.1)
        self.assertFalse(changed_executed.called)

        yield self.lifecycle.start(start_watches=False)
        self.assertTrue(self.lifecycle.watching)
        self.assertTrue(self.lifecycle.executing)
        yield changed_executed

    @inlineCallbacks
    def test_join_hook_error(self):
        """Join hook errors don't execute synthetic change hook.

        A related unit membership event is aliased to execute both a
        join hook and change hook, an error executing the join hook,
        stops the execution of the change hook.
        """
        yield self.add_opposite_service_unit(self.states)

        errors = []

        def on_error(change, error):
            assert not errors, "Changed hook fired after join error"
            errors.append((change, error))

        self.lifecycle.set_hook_error_handler(on_error)

        self.write_hook(
            "app-relation-joined", "!/bin/bash\nexit 1")
        self.write_hook(
            "app-relation-changed", "!/bin/bash\nexit 1")

        yield self.lifecycle.start()
        yield self.wait_on_hook("app-relation-joined")
        self.assertEqual(len(errors), 1)
        # Give a chance for the relation-changed hook to erroneously fire.
        yield self.sleep(0.3)

    @inlineCallbacks
    def test_initial_start_lifecycle_with_related(self):
        """
        If there are related units on startup, the relation changed hook
        is invoked.
        """
        yield self.add_opposite_service_unit(self.states)

        file_path = self.makeFile()
        self.write_hook("%s-relation-joined" % self.relation_name,
                        self.hook_template % dict(change_type="joined",
                                                  file_path=file_path))
        self.write_hook("%s-relation-changed" % self.relation_name,
                        self.hook_template % dict(change_type="changed",
                                                  file_path=file_path))

        yield self.lifecycle.start()
        yield self.wait_on_hook(
            sequence=["app-relation-joined", "app-relation-changed"])
        self.assertTrue(os.path.exists(file_path))

        with open(file_path)as f:
            contents = f.read()
        self.assertEqual(contents,
                         ("joined\n"
                          "JUJU_RELATION=app\n"
                          "JUJU_RELATION_ID=app:0\n"
                          "JUJU_REMOTE_UNIT=wordpress/0\n"
                          "changed\n"
                          "JUJU_RELATION=app\n"
                          "JUJU_RELATION_ID=app:0\n"
                          "JUJU_REMOTE_UNIT=wordpress/0\n"))

    @inlineCallbacks
    def test_hooks_executed_during_lifecycle_start_stop_start(self):
        """If the unit relation lifecycle is stopped, hooks will no longer
        be executed."""
        file_path = self.makeFile()
        self.write_hook("%s-relation-joined" % self.relation_name,
                        self.hook_template % dict(change_type="joined",
                                                  file_path=file_path))
        self.write_hook("%s-relation-changed" % self.relation_name,
                        self.hook_template % dict(change_type="changed",
                                                  file_path=file_path))

        # starting is async
        yield self.lifecycle.start()
        self.assertTrue(self.lifecycle.watching)
        self.assertTrue(self.lifecycle.executing)

        # stopping is sync.
        self.lifecycle.stop()
        self.assertFalse(self.lifecycle.watching)
        self.assertFalse(self.lifecycle.executing)

        # Add a related unit.
        yield self.add_opposite_service_unit(self.states)

        # Give a chance for things to go bad
        yield self.sleep(0.1)

        self.assertFalse(os.path.exists(file_path))

        # Now start again
        yield self.lifecycle.start()
        self.assertTrue(self.lifecycle.watching)
        self.assertTrue(self.lifecycle.executing)

        # Verify we get our join event.
        yield self.wait_on_hook("app-relation-changed")

        self.assertTrue(os.path.exists(file_path))

    @inlineCallbacks
    def test_hook_error_handler(self):
        # use an error handler that completes async.
        self.write_hook("app-relation-joined", "#!/bin/bash\nexit 0\n")
        self.write_hook("app-relation-changed", "#!/bin/bash\nexit 1\n")

        results = []
        finish_callback = Deferred()

        @inlineCallbacks
        def error_handler(change, e):
            yield self.client.create(
                "/errors", str(e),
                flags=zookeeper.EPHEMERAL | zookeeper.SEQUENCE)
            results.append((change.change_type, e))
            yield self.lifecycle.stop()
            finish_callback.callback(True)

        self.lifecycle.set_hook_error_handler(error_handler)

        # Add a related unit.
        yield self.add_opposite_service_unit(self.states)

        yield self.lifecycle.start()
        yield finish_callback
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][0], "joined")
        self.assertTrue(isinstance(results[0][1], CharmInvocationError))

        hook_relative_path = "charm/hooks/app-relation-changed"
        output = (
            "started relation:app lifecycle",
            "Executing hook app-relation-joined",
            "Executing hook app-relation-changed",
            "Error in app-relation-changed hook: %s '%s/%s': exit code 1." % (
                "Error processing",
                self.unit_directory, hook_relative_path),
            "Invoked error handler for app-relation-changed hook",
            "stopped relation:app lifecycle\n")
        self.assertEqual(self.log_stream.getvalue(), "\n".join(output))

    @inlineCallbacks
    def test_depart(self):
        """If a relation is departed, the depart hook is executed.
        """
        file_path = self.makeFile()
        self.write_hook("%s-relation-broken" % self.relation_name,
                        self.hook_template % dict(change_type="broken",
                                                  file_path=file_path))

        yield self.lifecycle.start()
        wordpress_states = yield self.add_opposite_service_unit(self.states)
        yield self.wait_on_hook(
            sequence=["app-relation-joined", "app-relation-changed"])
        yield self.lifecycle.stop()
        yield self.relation_manager.remove_relation_state(
            wordpress_states["relation"])
        hook_complete = self.wait_on_hook("app-relation-broken")
        yield self.lifecycle.depart()
        yield hook_complete
        contents = open(file_path).read()
        self.assertEqual(
            contents,
            ("broken\n"
             "JUJU_RELATION=app\n"
             "JUJU_RELATION_ID=app:0\n"
             "JUJU_REMOTE_UNIT=\n"))

    @inlineCallbacks
    def test_lock_start_stop(self):
        """
        The relation lifecycle, internally uses a lock when its interacting
        with zk, and acquires the lock to protct its internal data structures.
        """

        original_method = ClientServerUnitWatcher.start
        watcher = self.mocker.patch(ClientServerUnitWatcher)

        finish_callback = Deferred()

        @inlineCallbacks
        def long_op(*args):
            yield finish_callback
            yield original_method(*args)

        watcher.start()
        self.mocker.call(long_op, with_object=True)
        self.mocker.replay()

        start_complete = self.lifecycle.start()
        stop_complete = self.lifecycle.stop()

        yield self.sleep(0.1)
        self.assertFalse(start_complete.called)
        self.assertFalse(stop_complete.called)
        finish_callback.callback(True)

        yield start_complete
        self.assertTrue(stop_complete.called)

    @inlineCallbacks
    def test_start_scheduler(self):
        yield self.lifecycle.start(start_scheduler=False)
        self.assertTrue(self.lifecycle.watching)
        self.assertFalse(self.lifecycle.executing)
        hooks_complete = self.wait_on_hook(
            sequence=["app-relation-joined", "app-relation-changed"])

        # Watches are firing, but scheduler is not running hooks
        yield self.add_opposite_service_unit(self.states)
        yield self.sleep(0.1)
        self.assertFalse(hooks_complete.called)

        # Shut down everything
        yield self.lifecycle.stop()
        self.assertFalse(self.lifecycle.watching)
        self.assertFalse(self.lifecycle.executing)

        # Start the scheduler only, without the watches
        yield self.lifecycle.start(start_watches=False)
        self.assertFalse(self.lifecycle.watching)
        self.assertTrue(self.lifecycle.executing)

        # The scheduler should run the hooks it queued up earlier
        yield hooks_complete


class SubordinateUnitLifecycleTest(LifecycleTestBase, CharmPublisherTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(SubordinateUnitLifecycleTest, self).setUp()
        self.output = self.capture_logging(level=logging.DEBUG)
        environment = self.config.get_default()
        self.provider = environment.get_machine_provider()
        self.storage = self.provider.get_file_storage()

    @inlineCallbacks
    def get_local_charm(self, charm_name):
        "override get_local_charm to use copy"

        repo = self.makeDir()
        os.rmdir(repo)
        shutil.copytree(unbundled_repository, repo)

        charm_dir = CharmDirectory(
            os.path.join(repo, "series", charm_name))

        charm, charm_state = yield self.publish_charm(charm_dir.path)

        @self.addCleanup
        def rm_repo():
            shutil.rmtree(repo)

        returnValue(charm_state)

    @inlineCallbacks
    def setup_default_subordinate_relation(self):
        mysql_ep = RelationEndpoint("mysql", "juju-info", "juju-info",
                                    "server", "global")
        logging_ep = RelationEndpoint("logging", "juju-info", "juju-info",
                                      "client", "container")

        mysql, my_units = yield self.get_service_and_units_by_charm_name(
            "mysql", 1)
        self.assertFalse((yield mysql.is_subordinate()))

        log, log_units = yield self.get_service_and_units_by_charm_name(
            "logging")
        self.assertTrue((yield log.is_subordinate()))

        # add the relationship so we can create units with  containers
        relation_state, service_states = (yield
            self.relation_manager.add_relation_state(
            mysql_ep, logging_ep))

        mu1 = my_units[0]
        msm = MachineStateManager(self.client)
        m1 = yield msm.add_machine_state(series_constraints)
        yield m1.set_instance_id(str(m1.id))
        yield mu1.assign_to_machine(m1)
        yield my_units[0].set_private_address("10.10.10.42")

        self.unit_directory = os.path.join(self.juju_directory,
            "units",
            mu1.unit_name.replace("/", "-"))

        os.makedirs(os.path.join(self.unit_directory, "charm", "hooks"))
        self.state_directory = os.path.join(self.juju_directory, "state")
        os.makedirs(self.state_directory)

        returnValue(dict(principal=(mu1, mysql),
                         subordinate=[None, log],
                         relation_state=relation_state))

    @inlineCallbacks
    def test_subordinate_lifecycle_start(self):
        state = yield self.setup_default_subordinate_relation()
        unit, service = state["principal"]

        lifecycle = UnitLifecycle(
            self.client, unit, service,
            self.unit_directory, self.state_directory, self.executor)

        test_deferred = Deferred()
        results = []

        def test_complete(unit_name, machine_id, charm_dir):
            results.append([unit_name, machine_id, charm_dir])
            test_deferred.callback(True)

        mock_unit_deploy = self.mocker.patch(lifecycle)
        mock_unit_deploy._do_unit_deploy("logging/0", 0,
                                         MATCH(lambda x: os.path.exists(x)))
        self.mocker.call(test_complete)
        self.mocker.replay()

        yield lifecycle.start()
        yield test_deferred
        # mocker has already verified the call signature, we're happy
