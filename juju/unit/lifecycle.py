import os
import logging
import shutil
import tempfile
import yaml

from twisted.internet.defer import (
    inlineCallbacks, DeferredLock, DeferredList, returnValue)

from juju.errors import CharmUpgradeError
from juju.hooks.invoker import Invoker
from juju.hooks.scheduler import HookScheduler
from juju.state.hook import (
    DepartedRelationHookContext, HookContext, RelationChange)
from juju.state.errors import StopWatcher, UnitRelationStateNotFound
from juju.state.relation import (
    RelationStateManager, UnitRelationState)

from juju.unit.charm import download_charm
from juju.unit.deploy import UnitDeployer
from juju.unit.workflow import RelationWorkflowState


HOOK_SOCKET_FILE = ".juju.hookcli.sock"

hook_log = logging.getLogger("hook.output")

# This is used as `client_id` when constructing Invokers
_EVIL_CONSTANT = "constant"


class _CharmUpgradeOperation(object):
    """Helper class dealing only with the bare mechanics of upgrading"""

    def __init__(self, client, service, unit, unit_dir):
        self._client = client
        self._service = service
        self._unit = unit
        self._old_id = None
        self._new_id = None
        self._download_dir = tempfile.mkdtemp(prefix="tmp-charm-upgrade")
        self._bundle = None
        self._charm_dir = os.path.join(unit_dir, "charm")
        self._log = logging.getLogger("charm.upgrade")

    @inlineCallbacks
    def prepare(self):
        self._log.debug("Checking for newer charm...")
        try:
            self._new_id = yield self._service.get_charm_id()
            self._old_id = yield self._unit.get_charm_id()
            if self._new_id != self._old_id:
                self._log.debug("Downloading %s...", self._new_id)
                self._bundle = yield download_charm(
                    self._client, self._new_id, self._download_dir)
            else:
                self._log.debug("Latest charm is already present.")
        except Exception as e:
            self._log.exception("Charm upgrade preparation failed.")
            raise CharmUpgradeError(str(e))

    @property
    def ready(self):
        return self._bundle is not None

    @inlineCallbacks
    def run(self):
        assert self.ready
        self._log.debug(
            "Replacing charm %s with %s.", self._old_id, self._new_id)
        try:
            # TODO this will leave droppings from the old charm; but we can't
            # delete the whole charm dir and replace it, because some charms
            # store state within their directories. See lp:791035
            self._bundle.extract_to(self._charm_dir)
            self._log.debug(
                "Charm has been upgraded to %s.", self._new_id)

            yield self._unit.set_charm_id(self._new_id)
            self._log.debug("Upgrade recorded.")
        except Exception as e:
            self._log.exception("Charm upgrade failed.")
            raise CharmUpgradeError(str(e))

    def cleanup(self):
        if os.path.exists(self._download_dir):
            shutil.rmtree(self._download_dir)


