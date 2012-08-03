import logging

from twisted.internet.defer import succeed, fail, inlineCallbacks, Deferred

from juju.lib.testing import TestCase
from juju.lib.statemachine import (
    Workflow, Transition, WorkflowState, InvalidStateError,
    InvalidTransitionError, TransitionError)


class TestWorkflowState(WorkflowState):

    _workflow_state = None

    # required workflow state implementations
    def _store(self, state_dict):
        self._workflow_state = state_dict
        return succeed(True)

    def _load(self):
        return self._workflow_state


class AttributeWorkflowState(TestWorkflowState):

    # transition handlers.
    def do_jump_puddle(self):
        self._jumped = True

    def do_error_transition(self):
        self._error_handler_invoked = True
        return dict(error=True)

    def do_transition_variables(self):
        return dict(hello="world")

    def do_error_unknown(self):
        raise AttributeError("unknown")

    def do_error_deferred(self):
        return fail(AttributeError("unknown"))

    def do_raises_transition_error(self):
        raise TransitionError("eek")


class StateMachineTest(TestCase):

    def setUp(self):
        super(StateMachineTest, self).setUp()
        self.log_stream = self.capture_logging(
            "statemachine", level=logging.DEBUG)

    def test_transition_constructor(self):
        t = Transition("id", "label", "source_state", "destination_state")
        self.assertEqual(t.transition_id, "id")
        self.assertEqual(t.label, "label")
        self.assertEqual(t.source, "source_state")
        self.assertEqual(t.destination, "destination_state")

    def test_workflow_get_transitions(self):
        workflow = Workflow(
            Transition("init_workflow", "", None, "initialized"),
            Transition("start", "", "initialized", "started"))
        self.assertRaises(InvalidStateError,
                          workflow.get_transitions,
                          "magic")
        self.assertEqual(
            workflow.get_transitions("initialized"),
            [workflow.get_transition("start")])

    def test_workflow_get_transition(self):
        transition = Transition("init_workflow", "", None, "initialized")
        workflow = Workflow(
            Transition("init_workflow", "", None, "initialized"),
            Transition("start", "", "initialized", "started"))
        self.assertRaises(KeyError, workflow.get_transition, "rabid")
        self.assertEqual(
            workflow.get_transition("init_workflow").transition_id,
            transition.transition_id)

    @inlineCallbacks
    def test_state_get_available_transitions(self):
        workflow = Workflow(
            Transition("init_workflow", "", None, "initialized"),
            Transition("start", "", "initialized", "started"))
        workflow_state = AttributeWorkflowState(workflow)
        transitions = yield workflow_state.get_available_transitions()
        yield self.assertEqual(
            transitions, [workflow.get_transition("init_workflow")])

    @inlineCallbacks
    def test_fire_transition_alias_multiple(self):
        workflow = Workflow(
            Transition("init", "", None, "initialized", alias="init"),
            Transition("init_start", "", None, "started", alias="init"))
        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield self.assertFailure(
                workflow_state.fire_transition_alias("init"),
                InvalidTransitionError)

    @inlineCallbacks
    def test_fire_transition_alias_none(self):
        workflow = Workflow(
            Transition("init_workflow", "", None, "initialized"),
            Transition("start", "", "initialized", "started"))
        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield self.assertFailure(
                workflow_state.fire_transition_alias("dog"),
                InvalidTransitionError)

    @inlineCallbacks
    def test_fire_transition_alias(self):
        workflow = Workflow(
            Transition("init_magic", "", None, "initialized", alias="init"))
        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            value = yield workflow_state.fire_transition_alias("init")
        self.assertEqual(value, True)

    @inlineCallbacks
    def test_state_get_set(self):
        workflow = Workflow(
            Transition("init_workflow", "", None, "initialized"),
            Transition("start", "", "initialized", "started"))

        workflow_state = AttributeWorkflowState(workflow)
        current_state = yield workflow_state.get_state()
        self.assertEqual(current_state, None)
        current_vars = yield workflow_state.get_state_variables()
        self.assertEqual(current_vars, {})

        with (yield workflow_state.lock()):
            yield workflow_state.set_state("started")

        current_state = yield workflow_state.get_state()
        self.assertEqual(current_state, "started")
        current_vars = yield workflow_state.get_state_variables()
        self.assertEqual(current_vars, {})

    @inlineCallbacks
    def test_state_fire_transition(self):
        workflow = Workflow(
            Transition("init_workflow", "", None, "initialized"),
            Transition("start", "", "initialized", "started"))
        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield workflow_state.fire_transition("init_workflow")
            current_state = yield workflow_state.get_state()
            self.assertEqual(current_state, "initialized")
            yield workflow_state.fire_transition("start")
            current_state = yield workflow_state.get_state()
            self.assertEqual(current_state, "started")
            yield self.assertFailure(workflow_state.fire_transition("stop"),
                                     InvalidTransitionError)

        name = "attributeworkflowstate"
        output = (
            "%s: transition init_workflow (None -> initialized) {}",
            "%s: transition complete init_workflow (state initialized) {}",
            "%s: transition start (initialized -> started) {}",
            "%s: transition complete start (state started) {}\n")
        self.assertEqual(self.log_stream.getvalue(),
                         "\n".join([line % name for line in output]))

    @inlineCallbacks
    def test_state_transition_callback(self):
        """If the workflow state, defines an action callback for a transition,
        its invoked when the transition is fired.
        """
        workflow = Workflow(
            Transition("jump_puddle", "", None, "dry"))

        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield workflow_state.fire_transition("jump_puddle")
        current_state = yield workflow_state.get_state()
        self.assertEqual(current_state, "dry")
        self.assertEqual(
            getattr(workflow_state, "_jumped", None),
            True)

    @inlineCallbacks
    def test_transition_action_workflow_error(self):
        """If a transition action callback raises a transitionerror, the
        transition does not complete, and the state remains the same.
        The fire_transition method in this case returns False.
        """
        workflow = Workflow(
            Transition("raises_transition_error", "", None, "next-state"))
        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            result = yield workflow_state.fire_transition(
                "raises_transition_error")
        self.assertEqual(result, False)
        current_state = yield workflow_state.get_state()
        self.assertEqual(current_state, None)

        name = "attributeworkflowstate"
        output = (
            "%s: transition raises_transition_error (None -> next-state) {}",
            "%s:  execute action do_raises_transition_error",
            "%s:  transition raises_transition_error failed eek\n")
        self.assertEqual(self.log_stream.getvalue(),
                         "\n".join([line % name for line in output]))

    @inlineCallbacks
    def test_transition_action_unknown_error(self):
        """If an unknown error is raised by a transition action, it
        is raised from the fire transition method.
        """
        workflow = Workflow(
            Transition("error_unknown", "", None, "next-state"))

        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield self.assertFailure(
                workflow_state.fire_transition("error_unknown"),
                AttributeError)

    @inlineCallbacks
    def test_transition_resets_state_variables(self):
        """State variables are only stored, while the associated state is
        current.
        """
        workflow = Workflow(
            Transition("transition_variables", "", None, "next-state"),
            Transition("some_transition", "", "next-state", "final-state"))

        workflow_state = AttributeWorkflowState(workflow)
        state_variables = yield workflow_state.get_state_variables()
        self.assertEqual(state_variables, {})

        with (yield workflow_state.lock()):
            yield workflow_state.fire_transition("transition_variables")
            current_state = yield workflow_state.get_state()
            self.assertEqual(current_state, "next-state")
            state_variables = yield workflow_state.get_state_variables()
            self.assertEqual(state_variables, {"hello": "world"})

            yield workflow_state.fire_transition("some_transition")
            current_state = yield workflow_state.get_state()
            self.assertEqual(current_state, "final-state")
            state_variables = yield workflow_state.get_state_variables()
            self.assertEqual(state_variables, {})

    @inlineCallbacks
    def test_transition_success_transition(self):
        """If a transition specifies a success transition, and its action
        handler completes successfully, the success transistion and associated
        action handler are executed.
        """
        workflow = Workflow(
            Transition("initialized", "", None, "next"),
            Transition("markup", "", "next", "final", automatic=True),
            )
        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield workflow_state.fire_transition("initialized")
        self.assertEqual((yield workflow_state.get_state()), "final")

    @inlineCallbacks
    def test_transition_error_transition(self):
        """If a transition specifies an error transition, and its action
        handler raises a transition error, the error transition and associated
        hooks are executed.
        """
        workflow = Workflow(
            Transition("raises_transition_error", "", None, "next-state",
                       error_transition_id="error_transition"),
            Transition("error_transition", "", None, "error-state"))

        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield workflow_state.fire_transition("raises_transition_error")

        current_state = yield workflow_state.get_state()
        self.assertEqual(current_state, "error-state")
        state_variables = yield workflow_state.get_state_variables()
        self.assertEqual(state_variables, {"error": True})

    @inlineCallbacks
    def test_state_machine_observer(self):
        """A state machine observer can be registered for tests visibility
        of async state transitions."""

        results = []

        def observer(state, variables):
            results.append((state, variables))

        workflow = Workflow(
            Transition("begin", "", None, "next-state"),
            Transition("continue", "", "next-state", "final-state"))

        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            workflow_state.set_observer(observer)
            yield workflow_state.fire_transition("begin")
            yield workflow_state.fire_transition("continue")

        self.assertEqual(results,
                         [("next-state", {}), ("final-state", {})])

    @inlineCallbacks
    def test_state_variables_via_transition(self):
        """Per state variables can be passed into the transition.
        """
        workflow = Workflow(
            Transition("begin", "", None, "next-state"),
            Transition("continue", "", "next-state", "final-state"))

        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            yield workflow_state.fire_transition(
                "begin", rabbit="moon", hello=True)
            current_state = yield workflow_state.get_state()
            self.assertEqual(current_state, "next-state")
            variables = yield workflow_state.get_state_variables()
            self.assertEqual({"rabbit": "moon", "hello": True}, variables)

            yield workflow_state.fire_transition("continue")
            current_state = yield workflow_state.get_state()
            self.assertEqual(current_state, "final-state")
            variables = yield workflow_state.get_state_variables()
            self.assertEqual({}, variables)

    @inlineCallbacks
    def test_transition_state(self):
        """Transitions can be specified by the desired state.
        """
        workflow = Workflow(
            Transition("begin", "", None, "trail"),
            Transition("to_cabin", "", "trail", "cabin"),
            Transition("to_house", "", "trail", "house"))

        workflow_state = AttributeWorkflowState(workflow)
        with (yield workflow_state.lock()):
            result = yield workflow_state.transition_state("trail")
            self.assertEqual(result, True)
            current_state = yield workflow_state.get_state()
            self.assertEqual(current_state, "trail")

            result = yield workflow_state.transition_state("cabin")
            self.assertEqual(result, True)

            result = yield workflow_state.transition_state("house")
            self.assertEqual(result, False)

            self.assertFailure(workflow_state.transition_state("unknown"),
                               InvalidStateError)

    @inlineCallbacks
    def test_load_bad_state(self):
        class BadLoadWorkflowState(WorkflowState):
            def _load(self):
                return succeed({"some": "other-data"})

        workflow = BadLoadWorkflowState(Workflow())
        yield self.assertFailure(workflow.get_state(), KeyError)
        yield self.assertFailure(workflow.get_state_variables(), KeyError)


