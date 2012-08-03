Notes on the current state of machine constraints
=================================================

Summary
-------

EC2 constraints and generic constraints can each be used to provision EC2 machines as intended. Orchestra support is not implemented, and should remain on hold until we get some clarity about MaaS. Environment constraints are not implemented; machine reuse is limited but predictable. Unit constraints, and a get-constraints command, should probably be added despite not being in the initial spec.

We should prepare the ground for, and implement, environment constraints above all else. Adding a --constraints argument to add-unit will be very easy, and adding a get-constraints command won't be much harder, and the feature will feel much more complete with these capabilities, so they should come next. After that we should, with care and discretion, start tweaking the unit/machine matching algorithms such that we do the Right Thing more often without running the risk of doing a surprising and/or costly Wrong Thing.


Code overview
-------------

`juju.machine.constraints` contains a `Constraints` type and a `_Constraint` type (which holds the configuration for a given constraint). The actual configuration is set up in `juju.machine.constraints_info` by means of calls to `register_constraint` and `register_conflicts`. Note that `ubuntu-series` and `provider-type` are themselves modelled as constraints (largely for ease/ consitency of representation) but are not exposed via `from_strs`, thereby preventing users from specifying things that should only be set by the code.

The juju defaults are contained in the individual configured `_Constraint` objects; empty values are converted to defaults at `from_strs` time (when a provider type and a list of strings are used to construct a `Constraints` object). Therefore, the juju defaults are not themselves a separate `Constraints` object.

Once created, `Constraints` objects store everything in their `_layer_data` dict, which contains keys for (1) every constraint specified explicitly and (2) every constraint conflicting with one specified explicitly (these are set to None). When storing constraints in ZK, all you need to do is serialise this dict (exposed as `layer_data`); `Constraints` objects can then be reconstructed directly from such dicts.

This layer data does not necessarily include all possible constraints; so, when a `Constraints` is being used as a dict, we ensure we return default values for every key not in `_layer_data` (we also convert values from their textual representations to more-easily-consumable ones; so, eg, a mem of "4G" becomes 4096.0). However, we don't have any reason to store unset constraints at any stage; doing so would make it far harder to update one `Constraints` object with another. This is because `_layer_data` is effectively a mask: by updating A with B, the conflict Nones in B will overwrite any values set in A; while any keys not overridden will retain their values or conflict Nones, which ensures that we don't accidentally insert defaults due to missing values.  Consider:

A = "orchestra-name=jimbob" (masks arch, cpu, mem, orchestra-classes)
B = "arch=arm orchestra-classes=ultralightweight" (masks orchestra-name)

Now, if we A.update(B): orchestra-name gets masked out; cpu and mem remain masked out from before; the only remaining (user-visible) constraints are arch and orchestra-classes. Because the "ultralightweight" orchestra-class could easily be referring to a set of machines with very low cpu values, we *don't* want to reapply the default cpu constraint: we want to keep it masked as None, so we don't end up with a surprisingly unfulfillable request.

It's a bit tricker on EC2: if arch overrides ec2-instance-type, this masks out cpu and mem as before and will therefore, in the absence of other constraints, deliver a t1.micro. IMO this is acceptable because: if someone's already thinking in instance-types, that's what they'll use in practice, so it's an edge case anyway; the results are actually not unreasonable; and the cost of incorrect behaviour is very low. The alternative -- of treating ec2-instance-type constraints as implicit arch/cpu/mem constraints, (so the update would end up with: a blank ec2-instance type; arch from the strings; and cpu/mem from the previous layer's ec2-instance-type) -- seems to me to be more likely to lead to surprising and potentially costly behaviour. This behaviour could be bolted on with little effort if deemed helpful, though.

There are a couple of extra notes on EC2 in the source, both in `constraints_info` and in the ec2 provider code itself. (Note that ec2 constraints are defined in `constraints_info`, not the provider itself, because I want to be sure (without excessive magic or circular imports) that all constraints have been registered *before* any `Constraints` object is constructed, lest one be constructed with a incorrect mask. (It would be quick and easy to notify `_Constraint` once a `Constraints` is constructed, and refuse to register further constraints and conflicts, to ensure nobody does this accidentally; I only just thought if this though.) Most of the code for interpreting constraints is in the ec2 provider, though; this is because it only comes into play at provisioning time.


Usage
-----

Actual use is pretty trivial, but involved an irritating weight of API changes: service states, unit states, and machine states all now require constraints, and it's tricky to figure out what the right "default" would be. (Well, we could grab provider-type from `GlobalSettingsStateManager` and construct it from an empty string list, but I have a disinclination to access such things without a very good reason; and since now, I think, all the "real" uses (as opposed to "test") can be expected to have constraints readily available, the inconvenience of changing all the tests doesn't seem like a valid argument against explicit specification. Does pollute the diffs a bit, though; sorry.)

Basically, constraints are set on a service via `deploy` or `set-constraints`; then, when a unit is added, they're copied from the service to the unit (note: will be trivial to combine with env constraints and save the combined constraints... that is, when we actually have some env constraints); then, when a machine is added for a unit, the constraints are matched against existing machines' states, or used to construct a new one onto which the unit will be placed; and finally, those machine constraints are eventually passed into the provider by the provisioning agent.

