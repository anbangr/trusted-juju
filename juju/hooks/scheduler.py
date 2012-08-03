import logging
import os
import yaml

from twisted.internet.defer import (
    DeferredQueue, inlineCallbacks, succeed, Deferred,
    QueueUnderflow, QueueOverflow)
from juju.state.hook import RelationHookContext, RelationChange


ADDED = "joined"
REMOVED = "departed"
MODIFIED = "modified"


log = logging.getLogger("hook.scheduler")


def check_writeable(path):
    try:
        with open(path, "a"):
            pass
    except IOError:
        raise AssertionError("%s is not writable!" % path)


class HookQueue(DeferredQueue):
    # Single consumer, multi producer LIFO, with Nones treated
    # as FIFO

    def __init__(self, modify_callback):
        self._modify_callback = modify_callback
        super(HookQueue, self).__init__(backlog=1)

    def put(self, change, offset=1):
        """
        LIFO except for a None value which is FIFO

        Add an object to this queue.

        @raise QueueOverflow: Too many objects are in this queue.
        """
        if self.waiting:
            if change is None:
                return self.waiting.pop(0).callback(change)
            # Because there's a waiter we know the offset is 0
            self._queue_change(change, offset=0)
            if self.pending:
                self.waiting.pop(0).callback(self.pending[0])
        elif self.size is None or len(self.pending) < self.size:
            # If the queue is currently processing, no need
            # to store the stop change, it will catch the stop
            # during the loop iter.
            if change is None:
                return
            self._queue_change(change, offset)
        else:
            raise QueueOverflow()

    def next_change(self):
        """Get the next change from the queue"""
        if self.pending:
            return succeed(self.pending[0])
        elif self.backlog is None or len(self.waiting) < self.backlog:
            d = Deferred(canceller=self._cancelGet)
            self.waiting.append(d)
            return d
        else:
            raise QueueUnderflow()

    def finished_change(self):
        """The last change fetched has been processed."""
        if self.pending:
            value = self.pending.pop(0)
            self.cb_modified()
            return value

    def cb_modified(self):
        """Callback invoked by queue when the state has been modified"""
        self._modify_callback()

    def _previous(self, unit_name, pending):
        """Find the most recent previous operation for a unit.

        :param pending: sequence of pending operations to consider.
        """
        for p in reversed(pending):
            if p['unit_name'] == unit_name:
                return pending.index(p), p
        return None, None

    def _wipe_member(self, unit_name, pending):
        """Remove a given unit from membership in pending."""
        for p in pending:
            if unit_name in p['members']:
                p['members'].remove(unit_name)

    def _queue_change(self, change, offset=1):
        """Queue up the node change for execution.

        The process of queuing the change will automatically
        merge with previously queued changes.

        :param change: The change to queue up.
        :param offset: Starting position of any queue merges.
               If the queue is currently being processed, we
               don't attempt to merge with the head of the queue
               as its currently being operated on.
        """
        # Find the previous change if any.
        previous_idx, previous = self._previous(
            change['unit_name'], self.pending[offset:])

        # No previous change, just add
        if previous_idx is None:
            self.pending.append(change)
            return self.cb_modified()

        # Reduce
        change_idx, change_type = self._reduce(
            (previous_idx, previous['change_type']),
            (-1, change['change_type']))

        # Previous change, done.
        if previous_idx == change_idx:
            return

        # New change, remove previous
        elif change_type is not None:
            self.pending.pop(previous_idx)
            change['change_type'] = change_type
            self.pending.append(change)

        # Changes cancelled, remove previous, wipe membership
        elif change_type is None or change_idx != previous_idx:
            assert change['change_type'] == REMOVED
            self._wipe_member(
                change['unit_name'], self.pending[offset:])
            self.pending.pop(previous_idx)

        # Notify changed
        self.cb_modified()

    def _reduce(self, previous, new):
        """Given two change operations for a node, reduce to one operation.

        We depend on zookeeper's total ordering behavior as we don't
        attempt to handle nonsensical operation sequences like
        removed followed by a modified, or modified followed by an
        add.
        """
        previous_clock, previous_change = previous
        new_clock, new_change = new

        if previous_change == REMOVED and new_change == ADDED:
            return (new_clock, MODIFIED)

        elif previous_change == ADDED and new_change == MODIFIED:
            return (previous_clock, previous_change)

        elif previous_change == ADDED and new_change == REMOVED:
            return (None, None)

        elif previous_change == MODIFIED and new_change == REMOVED:
            return (new_clock, new_change)

        elif previous_change == MODIFIED and new_change == MODIFIED:
            return (previous_clock, previous_change)

        elif previous_change == REMOVED and new_change == MODIFIED:
            return (previous_clock, previous_change)