SyncWorkflow = Workflow(
    Transition("init", "", None, "inited", error_transition_id="error_init"),
    Transition("error_init", "", None, "borken"),
    Transition("start", "", "inited", "started", automatic=True),

    # Disjoint states for testing default transition synchronize.
    Transition("predefault", "", "default_init", "default_start"),
    Transition("default", "", "default_start", "default_end", automatic=True),
)

class SyncWorkflowState(TestWorkflowState):

    _workflow = SyncWorkflow

    def __init__(self):
        super(SyncWorkflowState, self).__init__()
        self.started = {
            "init": Deferred(), "error_init": Deferred(), "start": Deferred()}
        self.blockers = {
            "init": Deferred(), "error_init": Deferred(), "start": Deferred()}

    def do(self, transition):
        self.started[transition].callback(None)
        return self.blockers[transition]

    def do_init(self):
        return self.do("init")

    def do_error_init(self):
        return self.do("error_init")

    def do_start(self):
        return self.do("start")


class StateMachineSynchronizeTest(TestCase):

    @inlineCallbacks
    def setUp(self):
        yield super(StateMachineSynchronizeTest, self).setUp()
        self.workflow = SyncWorkflowState()

    @inlineCallbacks
    def assert_state(self, state, inflight):
        self.assertEquals((yield self.workflow.get_state()), state)
        self.assertEquals((yield self.workflow.get_inflight()), inflight)

    @inlineCallbacks
    def test_plain_synchronize(self):
        """synchronize does nothing when no inflight transitions or applicable
        default transitions"""
        yield self.assert_state(None, None)
        with (yield self.workflow.lock()):
            yield self.workflow.synchronize()
        yield self.assert_state(None, None)

    @inlineCallbacks
    def test_synchronize_default_transition(self):
        """synchronize runs default transitions after inflight recovery"""
        with (yield self.workflow.lock()):
            yield self.workflow.set_state("default_init")
            yield self.workflow.set_inflight("predefault")
            yield self.workflow.synchronize()
            yield self.assert_state("default_end", None)

    @inlineCallbacks
    def test_synchronize_inflight_success(self):
        """synchronize will complete an unfinished transition and run the
        success transition where warranted"""
        with (yield self.workflow.lock()):
            yield self.workflow.set_inflight("init")
            d = self.workflow.synchronize()
            yield self.workflow.started["init"]
            yield self.assert_state(None, "init")
            self.workflow.blockers["init"].callback(None)
            yield self.workflow.started["start"]
            yield self.assert_state("inited", "start")
            self.workflow.blockers["start"].callback(None)
            yield d
            yield self.assert_state("started", None)

    @inlineCallbacks
    def test_synchronize_inflight_error(self):
        """synchronize will complete an unfinished transition and run the
        error transition where warranted"""
        with (yield self.workflow.lock()):
            yield self.workflow.set_inflight("init")
            d = self.workflow.synchronize()
            yield self.workflow.started["init"]
            yield self.assert_state(None, "init")
            self.workflow.blockers["init"].errback(TransitionError())
            yield self.workflow.started["error_init"]
            yield self.assert_state(None, "error_init")
            self.workflow.blockers["error_init"].callback(None)
            yield d
            yield self.assert_state("borken", None)

    @inlineCallbacks
    def test_error_without_transition_clears_inflight(self):
        """when a transition fails, it should no longer be marked inflight"""
        with (yield self.workflow.lock()):
            yield self.workflow.set_state("inited")
            d = self.workflow.fire_transition("start")
            yield self.workflow.started["start"]
            yield self.assert_state("inited", "start")
            self.workflow.blockers["start"].errback(TransitionError())
            yield d
            yield self.assert_state("inited", None)