class UnitLifecycle(object):
    """Manager for a unit lifecycle.

    Primarily used by the workflow interaction, to modify unit behavior
    according to the current unit workflow state and transitions.

    See docs/source/internals/unit-workflow-lifecycle.rst for a brief
    discussion of some of the more interesting implementation decisions.
    """

    def __init__(self, client, unit, service, unit_dir, state_dir, executor):
        self._client = client
        self._unit = unit
        self._service = service
        self._executor = executor
        self._unit_dir = unit_dir
        self._state_dir = state_dir
        self._relations = None
        self._running = False
        self._watching_relation_memberships = False
        self._watching_relation_resolved = False
        self._run_lock = DeferredLock()
        self._log = logging.getLogger("unit.lifecycle")

    @property
    def running(self):
        return self._running

    def get_relation_workflow(self, relation_id):
        """Accessor to a unit relation workflow, by relation id.

        Primarily intended for and used by unit tests. Raises
        a KeyError if the relation workflow does not exist.
        """
        return self._relations[relation_id]

    @inlineCallbacks
    def install(self, fire_hooks=True):
        """Invoke the unit's install hook.
        """
        if fire_hooks:
            yield self._execute_hook("install")

    @inlineCallbacks
    def start(self, fire_hooks=True, start_relations=True):
        """Invoke the start hook, and setup relation watching.

        :param fire_hooks: False to skip running config-change and start hooks.
            Will not affect any relation hooks that happen to be fired as a
            consequence of starting up.

        :param start_relations: True to transition all "down" relation
            workflows to "up".
        """
        self._log.debug("pre-start acquire, running:%s", self._running)
        yield self._run_lock.acquire()
        self._log.debug("start running, unit lifecycle")
        watches = []

        try:
            if fire_hooks:
                yield self._execute_hook("config-changed")
                yield self._execute_hook("start")

            if self._relations is None:
                yield self._load_relations()

            if start_relations:
                # We actually want to transition from "down" to "up" where
                # applicable (ie a stopped unit is starting up again)
                for workflow in self._relations.values():
                    with (yield workflow.lock()):
                        state = yield workflow.get_state()
                        if state == "down":
                            yield workflow.transition_state("up")

            # Establish a watch on the existing relations.
            if not self._watching_relation_memberships:
                self._log.debug("starting service relation watch")
                watches.append(self._service.watch_relation_states(
                    self._on_service_relation_changes))
                self._watching_relation_memberships = True

            # Establish a watch for resolved relations
            if not self._watching_relation_resolved:
                self._log.debug("starting unit relation resolved watch")
                watches.append(self._unit.watch_relation_resolved(
                    self._on_relation_resolved_changes))
                self._watching_relation_resolved = True

            # Set current status
            self._running = True
        finally:
            self._run_lock.release()

        # Give up the run lock before waiting on initial watch invocations.
        results = yield DeferredList(watches, consumeErrors=True)

        # If there's an error reraise the first one found.
        errors = [e[1] for e in results if not e[0]]
        if errors:
            returnValue(errors[0])

        self._log.debug("started unit lifecycle")

    @inlineCallbacks
    def stop(self, fire_hooks=True, stop_relations=True):
        """Stop the unit, executes the stop hook, and stops relation watching.

        :param fire_hooks: False to skip running stop hooks.

        :param stop_relations: True to transition all "up" relation
            workflows to "down"; when False, simply shut down relation
            lifecycles (in preparation for process shutdown, for example).
        """
        self._log.debug("pre-stop acquire, running:%s", self._running)
        yield self._run_lock.acquire()
        try:
            # Verify state
            assert self._running, "Already Stopped"

            if stop_relations:
                # We actually want to transition relation states
                # (probably because the unit workflow state is stopped/error)
                for workflow in self._relations.values():
                    with (yield workflow.lock()):
                        yield workflow.transition_state("down")
            else:
                # We just want to stop the relations from acting
                # (probably because the process is going down)
                self._log.debug("stopping relation lifecycles")
                for workflow in self._relations.values():
                    yield workflow.lifecycle.stop()

            if fire_hooks:
                yield self._execute_hook("stop")

            # Set current status
            self._running = False
        finally:
            self._run_lock.release()
        self._log.debug("stopped unit lifecycle")

    @inlineCallbacks
    def configure(self, fire_hooks=True):
        """Inform the unit that its service config has changed.
        """
        if not fire_hooks:
            returnValue(None)
        yield self._run_lock.acquire()
        try:
            # Verify State
            assert self._running, "Needs to be running."

            # Execute hook
            yield self._execute_hook("config-changed")
        finally:
            self._run_lock.release()
        self._log.debug("configured unit")

    @inlineCallbacks
    def upgrade_charm(self, fire_hooks=True, force=False):
        """Upgrade the charm and invoke the upgrade-charm hook if requested.

        :param fire_hooks: if False, *and* the actual upgrade operation is not
            necessary, skip the upgrade-charm hook. When the actual charm has
            changed during this invocation, this flag is ignored: hooks will
            always be fired.

        :param force: Boolean, if true then we're merely putting the charm into
            place on disk, not executing charm hooks.
        """
        msg = "Upgrading charm"
        if force:
            msg += " - forced"
        self._log.debug(msg)
        upgrade = _CharmUpgradeOperation(
            self._client, self._service, self._unit, self._unit_dir)
        yield self._run_lock.acquire()
        try:
            yield upgrade.prepare()

            # Executor may already be stopped if we're retrying.
            if self._executor.running:
                self._log.debug("Pausing normal hook execution")
                yield self._executor.stop()

            if upgrade.ready:
                yield upgrade.run()
                fire_hooks = True

            if fire_hooks and not force:
                yield self._execute_hook("upgrade-charm", now=True)

            # Always restart executor on success; charm upgrade operations and
            # errors are the only reasons for the executor to be stopped.
            self._log.debug("Resuming normal hook execution.")
            self._executor.start()
        finally:
            self._run_lock.release()
            upgrade.cleanup()

    @inlineCallbacks
    def _on_relation_resolved_changes(self, event):
        """Callback for unit relation resolved watching.

        The callback is invoked whenever the relation resolved
        settings change.
        """
        self._log.debug("relation resolved changed")
        # Acquire the run lock, and process the changes.
        yield self._run_lock.acquire()

        try:
            # If the unit lifecycle isn't running we shouldn't process
            # any relation resolutions.
            if not self._running:
                self._log.debug("stop watch relation resolved changes")
                self._watching_relation_resolved = False
                raise StopWatcher()

            self._log.info("processing relation resolved changed")
            if self._client.connected:
                yield self._process_relation_resolved_changes()
        finally:
            yield self._run_lock.release()

    @inlineCallbacks
    def _process_relation_resolved_changes(self):
        """Invoke retry transitions on relations if their not running.
        """
        relation_resolved = yield self._unit.get_relation_resolved()
        if relation_resolved is None:
            returnValue(None)
        else:
            yield self._unit.clear_relation_resolved()

        keys = set(relation_resolved).intersection(self._relations)
        for internal_rel_id in keys:
            workflow = self._relations[internal_rel_id]
            with (yield workflow.lock()):
                state = yield workflow.get_state()
                if state != "up":
                    yield workflow.transition_state("up")

    @inlineCallbacks
    def _on_service_relation_changes(self, old_relations, new_relations):
        """Callback for service relation watching.

        The callback is used to manage the unit relation lifecycle in
        accordance with the current relations of the service.

        @param old_relations: Previous service relations for a service. On the
               initial execution, this value is None.
        @param new_relations: Current service relations for a service.
        """
        self._log.debug(
            "services changed old:%s new:%s", old_relations, new_relations)

        # Acquire the run lock, and process the changes.
        yield self._run_lock.acquire()
        try:
            # If the lifecycle is not running, then stop the watcher
            if not self._running:
                self._log.debug("stop service-rel watcher, discarding changes")
                self._watching_relation_memberships = False
                raise StopWatcher()

            self._log.debug("processing relations changed")
            yield self._process_service_changes(old_relations, new_relations)
        finally:
            self._run_lock.release()

    @inlineCallbacks
    def _process_service_changes(self, old_relations, new_relations):
        """Add and remove unit lifecycles per the service relations Determine.
        """
        # Calculate delta between zookeeper state and our stored state.
        new_relations = dict(
            (service_relation.internal_relation_id, service_relation)
            for service_relation in new_relations)

        if old_relations:
            old_relations = dict(
                (service_relation.internal_relation_id, service_relation)
                for service_relation in old_relations)

        added = set(new_relations.keys()) - set(self._relations.keys())
        removed = set(self._relations.keys()) - set(new_relations.keys())
        # Could this service be a principal container?
        is_principal = not (yield self._service.is_subordinate())

        # Once we know a relation is departed, *immediately* stop running
        # its hooks. We can't really handle the case in which a hook is
        # *already* running, but we can at least make sure it doesn't run
        # any *more* hooks (which could have been queued in the past, but
        # not yet executed).# This isn't *currently* an exceptionally big
        # deal, because:
        #
        # (1) The ZK state won't actually be deleted, so an inappropriate
        #     hook will still run happily.
        # (2) Even if the state is deleted, and the hook errors out, the
        #     only actual consequence is that we'll eventually run the
        #     error_depart transition rather than depart or down_depart.
        #
        # However, (1) will certainly change in the future, and (2) is not
        # necessarily a watertight guarantee.
        for relation_id in removed:
            yield self._relations[relation_id].lifecycle.stop()

        # Actually depart old relations.
        for relation_id in removed:
            workflow = self._relations.pop(relation_id)
            with (yield workflow.lock()):
                yield workflow.transition_state("departed")
            self._store_relations()

        # Process new relations.
        for relation_id in added:
            service_relation = new_relations[relation_id]
            yield self._add_relation(service_relation)
            if (is_principal and service_relation.relation_scope == "container"):
                self._add_subordinate_unit(service_relation)
            yield self._store_relations()

    @inlineCallbacks
    def _add_relation(self, service_relation):
        try:
            unit_relation = yield service_relation.get_unit_state(
                self._unit)
        except UnitRelationStateNotFound:
            # This unit has not yet been assigned a unit relation state,
            # Go ahead and add one.
            unit_relation = yield service_relation.add_unit_state(
                self._unit)

        lifecycle = UnitRelationLifecycle(
            self._client, self._unit.unit_name, unit_relation,
            service_relation.relation_ident,
            self._unit_dir, self._state_dir, self._executor)

        workflow = RelationWorkflowState(
            self._client, unit_relation, service_relation.relation_name,
            lifecycle, self._state_dir)

        self._relations[service_relation.internal_relation_id] = workflow

        with (yield workflow.lock()):
            yield workflow.synchronize()

    @inlineCallbacks
    def _do_unit_deploy(self, unit_name, machine_id, charm_dir):
        # this method exists to aid testing rather than being an
        # inline
        unit_deployer = UnitDeployer(self._client, machine_id, charm_dir)
        yield unit_deployer.start("subordinate")
        yield unit_deployer.start_service_unit(unit_name)

    @inlineCallbacks
    def _add_subordinate_unit(self, service_relation):
        """Deploy a subordinate unit for service_relation remote endpoint."""
        # Figure out the remote service state
        service_states = yield service_relation.get_service_states()
        subordinate_service = [s for s in service_states if
                               s.service_name != self._unit.service_name][0]

        # add a unit state to service (using self._unit as the
        # principal container)
        subordinate_unit = yield subordinate_service.add_unit_state(
            container=self._unit)
        machine_id = yield self._unit.get_assigned_machine_id()

        subordinate_unit_dir = os.path.dirname(self._unit_dir)
        charm_dir = os.path.join(subordinate_unit_dir,
                                 subordinate_unit.unit_name.replace(
                                     "/", "-"))
        state_dir = os.path.join(charm_dir, "state")
        if not os.path.exists(state_dir):
                os.makedirs(state_dir)

        self._log.debug("deploying %s as subordinate of %s",
                        subordinate_unit.unit_name,
                        self._unit.unit_name)
        # with the relation in place and the units added to the
        # container we can start the unit agent
        yield self._do_unit_deploy(subordinate_unit.unit_name,
                                   machine_id,
                                   charm_dir)

    @property
    def _known_relations_path(self):
        return os.path.join(
            self._state_dir, "%s.lifecycle.relations" % self._unit.internal_id)

    def _store_relations(self):
        """Store *just* enough information to recreate RelationWorkflowStates.

        Note that we don't need to store the actual states -- if we can
        reconstruct the RWS, it will be responsible for finding its own state
        -- but we *do* need to store the fact of their existence, so that we
        can still depart broken relations even if they break while we're not
        running.
        """
        state_dict = {}
        for relation_wf in self._relations.itervalues():
            state_dict.update(relation_wf.get_relation_info())
        state = yaml.dump(state_dict)
        temp_path = self._known_relations_path + "~"

        with open(temp_path, "w") as f:
            f.write(state)
        os.rename(temp_path, self._known_relations_path)

    @inlineCallbacks
    def _load_relations(self):
        """Recreate workflows for any relation we had previously stored.

        All relations (including those already departed) are stored in
        ._relations (and will be added or departed as usual); but only
        relations *not* already departed will be synchronized, to avoid
        errors caused by trying to access ZK state that may not exist any
        more.
        """
        self._relations = {}
        if not os.path.exists(self._known_relations_path):
            return

        rsm = RelationStateManager(self._client)
        relations = yield rsm.get_relations_for_service(self._service)
        relations_by_id = dict((r.internal_relation_id, r) for r in relations)

        with open(self._known_relations_path) as f:
            known_relations = yaml.load(f.read())

        for relation_id, relation_info in known_relations.items():
            if relation_id in relations_by_id:
                # The service relation's still around: set up workflow as usual
                yield self._add_relation(relations_by_id[relation_id])
            else:
                # The relation has departed. Create an *un*synchronized
                # workflow and place it in relations for detection and
                # removal (with hook-firing) in _process_service_changes.
                workflow = self._reconstruct_workflow(
                    relation_id,
                    relation_info["relation_name"],
                    relation_info["relation_scope"])
                self._relations[relation_id] = workflow

    def _reconstruct_workflow(self, relation_id, relation_ident, relation_scope):
        """Create a RelationWorkflowState which may refer to outdated state.

        This means that *if* this service has already departed the relevant
        relation, it is not safe to synchronize the resultant workflow,
        because its lifecycle may attempt to watch state that doesn't exist.

        Since synchronization is a one-time occurrence, and this method has
        only one client, this shouldn't be too hard to keep track of.
        """
        unit_relation = UnitRelationState(
            self._client, self._service.internal_id, self._unit.internal_id,
            relation_id, relation_scope)
        lifecycle = UnitRelationLifecycle(
            self._client, self._unit.unit_name, unit_relation, relation_ident,
            self._unit_dir, self._state_dir, self._executor)
        relation_name = relation_ident.split(":")[0]
        return RelationWorkflowState(
            self._client, unit_relation, relation_name, lifecycle,
            self._state_dir)

    @inlineCallbacks
    def _execute_hook(self, hook_name, now=False):
        """Execute the hook with the given name.

        For priority hooks, the hook is scheduled and then the
        executioner started, before wait on the result.
        """
        hook_path = os.path.join(self._unit_dir, "charm", "hooks", hook_name)
        socket_path = os.path.join(self._unit_dir, HOOK_SOCKET_FILE)
        invoker = Invoker(
            HookContext(self._client, self._unit.unit_name), None,
            _EVIL_CONSTANT, socket_path, self._unit_dir, hook_log)
        yield invoker.start()

        if now:
            yield self._executor.run_priority_hook(invoker, hook_path)
        else:
            yield self._executor(invoker, hook_path)