class HookScheduler(object):
    def __init__(self, client, executor, unit_relation, relation_ident,
                 unit_name, state_path):
        self._running = False
        self._state_path = state_path

        # The thing that will actually run the hook for us
        self._executor = executor
        # For hook context construction.
        self._client = client
        self._unit_relation = unit_relation
        self._relation_ident = relation_ident
        self._relation_name = relation_ident.split(":")[0]
        self._unit_name = unit_name
        self._current_clock = None

        if os.path.exists(self._state_path):
            self._load_state()
        else:
            self._create_state()

    def _create_state(self):
        # Current units (as far as the next hook should know)
        self._context_members = None
        # Current units and settings versions (as far as the scheduler knows)
        self._member_versions = {}
        # Run queue (clock)
        self._run_queue = HookQueue(self._save_state)

    def _load_state(self):
        with open(self._state_path) as f:
            state = yaml.load(f.read())
            if not state:
                return self._create_state()
        self._context_members = set(state["context_members"])
        self._member_versions = state["member_versions"]
        self._run_queue = HookQueue(self._save_state)
        self._run_queue.pending = state["change_queue"]

    def _save_state(self):
        state = yaml.dump({
            "context_members": sorted(self._context_members),
            "member_versions": self._member_versions,
            "change_queue": self._run_queue.pending})

        temp_path = self._state_path + "~"
        with open(temp_path, "w") as f:
            f.write(state)
        os.rename(temp_path, self._state_path)

    def _execute(self, change):
        """Execute a hook script for a change.
        """
        # Assemble the change and hook execution context
        rel_change = RelationChange(
            self._relation_ident, change['change_type'], change['unit_name'])
        context = RelationHookContext(
            self._client, self._unit_relation, self._relation_ident,
            change['members'], unit_name=self._unit_name)
        # Execute the change.
        return self._executor(context, rel_change)

    def _get_change(self, unit_name, change_type, members):
        """
        Return a hook context, corresponding to the current state of the
        system.
        """
        return dict(unit_name=unit_name,
                    change_type=change_type,
                    members=sorted(members))

    @property
    def running(self):
        return self._running is True

    @inlineCallbacks
    def run(self):
        assert not self._running, "Scheduler is already running"
        check_writeable(self._state_path)

        self._running = True
        log.debug("start")

        while self._running:
            change = yield self._run_queue.next_change()

            if change is None:
                if not self._running:
                    break
                continue

            log.debug(
                "executing hook for %s:%s",
                change['unit_name'], change['change_type'])

            # Execute the hook
            success = yield self._execute(change)

            # Queue up modified immediately after change.
            if change['change_type'] == ADDED:
                self._run_queue.put(
                    self._get_change(change['unit_name'],
                                     MODIFIED,
                                     self._context_members))

            if success:
                self._run_queue.finished_change()
            else:
                log.debug("hook error, stopping scheduler execution")
                self._running = False
                break
        log.info("stopped")

    def stop(self):
        """Stop the hook execution.

        Note this does not stop the scheduling, the relation watcher
        that feeds changes to the scheduler needs to be stopped to
        achieve that effect.
        """
        log.debug("stopping")
        if not self._running:
            return
        self._running = False
        # Put a marker value onto the queue to designate, stop now.
        # This is in case we're waiting on the queue, when the stop
        # occurs. The queue treats this None specially as a transient
        # value for extant waiters wakeup.
        self._run_queue.put(None)

    def pop(self):
        """Pop the next event on the queue.

        The goal is that on a relation hook error we'll come back up
        and we have the option of retrying the failed hook OR to proceed
        to the next event. To proceed to the next event we pop the failed
        event off the queue.
        """
        assert not self._running, "Scheduler must be stopped for pop()"
        return self._run_queue.finished_change()

    def cb_change_members(self, old_units, new_units):
        """Watch callback invoked when the relation membership changes.
        """
        log.debug("members changed: old=%s, new=%s", old_units, new_units)

        if self._context_members is None:
            self._context_members = set(old_units)

        if set(self._member_versions) != set(old_units):
            # Can happen when we miss seeing some changes ie. disconnected.
            log.debug(
                "old does not match last recorded units: %s",
                sorted(self._member_versions))

        added = set(new_units) - set(self._member_versions)
        removed = set(self._member_versions) - set(new_units)

        self._member_versions.update(dict((unit, 0) for unit in added))
        for unit in removed:
            del self._member_versions[unit]

        for unit_name in sorted(added):
            self._context_members.add(unit_name)
            self._run_queue.put(
                self._get_change(unit_name, ADDED, self._context_members),
                int(self._running))

        for unit_name in sorted(removed):
            self._context_members.remove(unit_name)
            self._run_queue.put(
                self._get_change(unit_name, REMOVED, self._context_members),
                int(self._running))
        self._save_state()

    def cb_change_settings(self, unit_versions):
        """Watch callback invoked when related units change data.
        """
        log.debug("settings changed: %s", unit_versions)
        for (unit_name, version) in unit_versions:
            if version > self._member_versions.get(unit_name, 0):
                self._member_versions[unit_name] = version
                self._run_queue.put(
                    self._get_change(
                        unit_name, MODIFIED, self._context_members),
                    int(self._running))
        self._save_state()
