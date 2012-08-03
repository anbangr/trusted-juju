import logging
import os
import yaml

from twisted.internet.defer import (
    inlineCallbacks, fail, succeed, Deferred, returnValue)

from juju.hooks.scheduler import HookScheduler
from juju.state.tests.test_service import ServiceStateManagerTestBase


class SomeError(Exception):
    pass


class HookSchedulerTest(ServiceStateManagerTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(HookSchedulerTest, self).setUp()
        self.client = self.get_zookeeper_client()
        self.unit_relation = self.mocker.mock()
        self.executions = []
        self.service = yield self.add_service_from_charm("wordpress")
        self.state_file = self.makeFile()
        self.executor = self.collect_executor
        self._scheduler = None
        self.log_stream = self.capture_logging(
            "hook.scheduler", level=logging.DEBUG)

    @property
    def scheduler(self):
        # Create lazily, so we can create with a state file if we want to,
        # and swap out collect_executor when helpful to do so.
        if self._scheduler is None:
            self._scheduler = HookScheduler(
                self.client, self.executor, self.unit_relation, "",
                "wordpress/0", self.state_file)
        return self._scheduler

    def collect_executor(self, context, change):
        self.executions.append((context, change))
        return True

    def write_single_unit_state(self):
        with open(self.state_file, "w") as f:
            f.write(yaml.dump({
                "context_members": ["u-1"],
                "member_versions": {"u-1": 0},
                "unit_ops": {},
                "clock_units": {},
                "change_queue": [],
                "clock_sequence": 1}))

    def test_add_expanded_modified(self):
        """An add event is expanded to a modified event.
        """
        self.scheduler.cb_change_members([], ["u-1"])
        self.scheduler.run()
        self.assertEqual(len(self.executions), 2)
        self.assertTrue(
            self.executions[-1][1].change_type == 'modified')

    @inlineCallbacks
    def test_add_expanded_on_error(self):
        """If the hook exec for an add fails, its still expanded.
        """
        results = [succeed(False), succeed(True)]
        collected = []

        def executor(context, change):
            res = results[len(collected)]
            collected.append((context, change))
            return res

        self.executor = executor
        self.scheduler.cb_change_members([], ["u-1"])
        sched_done = self.scheduler.run()
        self.assertEqual(len(collected), 1)
        self.assertTrue(
            collected[0][1].change_type == 'joined')
        yield sched_done
        self.assertFalse(self.scheduler.running)
        with open(self.state_file) as f:
            self.assertEquals(yaml.load(f.read()), {
                "context_members": ['u-1'],
                "member_versions": {"u-1": 0},
                "change_queue": [
                    {'change_type': 'joined',
                     'members': ['u-1'], 'unit_name': 'u-1'},
                    {'change_type': 'modified',
                     'members': ['u-1'], 'unit_name': 'u-1'}]})

    @inlineCallbacks
    def test_add_and_immediate_remove_can_ellipse_change(self):
        """A concurrent depart of a unit during the join hook elides expansion.

        Ie. This is the one scenario where a change hook won't be
        executed after a successful join, because the remote side is
        already gone, its of little purpose to execute the modify
        additionally.
        """

        results = [Deferred() for i in range(5)]
        collected = []

        @inlineCallbacks
        def executor(context, change):
            res = yield results[len(collected)]
            collected.append((context, change))
            returnValue(res)

        self.executor = executor
        self.scheduler.cb_change_members([], ["u-1"])
        sched_done = self.scheduler.run()

        self.scheduler.cb_change_members(["u-1"], [])
        self.assertFalse(collected)

        with open(self.state_file) as f:
            self.assertEquals(yaml.load(f.read()), {
                "context_members": [],
                "member_versions": {},
                "change_queue": [
                    {'change_type': 'joined', 'members': ['u-1'],
                     'unit_name': 'u-1'},
                    {'change_type': 'departed', 'members': [],
                     'unit_name': 'u-1'},
                    ]})

        for i in results:
            i.callback(True)

        self.scheduler.stop()
        yield sched_done

        self.assertEqual(
            [(i[1].unit_name, i[1].change_type, (yield i[0].get_members())) \
             for i in collected],
            [("u-1", "joined", ["u-1"]),
             ("u-1", "departed", [])])

    @inlineCallbacks
    def test_hook_error_doesnt_stop_reduction_of_events_in_clock(self):
        """Reduction of events continues even in the face of a hook error.
        """
        results = [succeed(False), succeed(True), succeed(True)]
        collected = []

        def executor(context, change):
            res = results[len(collected)]
            collected.append((context, change))
            return res

        self.executor = executor
        self.scheduler.cb_change_members([], ["u-1"])
        sched_done = self.scheduler.run()
        self.assertEqual(len(collected), 1)
        self.assertTrue(
            collected[0][1].change_type == 'joined')
        yield sched_done
        self.assertFalse(self.scheduler.running)
        self.scheduler.cb_change_members(["u-1"], ["u-2"])

        with open(self.state_file) as f:
            self.assertEquals(yaml.load(f.read()), {
                "context_members": ['u-2'],
                "member_versions": {"u-2": 0},
                "change_queue": [
                    {'change_type': 'joined', 'members': ['u-1'],
                     'unit_name': 'u-1'},
                    {'change_type': 'joined', 'members': ['u-1', 'u-2'],
                     'unit_name': 'u-2'},
                    {'change_type': 'departed', 'members': ['u-2'],
                     'unit_name': 'u-1'}]})

    @inlineCallbacks
    def test_depart_hook_error_membership_affects(self):
        """An error in a remove event should not distort the membership.

        Also verifies restart after error starts with error event.
        """
        yield self.write_single_unit_state()
        results = [
            succeed(True), succeed(False), succeed(True), succeed(True)]
        collected = []

        def executor(context, change):
            res = results[len(collected)]
            collected.append((context, change))
            return res

        self.executor = executor

        self.scheduler.cb_change_members(["u-1"], ["u-2"])
        self.scheduler.run()
        self.assertEqual(
            [(i[1].unit_name, i[1].change_type) for i in collected],
            [("u-2", "joined"), ("u-1", "departed")])
        self.assertFalse(self.scheduler.running)

        with open(self.state_file) as f:
            self.assertEquals(yaml.load(f.read()), {
                "context_members": ['u-2'],
                "member_versions": {"u-2": 0},
                "change_queue": [
                    {'change_type': 'departed',
                     'members': ['u-2'],
                     'unit_name': 'u-1'},
                    {'change_type': 'modified',
                     'members': ['u-2'],
                     'unit_name': 'u-2'}]})

        self.scheduler.run()

        self.assertEqual(
            [(i[1].unit_name, i[1].change_type, (yield i[0].get_members())) \
             for i in collected],
            [("u-2", "joined", ["u-1", "u-2"]),
             ("u-1", "departed", ["u-2"]),
             ("u-1", "departed", ["u-2"]),
             ("u-2", "modified", ["u-2"])])

    @inlineCallbacks
    def test_restart_after_error_and_pop_starts_with_next_event(self):
        """If a hook errors, the schedule is popped, the next hook is new.
        """
        yield self.write_single_unit_state()
        results = [
            succeed(True), succeed(False), succeed(True), succeed(True)]
        collected = []

        def executor(context, change):
            res = results[len(collected)]
            collected.append((context, change))
            return res

        self.executor = executor

        self.scheduler.cb_change_members(["u-1"], ["u-1", "u-2"])
        self.scheduler.cb_change_settings((("u-1", 2),))

        yield self.scheduler.run()
        self.assertFalse(self.scheduler.running)
        self.assertEqual(
            (yield self.scheduler.pop()),
            {'change_type': 'modified', 'unit_name': 'u-1',
             'members': ['u-1', 'u-2']})

        self.scheduler.run()

        self.assertEqual(
            [(i[1].unit_name, i[1].change_type, (yield i[0].get_members())) \
             for i in collected],
            [("u-2", "joined", ["u-1", "u-2"]),
             ("u-1", "modified", ["u-1", "u-2"]),
             ("u-2", "modified", ["u-1", "u-2"])])

    @inlineCallbacks
    def test_current_unit_op_changes_while_executing(self):
        """The current operation being executed changing during execution is ok
        """
        yield self.write_single_unit_state()

        results = [Deferred() for i in range(5)]
        collected = []

        @inlineCallbacks
        def executor(context, change):
            res = yield results[len(collected)]
            collected.append((context, change))
            returnValue(res)

        self.executor = executor
        self.scheduler.cb_change_settings((("u-1", 2),))
        sched_done = self.scheduler.run()

        self.scheduler.cb_change_members(["u-1"], [])
        self.assertFalse(collected)

        results[0].callback(True)
        self.assertEqual(collected[-1][1].change_type, "modified")
        results[1].callback(True)

        self.scheduler.stop()
        yield sched_done

        self.assertEqual(collected[-1][1].change_type, "departed")

    @inlineCallbacks
    def test_next_unit_op_changes_during_previous_hook_exec(self):
        results = [Deferred() for i in range(5)]
        collected = []

        @inlineCallbacks
        def executor(context, change):
            res = yield results[len(collected)]
            collected.append((context, change))
            returnValue(res)

        self.executor = executor
        self.scheduler.cb_change_members([], ["u-1", "u-2"])
        sched_done = self.scheduler.run()

        self.scheduler.cb_change_members(["u-1", "u-2"], ["u-1"])
        self.assertFalse(collected)

        for i in results:
            i.callback(True)

        self.scheduler.stop()
        yield sched_done

        self.assertEqual(
            [(i[1].unit_name, i[1].change_type, (yield i[0].get_members())) \
             for i in collected],
            [("u-1", "joined", ["u-1"]),
             ("u-1", "modified", ["u-1"])])

        with open(self.state_file) as f:
            self.assertEquals(yaml.load(f.read()), {
                "context_members": ['u-1'],
                "member_versions": {"u-1": 0},
                "change_queue": []})

    # Event reduction/coalescing cases
    def test_reduce_removed_added(self):
        """ A remove event for a node followed by an add event,
        results in a modify event.

        note this isn't possible, unit ids are unique.
        """
        self.write_single_unit_state()
        self.scheduler.cb_change_members(["u-1"], [])
        self.scheduler.cb_change_members([], ["u-1"])
        self.scheduler.run()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.executions[0][1].change_type, "modified")

        output = ("members changed: old=['u-1'], new=[]",
                  "members changed: old=[], new=['u-1']",
                  "start",
                  "executing hook for u-1:modified\n")
        self.assertEqual(self.log_stream.getvalue(), "\n".join(output))

    def test_reduce_modify_remove_add(self):
        """A modify, remove, add event for a node results in a modify.
        An extra validation of the previous test.
        """
        self.write_single_unit_state()
        self.scheduler.cb_change_settings([("u-1", 1)])
        self.scheduler.cb_change_members(["u-1"], [])
        self.scheduler.cb_change_members([], ["u-1"])
        self.scheduler.run()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.executions[0][1].change_type, "modified")

    def test_reduce_add_modify(self):
        """An add and modify event for a node are coalesced to an add."""
        self.scheduler.cb_change_members([], ["u-1"])
        self.scheduler.cb_change_settings([("u-1", 1)])
        self.scheduler.cb_change_settings([("u-1", 1)])
        self.scheduler.run()
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(self.executions[0][1].change_type, "joined")

    def test_reduce_add_remove(self):
        """an add followed by a removal results in a noop."""
        self.scheduler.cb_change_members([], ["u-1"])
        self.scheduler.cb_change_members(["u-1"], [])
        self.scheduler.run()
        self.assertEqual(len(self.executions), 0)

    def test_reduce_modify_remove(self):
        """Modifying and then removing a node, results in just the removal."""
        self.write_single_unit_state()
        self.scheduler.cb_change_settings([("u-1", 1)])
        self.scheduler.cb_change_members(["u-1"], [])
        self.scheduler.run()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.executions[0][1].change_type, "departed")

    def test_reduce_modify_modify(self):
        """Multiple modifies get coalesced to a single modify."""
        # simulate normal startup, the first notify will always be the existing
        # membership set.
        self.scheduler.cb_change_members([], ["u-1"])
        self.scheduler.run()
        self.scheduler.stop()
        self.assertEqual(len(self.executions), 2)

        # Now continue the modify/modify reduction.
        self.scheduler.cb_change_settings([("u-1", 1)])
        self.scheduler.cb_change_settings([("u-1", 2)])
        self.scheduler.cb_change_settings([("u-1", 3)])
        self.scheduler.run()

        self.assertEqual(len(self.executions), 3)
        self.assertEqual(self.executions[1][1].change_type, "modified")

    # Other stuff.
    @inlineCallbacks
    def test_start_stop(self):
        self.assertFalse(self.scheduler.running)
        d = self.scheduler.run()
        self.assertTrue(self.scheduler.running)
        # starting multiple times results in an error
        self.assertFailure(self.scheduler.run(), AssertionError)
        self.scheduler.stop()
        self.assertFalse(self.scheduler.running)
        yield d
        # stopping multiple times is not an error
        yield self.scheduler.stop()
        self.assertFalse(self.scheduler.running)

    def test_start_stop_start(self):
        """Stop values should only be honored if the scheduler is stopped.
        """
        waits = [Deferred(), succeed(True), succeed(True), succeed(True)]
        results = []

        @inlineCallbacks
        def executor(context, change):
            res = yield waits[len(results)]
            results.append((context, change))
            returnValue(res)

        scheduler = HookScheduler(
            self.client, executor,
            self.unit_relation, "", "wordpress/0", self.state_file)

        # Start the scheduler
        d = scheduler.run()

        # Now queue up some changes.
        scheduler.cb_change_members([], ["u-1"])
        scheduler.cb_change_members(["u-1"], ["u-1", "u-2"])

        # Stop the scheduler
        scheduler.stop()
        yield d
        self.assertFalse(scheduler.running)

        # Finish the hook execution
        waits[0].callback(True)
        d = scheduler.run()
        self.assertTrue(scheduler.running)

        # More changes
        scheduler.cb_change_settings([("u-1", 1)])
        scheduler.cb_change_settings([("u-2", 1)])

        # Scheduler should still be running.
        print self._debug_scheduler()
        print [(r[1].change_type, r[1].unit_name) for r in results]
        self.assertFalse(d.called)
        self.assertEqual(len(results), 4)

    @inlineCallbacks
    def test_run_requires_writable_state(self):
        # Induce lazy creation of scheduler, then break state file
        self.scheduler
        with open(self.state_file, "w"):
            pass
        os.chmod(self.state_file, 0)
        e = yield self.assertFailure(self.scheduler.run(), AssertionError)
        self.assertEquals(str(e), "%s is not writable!" % self.state_file)

    def test_empty_state(self):
        with open(self.state_file, "w") as f:
            f.write(yaml.dump({}))

        # Induce lazy creation to verify it can still survive
        self.scheduler

    @inlineCallbacks
    def test_membership_visibility_per_change(self):
        """Hooks are executed against changes, those changes are
        associated to a temporal timestamp, however the changes
        are scheduled for execution, and the state/time of the
        world may have advanced, to present a logically consistent
        view, we try to guarantee at a minimum, that hooks will
        always see the membership of a relation as it was at the
        time of their associated change. In conjunction with the
        event reduction, this keeps a consistent but up to date
        world view.
        """
        self.scheduler.cb_change_members([], ["u-1", "u-2"])
        self.scheduler.cb_change_members(["u-1", "u-2"], ["u-2", "u-3"])
        self.scheduler.cb_change_settings([("u-2", 1)])

        self.scheduler.run()
        self.scheduler.stop()
        # two reduced events, resulting u-2, u-3 add, two expanded
        # u-2, u-3 modified
        #self.assertEqual(len(self.executions), 4)
        self.assertEqual(
            [(i[1].unit_name, i[1].change_type, (yield i[0].get_members())) \
             for i in self.executions],
            [("u-2", "joined", ["u-2"]),
             ("u-3", "joined", ["u-2", "u-3"]),
             ("u-2", "modified", ["u-2", "u-3"]),
             ("u-3", "modified", ["u-2", "u-3"]),
             ])

        # Now the first execution (u-2 add) should only see members
        # from the time of its change, not the current members. However
        # since u-1 has been subsequently removed, it no longer retains
        # an entry in the membership list.
        change_members = yield self.executions[0][0].get_members()
        self.assertEqual(change_members, ["u-2"])

        self.scheduler.cb_change_settings([("u-2", 2)])
        self.scheduler.cb_change_members(["u-2", "u-3"], ["u-2"])
        self.scheduler.run()

        self.assertEqual(len(self.executions), 6)
        self.assertEqual(self.executions[4][1].change_type, "modified")
        # Verify modify events see the correct membership.
        change_members = yield self.executions[4][0].get_members()
        self.assertEqual(change_members, ["u-2", "u-3"])

    @inlineCallbacks
    def test_membership_visibility_with_change(self):
        """We express a stronger guarantee of the above, namely that
        a hook wont see any 'active' members in a membership list, that
        it hasn't previously been given a notify of before.
        """
        with open(self.state_file, "w") as f:
            f.write(yaml.dump({
                "context_members": ["u-1", "u-2"],
                "member_versions": {"u-1": 0, "u-2": 0},
                "change_queue": []}))

        self.scheduler.cb_change_members(["u-1", "u-2"], ["u-2", "u-3", "u-4"])
        self.scheduler.cb_change_settings([("u-2", 1)])

        self.scheduler.run()
        self.scheduler.stop()

        # add for u-3, u-4, mod for u3, u4, remove for u-1, modify for u-2
        self.assertEqual(len(self.executions), 6)

        # Verify members for each change.
        self.assertEqual(self.executions[0][1].change_type, "joined")
        members = yield self.executions[0][0].get_members()
        self.assertEqual(members, ["u-1", "u-2", "u-3"])

        self.assertEqual(self.executions[1][1].change_type, "joined")
        members = yield self.executions[1][0].get_members()
        self.assertEqual(members, ["u-1", "u-2", "u-3", "u-4"])

        self.assertEqual(self.executions[2][1].change_type, "departed")
        members = yield self.executions[2][0].get_members()
        self.assertEqual(members, ["u-2", "u-3", "u-4"])

        self.assertEqual(self.executions[3][1].change_type, "modified")
        members = yield self.executions[2][0].get_members()
        self.assertEqual(members, ["u-2", "u-3", "u-4"])

        with open(self.state_file) as f:
            state = yaml.load(f.read())
            self.assertEquals(state, {
                "change_queue": [],
                "context_members": ["u-2", "u-3", "u-4"],
                "member_versions": {"u-2": 1, "u-3": 0, "u-4": 0}})

    @inlineCallbacks
    def test_state_is_loaded(self):
        with open(self.state_file, "w") as f:
            f.write(yaml.dump({
                "context_members": ["u-1", "u-2", "u-3"],
                "member_versions": {"u-1": 5, "u-2": 2, "u-3": 0},
                "change_queue": [
                    {'unit_name': 'u-1',
                     'change_type': 'modified',
                     'members': ['u-1', 'u-2']},
                    {'unit_name': 'u-3',
                     'change_type': 'joined',
                     'members': ['u-1', 'u-2', 'u-3']}]}))

        d = self.scheduler.run()
        while len(self.executions) < 2:
            yield self.sleep(0.1)
        self.scheduler.stop()
        yield d

        self.assertEqual(self.executions[0][1].change_type, "modified")
        members = yield self.executions[0][0].get_members()
        self.assertEqual(members, ["u-1", "u-2"])

        self.assertEqual(self.executions[1][1].change_type, "joined")
        members = yield self.executions[1][0].get_members()
        self.assertEqual(members, ["u-1", "u-2", "u-3"])

        with open(self.state_file) as f:
            state = yaml.load(f.read())
        self.assertEquals(state, {
            "context_members": ["u-1", "u-2", "u-3"],
            "member_versions": {"u-1": 5, "u-2": 2, "u-3": 0},
            "change_queue": []})

    def test_state_is_stored(self):
        with open(self.state_file, "w") as f:
            f.write(yaml.dump({
                "context_members": ["u-1", "u-2"],
                "member_versions": {"u-1": 0, "u-2": 2},
                "change_queue": []}))

        self.scheduler.cb_change_members(["u-1", "u-2"], ["u-2", "u-3"])
        self.scheduler.cb_change_settings([("u-2", 3)])

        # Add a stop instruction to the queue, which should *not* be saved.
        self.scheduler.stop()

        with open(self.state_file) as f:
            state = yaml.load(f.read())
            self.assertEquals(state, {
                "context_members": ["u-2", "u-3"],
                "member_versions": {"u-2": 3, "u-3": 0},
                'change_queue': [{'change_type': 'joined',
                                  'members': ['u-1', 'u-2', 'u-3'],
                                  'unit_name': 'u-3'},
                                 {'change_type': 'departed',
                                  'members': ['u-2', 'u-3'],
                                  'unit_name': 'u-1'},
                                 {'change_type': 'modified',
                                  'members': ['u-2', 'u-3'],
                                  'unit_name': 'u-2'}],
                })

    @inlineCallbacks
    def test_state_stored_after_tick(self):

        def execute(context, change):
            self.execute_calls += 1
            if self.execute_calls > 1:
                return fail(SomeError())
            return succeed(True)
        self.execute_calls = 0
        self.executor = execute

        with open(self.state_file, "w") as f:
            f.write(yaml.dump({
            "context_members": ["u-1", "u-2"],
            "member_versions": {"u-1": 1, "u-2": 0, "u-3": 0},
            "change_queue": [
                    {"unit_name": "u-1", "change_type": "modified",
                     "members": ["u-1", "u-2"]},
                    {"unit_name": "u-3", "change_type": "added",
                     "members": ["u-1", "u-2", "u-3"]}]}))

        d = self.scheduler.run()
        while self.execute_calls < 2:
            yield self.poke_zk()
        yield self.assertFailure(d, SomeError)
        with open(self.state_file) as f:
            self.assertEquals(yaml.load(f.read()), {
                "context_members": ["u-1", "u-2"],
                "member_versions": {"u-1": 1, "u-2": 0, "u-3": 0},
                "change_queue": [
                    {"unit_name": "u-3", "change_type": "added",
                     "members": ["u-1", "u-2", "u-3"]}]})

    @inlineCallbacks
    def test_state_not_stored_mid_tick(self):

        def execute(context, change):
            self.execute_called = True
            return fail(SomeError())
        self.execute_called = False
        self.executor = execute

        initial_state = {
            "context_members": ["u-1", "u-2"],
            "member_versions": {"u-1": 1, "u-2": 0, "u-3": 0},
            "change_queue": [
                {"change_type": "modified", "unit_name": "u-1",
                 "members":["u-1", "u-2"]},
                {"change_type": "modified", "unit_name": "u-1",
                 "members":["u-1", "u-2", "u-3"]},
                ]}
        with open(self.state_file, "w") as f:
            f.write(yaml.dump(initial_state))

        d = self.scheduler.run()
        while not self.execute_called:
            yield self.poke_zk()
        yield self.assertFailure(d, SomeError)
        with open(self.state_file) as f:
            self.assertEquals(yaml.load(f.read()), initial_state)

    def test_ignore_equal_settings_version(self):
        """
        A modified event whose version is not greater than the latest known
        version for that unit will be ignored.
        """
        self.write_single_unit_state()
        self.scheduler.cb_change_settings([("u-1", 0)])
        self.scheduler.run()
        self.assertEquals(len(self.executions), 0)

    def test_settings_version_0_on_add(self):
        """
        When a unit is added, we assume its settings version to be 0, and
        therefore modified events with version 0 will be ignored.
        """
        self.scheduler.cb_change_members([], ["u-1"])
        self.scheduler.run()
        self.assertEquals(len(self.executions), 2)
        self.scheduler.cb_change_settings([("u-1", 0)])
        self.assertEquals(len(self.executions), 2)
        self.assertEqual(self.executions[0][1].change_type, "joined")

    def test_membership_timeslip(self):
        """
        Adds and removes are calculated based on known membership state, NOT
        on old_units.
        """
        with open(self.state_file, "w") as f:
            f.write(yaml.dump({
            "context_members": ["u-1", "u-2"],
            "member_versions": {"u-1": 0, "u-2": 0},
            "change_queue": []}))

        self.scheduler.cb_change_members(["u-2"], ["u-3", "u-4"])
        self.scheduler.run()

        output = (
            "members changed: old=['u-2'], new=['u-3', 'u-4']",
            "old does not match last recorded units: ['u-1', 'u-2']",
            "start",
            "executing hook for u-3:joined",
            "executing hook for u-4:joined",
            "executing hook for u-1:departed",
            "executing hook for u-2:departed",
            "executing hook for u-3:modified",
            "executing hook for u-4:modified\n")
        self.assertEqual(self.log_stream.getvalue(), "\n".join(output))