class RelationInvoker(Invoker):
    """A relation hook invoker, that populates the environment.
    """

    def get_environment_from_change(self, env, change):
        """Populate environment with relation change information."""
        env["JUJU_RELATION"] = change.relation_name
        env["JUJU_RELATION_ID"] = change.relation_ident
        env["JUJU_REMOTE_UNIT"] = change.unit_name
        return env


class UnitRelationLifecycle(object):
    """Unit Relation Lifcycle management.

    Provides for watching related units in a relation, and executing hooks
    in response to changes. The lifecycle is driven by the workflow.

    The Unit relation lifecycle glues together a number of components.
    It controls a watcher that recieves watch events from zookeeper,
    and it controls a hook scheduler which gets fed those events. When
    the scheduler wants to execute a hook, the executor is called with
    the hook path and the hook invoker.

    **Relation hook invocation do not maintain global order or
    determinism across relations**. They only maintain ordering and
    determinism within a relation. A shared scheduler across relations
    would be needed to maintain such behavior.

    See docs/source/internals/unit-workflow-lifecycle.rst for a brief
    discussion of some of the more interesting implementation decisions.
    """

    def __init__(self, client, unit_name, unit_relation, relation_ident,
                 unit_dir, state_dir, executor):
        self._client = client
        self._unit_dir = unit_dir
        self._relation_ident = relation_ident
        self._relation_name = relation_ident.split(":")[0]
        self._unit_relation = unit_relation
        self._unit_name = unit_name
        self._executor = executor
        self._run_lock = DeferredLock()
        self._log = logging.getLogger("unit.relation.lifecycle")
        self._error_handler = None

        schedule_path = os.path.join(
            state_dir, "%s.schedule" % unit_relation.internal_relation_id)
        self._scheduler = HookScheduler(
            client, self._execute_change_hook, self._unit_relation,
            self._relation_ident, unit_name, schedule_path)
        self._watcher = None

    @property
    def watching(self):
        """Are we queuing up hook executions in response to state changes?"""
        return self._watcher and self._watcher.running

    @property
    def executing(self):
        """Are we currently dequeuing and executing any queued hooks?"""
        return self._scheduler.running

    def set_hook_error_handler(self, handler):
        """Set an error handler to be invoked if a hook errors.

        The handler should accept two parameters, the RelationChange that
        triggered the hook, and the exception instance.
        """
        self._error_handler = handler

    @inlineCallbacks
    def start(self, start_watches=True, start_scheduler=True):
        """Start watching related units and executing change hooks.

        :param bool start_watches: True to start relation watches

        :param bool start_scheduler: True to run the scheduler and actually
            react to any changes delivered by the watcher
        """
        yield self._run_lock.acquire()
        try:
            # Start the hook execution scheduler.
            if start_scheduler and not self.executing:
                self._scheduler.run()
            # Create a watcher if we don't have one yet.
            if self._watcher is None:
                self._watcher = yield self._unit_relation.watch_related_units(
                    self._scheduler.cb_change_members,
                    self._scheduler.cb_change_settings)
            # And start the watcher.
            if start_watches and not self.watching:
                yield self._watcher.start()
        finally:
            self._run_lock.release()
        self._log.debug(
            "started relation:%s lifecycle", self._relation_name)

    @inlineCallbacks
    def stop(self, stop_watches=True):
        """Stop executing relation change hooks; maybe stop watching changes.

        :param bool stop_watches: True to stop watches as well as scheduler
            (which will prevent changes from being detected and queued, as well
            as stopping them being executed).
        """
        yield self._run_lock.acquire()
        try:
            if stop_watches and self.watching:
                self._watcher.stop()
            if self._scheduler.running:
                self._scheduler.stop()
        finally:
            yield self._run_lock.release()
        self._log.debug("stopped relation:%s lifecycle", self._relation_name)

    @inlineCallbacks
    def depart(self):
        """Inform the charm that the service has departed the relation.
        """
        self._log.debug("depart relation lifecycle")
        unit_id = self._unit_relation.internal_unit_id
        context = DepartedRelationHookContext(
            self._client, self._unit_name, unit_id, self._relation_name,
            self._unit_relation.internal_relation_id)
        change = RelationChange(self._relation_ident, "departed", "")
        invoker = self._get_invoker(context, change)
        hook_name = "%s-relation-broken" % self._relation_name
        yield self._execute_hook(invoker, hook_name, change)

    def _get_invoker(self, context, change):
        socket_path = os.path.join(self._unit_dir, HOOK_SOCKET_FILE)
        return RelationInvoker(
            context, change, "constant", socket_path, self._unit_dir,
            hook_log)

    def _execute_change_hook(self, context, change):
        """Invoked by the contained HookScheduler, to execute a hook.

        We utilize the HookExecutor to execute the hook, if an
        error occurs, it will be reraised, unless an error handler
        is specified see ``set_hook_error_handler``.
        """
        if change.change_type == "departed":
            hook_name = "%s-relation-departed" % self._relation_name
        elif change.change_type == "joined":
            hook_name = "%s-relation-joined" % self._relation_name
        else:
            hook_name = "%s-relation-changed" % self._relation_name

        invoker = self._get_invoker(context, change)
        return self._execute_hook(invoker, hook_name, change)

    @inlineCallbacks
    def _execute_hook(self, invoker, hook_name, change):
        hook_path = os.path.join(
            self._unit_dir, "charm", "hooks", hook_name)
        yield self._run_lock.acquire()
        self._log.debug("Executing hook %s", hook_name)
        try:
            yield self._executor(invoker, hook_path)
        except Exception, e:
            # We can't hold the run lock when we invoke the error
            # handler, or we get a deadlock if the handler
            # manipulates the lifecycle.
            yield self._run_lock.release()
            self._log.warn("Error in %s hook: %s", hook_name, e)

            if not self._error_handler:
                raise
            self._log.info(
                "Invoked error handler for %s hook", hook_name)
            yield self._error_handler(change, e)
            returnValue(False)
        else:
            yield self._run_lock.release()
            returnValue(True)
