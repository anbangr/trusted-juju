"""A simple state machine for twisted applications.

Responsibilities are divided between three classes. A workflow class,
composed of transitions, and responsible for verifying the transitions
available from each state. The transitions define their endpoints, and
optionally a transition action, and an error transition. When the
transition is executed to move a context between two endpoint states, the
transition action is invoked. If it fails with a TransitionError, the
error transition is fired. If it succeeds, it can return a dictionary
of values. These values are stored.

The workflow state class, forms the basis for interacting with the workflow
system. It bridges an arbitrary domain objectcontext, with its associated
workflow. Its used to fire transitions, store/load state, and as a location
for defining any relevant transition actions.
"""

import logging

from twisted.internet.defer import DeferredLock, inlineCallbacks, returnValue


class WorkflowError(Exception):
    pass


class InvalidStateError(WorkflowError):
    pass


class InvalidTransitionError(WorkflowError):
    pass


class TransitionError(WorkflowError):
    pass


log = logging.getLogger("statemachine")


def class_name(instance):
    return instance.__class__.__name__.lower()


class _ExitCaller(object):

    def __init__(self, func):
        self._func = func

    def __enter__(self):
        pass

    def __exit__(self, *exc_info):
        self._func()


class WorkflowState(object):

    _workflow = None

    def __init__(self, workflow=None):
        if workflow:
            self._workflow = workflow
        self._observer = None
        self._lock = DeferredLock()

    @inlineCallbacks
    def lock(self):
        yield self._lock.acquire()
        returnValue(_ExitCaller(self._lock.release))

    def _assert_locked(self):
        """Should be called at the start of any method which changes state.

        This is a frankly pitiful hack that should (handwave handwave) help
        people to use this correctly; it doesn't stop anyone from calling
        write methods on this object while someone *else* holds a lock, but
        hopefully it will help us catch these situations when unit testing.

        This method only exists as a place to put this documentation.
        """
        assert self._lock.locked

    @inlineCallbacks
    def get_available_transitions(self):
        """Return a list of valid transitions from the current state.
        """
        state_id = yield self.get_state()
        returnValue(self._workflow.get_transitions(state_id))

    @inlineCallbacks
    def fire_transition_alias(self, transition_alias):
        """Fire a transition with the matching alias.

        A transition from the current state with the given alias will
        be located.

        The purpose of alias is to allow groups of transitions, each
        from a different state, to be invoked unambigiously by
        a caller, for example::

          >> state.fire_transition_alias("upgrade")
          >> state.fire_transition_alias("settings-changed")
          >> state.fire_transition_alias("error")

        All will invoke the appropriate transition from their state
        without the caller having to do state inspection or transition
        id mangling.

        Ambigious (multiple) or no matching transitions cause an exception
        InvalidTransition to be raised.
        """
        self._assert_locked()

        found = []
        for t in (yield self.get_available_transitions()):
            if transition_alias == t.alias:
                found.append(t)

        if len(found) > 1:
            current_state = yield self.get_state()
            raise InvalidTransitionError(
                "Multiple transition for alias:%s state:%s transitions:%s" % (
                    transition_alias, current_state, found))

        if len(found) == 0:
            current_state = yield self.get_state()
            raise InvalidTransitionError(
                "No transition found for alias:%s state:%s" % (
                    transition_alias, current_state))

        value = yield self.fire_transition(found[0].transition_id)
        returnValue(value)

    @inlineCallbacks
    def transition_state(self, state_id):
        """Attempt a transition to the given state.

        Will look for a transition to the given state from the
        current state, and execute if it one exists.

        Returns a boolean value based on whether the state
        was achieved.
        """
        self._assert_locked()

        # verify it's a valid state id
        if not self._workflow.has_state(state_id):
            raise InvalidStateError(state_id)

        transitions = yield self.get_available_transitions()
        for transition in transitions:
            if transition.destination == state_id:
                break
        else:
            returnValue(False)

        log.debug("%s: transition state (%s -> %s)",
                  class_name(self),
                  transition.source,
                  transition.destination)
        result = yield self.fire_transition(transition.transition_id)
        returnValue(result)

    @inlineCallbacks
    def fire_transition(self, transition_id, **state_variables):
        """Fire a transition with given id.

        Invokes any transition actions, saves state and state variables, and
        error transitions as needed.
        """
        self._assert_locked()

        # Verify and retrieve the transition.
        available = yield self.get_available_transitions()
        available_ids = [t.transition_id for t in available]
        if not transition_id in available_ids:
            current_state = yield self.get_state()
            raise InvalidTransitionError(
                "%r not a valid transition for state %s" % (
                    transition_id, current_state))
        yield self.set_inflight(transition_id)
        transition = self._workflow.get_transition(transition_id)

        log.debug("%s: transition %s (%s -> %s) %r",
                  class_name(self),
                  transition_id,
                  transition.source,
                  transition.destination,
                  state_variables)

        # Execute any per transition action.
        state_variables = state_variables
        action_id = "do_%s" % transition_id
        action = getattr(self, action_id, None)

        if callable(action):
            try:
                log.debug("%s:  execute action %s",
                          class_name(self), action.__name__)
                variables = yield action()
                if isinstance(variables, dict):
                    state_variables.update(variables)
            except TransitionError, e:
                # If an error happens during the transition, allow for
                # executing an error transition.
                if transition.error_transition_id:
                    log.debug("%s:  executing error transition %s, %s",
                              class_name(self),
                              transition.error_transition_id,
                              e)
                    yield self.fire_transition(
                        transition.error_transition_id)
                else:
                    yield self.set_inflight(None)
                    log.debug("%s:  transition %s failed %s",
                              class_name(self), transition_id, e)
                # Bail, and note the error as a return value.
                returnValue(False)

        # Set the state with state variables (and implicitly clear inflight)
        yield self.set_state(transition.destination, **state_variables)
        log.debug("%s: transition complete %s (state %s) %r",
                  class_name(self), transition_id,
                  transition.destination, state_variables)
        yield self._fire_automatic_transitions()
        returnValue(True)

    @inlineCallbacks
    def _fire_automatic_transitions(self):
        self._assert_locked()
        available = yield self.get_available_transitions()
        for t in available:
            if t.automatic:
                yield self.fire_transition(t.transition_id)
                return

    @inlineCallbacks
    def get_state(self):
        """Get the current workflow state.
        """
        state_dict = yield self._load()
        if not state_dict:
            returnValue(None)
        returnValue(state_dict["state"])

    @inlineCallbacks
    def get_state_variables(self):
        """Retrieve a dictionary of variables associated to the current state.
        """
        state_dict = yield self._load()
        if not state_dict:
            returnValue({})
        returnValue(state_dict["state_variables"])

    def set_observer(self, observer):
        """Set a callback, that will be notified on state changes.

        The caller will receive the new state and the new state
        variables as dictionary via positional args. ie.::

           def callback(new_state, state_variables):
               print new_state, state_variables
        """
        self._observer = observer

    @inlineCallbacks
    def set_state(self, state, **variables):
        """Set the current workflow state, optionally setting state variables.
        """
        self._assert_locked()
        yield self._store(dict(state=state, state_variables=variables))
        if self._observer:
            self._observer(state, variables)

    @inlineCallbacks
    def set_inflight(self, transition_id):
        """Record intent to perform a transition, or completion of same.

        Ideally, this would not be exposed to the public, but it's necessary
        for writing sane tests.
        """
        self._assert_locked()
        state = yield self._load() or {}
        state.setdefault("state", None)
        state.setdefault("state_variables", {})
        if transition_id is not None:
            state["transition_id"] = transition_id
        else:
            state.pop("transition_id", None)
        yield self._store(state)

    @inlineCallbacks
    def get_inflight(self):
        """Get the id of the transition that is currently executing.

        (Or which was abandoned due to unexpected process death.)
        """
        state = yield self._load() or {}
        returnValue(state.get("transition_id"))

    @inlineCallbacks
    def synchronize(self):
        """Rerun inflight transition, if any, and any default transitions."""
        self._assert_locked()
        # First of all, complete any abandoned transition.
        transition_id = yield self.get_inflight()
        if transition_id is not None:
            yield self.fire_transition(transition_id)
        else:
            yield self._fire_automatic_transitions()

    def _load(self):
        """ Load the state and variables from persistent storage.
        """
        pass

    def _store(self, state_dict):
        """ Store the state and variables to persistent storage.
        """
        pass


