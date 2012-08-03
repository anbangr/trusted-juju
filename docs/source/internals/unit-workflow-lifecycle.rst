Notes on unit agent persistence
===============================

Introduction
------------

This was first written to explain the extensive changes made in the branch
lp:~fwereade/juju/restart-transitions; that branch has been split out into
four separate branches, but this discussion should remain a useful guide to
the changes made to the unit and relation workflows and lifecycles in the
course of making the unit agent resumable.


Glossary
--------

UA = UnitAgent
UL = UnitLifecycle
UWS = UnitWorkflowState
URL = UnitRelationLifecycle
RWS = RelationWorkflowState
URS = UnitRelationState
SRS = ServiceRelationState


Technical discussion
--------------------

Probably the most fundamental change is the addition of a "synchronize" method
to both UWS and RWS. Calling "synchronize" should generally be *all* you need
to do to put the workflow and associated components into "the right state"; ie
ZK state will be restored, the appropriate lifecycle will be started (or not),
and any initial transitons will automatically be fired ("start" for RWS;
"install", "start" for UWS).

The synchronize method keeps responsibility for the lifecycle's state purely in
the hands of the workflow; once a workflow is synced, the *only* necessary
interactions with it should be in response to changes in ZK.

The disadvantage is that lifecycle "start" and "stop" methods have become a
touch overloaded:

* UL.stop(): now takes "stop_relations" in addition to "fire_hooks", in which
    "stop_relations" being True causes the orginal behaviour (transition "up"
    RWSs to "down", as when transitioning the UWS to a "stopped" or "error"
    state), but False simply causes them to stop watching for changes (in
    preparation for an orderly shutdown, for example).

* UL.start(): now takes "start_relations" in addition to "fire_hooks", in which
    the "start_relations" flag being True causes the original behaviour
    (automatically transition "down" RWSs to "up", as when restarting/resolving
    the UWS), while False causes the RWSs only to be synced.

* URL.start(): now takes "scheduler" in addition to "watches", allowing the
    watching and the contained HookScheduler to be controlled separately
    (allowing us to actually perform the RWS synchronise correctly).

* URL.stop(): still just takes "watches", because there wasn't a scenario in
    which I wanted to stop the watches but not the HookScheduler.

I still think it's a win, though: and I don't think that turning them into
separate methods is the right way to go; "start" and "stop" remain perfectly
decent and appropriate names for what they do.

Now this has been done, we can always launch directly into whatever state we
shut down in, and that's great, because sudden process death doesn't hurt us
any more [0] [1]. Except... when we're upgrading a charm. It emerges that the
charm upgrade state transition only covers the process of firing the hook, and
not the process of actually upgrading the charm.

In short, we had a mechanism, completely outside the workflow's purview, for
potentially *brutal* modifications of state (both in terms of the charm itself,
on disk, and also in that the hook executor should remain stopped forever while
in "charm_upgrade_error" state); and this rather scuppered the "restart in the
same state" goal. The obvious thing to do was to move the charm upgrade
operation into the "charm_upgrade" transition, so we had a *chance* of being
able to start in the correct state.

UL.upgrade_charm, called by UWS, does itself have subtleties, but it should be
reasonably clear when examined in context; the most important point is that it
will call back at the start and end of the risky period, and that the UWS's
handler for this callback sets a flag in "started"'s state_vars for the
duration of the upgrade. If that flag is set when we subsequently start up
again and synchronize the UWS, then we know to immediately force the
charm_upgrade_error state and work from there.

[0] Well, it does, because we need to persist more than just the (already-
persisted) workflow state. This branch includes RWS persistence in the UL, as
requested in this branch's first pre-review (back in the day...), but does not
include HookScheduler persistence in the URLs, so it remains possible for
relation hooks which have been queued, but not yet executed, to be lost if the
process executes before the queue empties. That will be coming in another
branch (resolve-unit-relation-diffs).

[1] This seems like a good time to mention the UL's relation-broken handling
for relations that went away while the process was stopped: every time
._relations is changed, it writes out enough state to recreate a Frankenstein's
URS object, which it can then use on load to reconstruct the necessary URL and
hence RWS.

We don't strictly need to *reconstruct* it in every case -- we can just use
SRS.get_unit_state if the relation still exists -- but given that sometimes we
do, it seemed senseless to have two code paths for the same operations. Of the
RWSs we reconstruct, those with existing SRSs will be synchronized (because we
know it's safe to do so), and the remainder will be stored untouched (because
we know that _process_service_changes will fire the "depart" transition for us
before doing anything else... and the "relation-broken" hook will be executed
in a DepartedRelationHookContext, which is rather restricted, and so shouldn't
cause the Frankenstein's URS to hit state we can't be sure exists).


Appendix: a rough history of changes to restart-transitions
-----------------------------------------------------------

* Add UWS transitions from "stopped" to "started", so that process restarts can
    be made to restart UWSs.
* Upon review, add RWS persistence to UL, to ensure we can't miss
    relation-broken hooks; as part of this, as discussed, add
    DepartedRelationHookContext in which to execute them.
* Upon discussion, discover that original UWS "started" -> "stopped" behaviour
    on process shutdown is not actually the desired behaviour (and that the
    associated RWS "up" -> "down" shouldn't happen either.
* Make changes to UL.start/stop, and add UWS/RWS.synchronize, to allow us to
    shut down workflows cleanly without transitions and bring them up again in
    the same state.
* Discover that we don't have any other reason to transition UWS to "stopped";
    to actually fire stop hooks at the right time, we need a more sophisticated
    system (possibly involving the machine agent telling the unit agent to shut
    itself down). Remove the newly-added "restart" transitions, because they're
    meaningless now; ponder what good it does us to have a "stopped" state that
    we never actually enter; chicken out of actually removing it.
* Realise that charm upgrades do an end-run around the whole UWS mechanism, and
    resolve to integrate them so I can actually detect upgrades left incomplete
    due to process death.
* Move charm upgrade operation from agent into UL; come to appreciate the
    subtleties of the charm upgrade process; make necessary tweaks to
    UL.upgrade_charm, and UWS, to allow for synchronization of incomplete
    upgrades.