Bootstrap remains less than ideal -- but since we know the ubuntu-series and the provider-type, both on the client and on the bootstrap node, we can construct a full `Constraints` object and just use the default values for the other constraints; and, we can do so both when provisioning the initial machine (on the client) and when creating the bootstrap node's machine state (on the bootstrap node itself). When we have env constraints, we'll need to use them for provisioning *and* feed them through to the initialize command on the bootstrap node, so we can set up the correct machine state (in addition to the environment constraint state we'll need to set up).


Merge pipeline
--------------

* placement-spec (Just the spec. Despite the MaaS-related inaccuracies, worth merging, IMO.)

* constraint-types (Add `juju.machine.constraints` and `juju.machine.constraints_info`. Oh, and `juju.errors.ConstraintError`.)

* set-service-constraints (Add `set-constraints` command; add param to `deploy` command; store constraints in service state.)

* apply-machine-constraints (Constraints are set on the unit state by the service state, and on the machine state by (or from) the unit state; `initialize` command sets default constraints on machine/0.)

* pa-start-machine-constraints (When launching machines, the provisioning agent passes "constraints" in machine_data, which are now actually used by the ec2 provider (I couldn't break this up further: once the constraints were passed in, ec2 had to change). Also removed "default-instance-type" and "default-ami" from ec2 environment config, and set bootstrap to provision a machine from juju defaults. Note: appears to actually work as expected.)


Known problems
--------------

* Most horrifyingly: MaaS plans imply that we shouldn't depend on being able to specify `arch`, `cpu`, `mem`, or `orchestra-classes`, and I'm more than somewhat blindsided by this... certainly, with those (er) constraints, the eventual implementation cannot match the spec as currently written.

* No environment constraints are implemented; it's not hard per se (add handling to `bootstrap` and `set-constraints`; store them somewhere sensible; get and combine them at unit-add time) but (as discussed) it would be a good idea to make the environment settings behaviour saner *before* we add constraints to the mix. Once that's done, we need to remember to actually use the information at bootstrap time, both for provisioning the bootstrap node and constructing its ZK state.

  * I think it's fine for people to specify non-connection info inside their environments.yaml~s; but I think all such settings should be in a sub-dict called "initial" (or "bootstrap", or possibly even "default", as discussed at P-rally; I'm not sure which word carries the most appropriate semantic payload, but I'm not sure "default" is quite right).

* Unused machines are not reused very sensibly: we just grab the first one we see that fulfils the relevant service unit's constraints. It would be trivial to build up a list of matching machines; it's not immediately clear just how we should go about picking the most appropriate machine from such a list.

  * Well, ok, it's pretty clear -- we can "just" define a cost method on `Constraints`, and pick the cheapest. (But should we really reuse a cc1.4xlarge for a job that only demands an m1.small? Even if that's an easy question, what about orchestra? If we're interested in picking cheap machines -- and even assuming we can come up with a universally appropriate cost function -- we still need to consider the unused machines as well if we want to get a good answer.)

  I remain convinced that it'll have to take the provider into consideration somehow -- and, tangentially, that we'll need to be a bit more sophisticated about matching `ec2-instance-type` constraints against generic constraints and vice versa -- and so I'm getting a little concerned that the "single type for Constraints" preference expressed by you and Gustavo is not going to be workable in the long term (but it's set up to require a provider-type to create a `Constraints` anyway, so it should be trivial to factoryise it by provider-type anyway).

* We should probably bulk out machine constraints with actual data from the provider, once they've been provisioned (so we can tell, for example, what ec2-zone we ended up in (if it was left unspecified)). This affects how well we can match existing machines with new units; but what with MaaS, it remains an open question how much of this we'll even be able to do on orchestra(-without-landscape, anyway).

  Note that this functionality -- and other upgrades in machine-reuse sophistication, like the previous bullet -- should IMO be added slowly and incrementally; I think we'll inevitably make mistakes here, at least until we've properly figured out the usage patterns, and I think for our users' sake it's more important that the rules be *clear* than that they be clever.

* No `get-constraints` command exists. This command makes sense for all of env, service, unit, and machine; and on reflection, IMO, it should display the full constraints-as-dict in all cases (not just the layer data) so there's no chance of ambiguity; however, we should consider the implications of exporting explicit constraints as they were set, or as they "are" -- ie if we output mem as "4G" it will be irritating for machines but convenient for humans, while 4096.0 will be better suited for machine consumption (and also gives visibility into exactly what params will be passed to the provider itself). Not specced, but hard to justify leaving out.

* No explicit unit constraints are implemented; the code as it stands would make it *very* easy to set them at add-unit time, and some constraints (well, orchestra-name in particular) only really make sense at the unit level. Nonetheless, it was deliberately left out of the spec, and can be worked around by users where necesssary, so it's not really a priority; but IMO the benefit outwieghs the very small cost of implementing it.