class Workflow(object):

    def __init__(self, *transitions):
        self.initialize(transitions)

    def initialize(self, transitions):
        """Initialize the internal data structures with the given transitions.
        """
        self._sources = {}
        self._transitions = {}
        for t in transitions:
            self._sources.setdefault(t.source, []).append(t.transition_id)
            self._sources.setdefault(t.destination, [])
            self._transitions[t.transition_id] = t

    def get_transitions(self, source_id):
        """Retrieve transition ids valid from the srource id state.
        """
        if not source_id in self._sources:
            raise InvalidStateError(source_id)
        transitions = self._sources[source_id]
        return [self._transitions[t] for t in transitions]

    def get_transition(self, transition_id):
        """Retrieve a transition object by id.
        """
        return self._transitions[transition_id]

    def has_state(self, state_id):
        return state_id in self._sources


class Transition(object):
    """A transition encapsulates an edge in the statemachine graph.

    :attr:`transition_id` The identity of the transition.
    :attr:`label` A human readable label of the transition's purpose.
    :attr:`source` The origin/source state of the transition.
    :attr:`destination` The target/destination state of the transition.
    :attr:`action_id` The name of the action method to use for this transition.
    :attr:`error_transition_id`: A transition to fire if the action fails.
    :attr:`automatic`: If true, always try to fire this transition whenever in
        `source` state.
    :attr:`alias` See :meth:`WorkflowState.fire_transition_alias`
    """
    def __init__(self, transition_id, label, source, destination,
                 error_transition_id=None, automatic=False, alias=None):

        self._transition_id = transition_id
        self._label = label
        self._source = source
        self._destination = destination
        self._error_transition_id = error_transition_id
        self._automatic = automatic
        self._alias = alias

    @property
    def transition_id(self):
        """The id of this transition.
        """
        return self._transition_id

    @property
    def label(self):
        return self._label

    @property
    def destination(self):
        """The destination state id of this transition.
        """
        return self._destination

    @property
    def source(self):
        """The origin state id of this transition.
        """
        return self._source

    @property
    def alias(self):
        return self._alias

    @property
    def error_transition_id(self):
        """The id of a transition to fire upon an error of this transition.
        """
        return self._error_transition_id

    @property
    def automatic(self):
        """Should this transition always fire whenever possible?
        """
        return self._automatic
